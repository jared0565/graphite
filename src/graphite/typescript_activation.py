"""Consent and policy orchestration for optional project-local TypeScript.

This module's detector is deliberately pure and filesystem-driven. Process
execution, consent, validation, installation, and verification belong to the
activation orchestration layer.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import Config
from .dependency_install import (
    ACTIVATION_MAX_FILES,
    MAX_CONTROL_FILE_BYTES,
    FileSnapshot,
    Manager,
    adapter_for,
    control_files_use_trusted_sources,
    snapshot_control_file,
)
from .ingest import SKIP_DIRS


class ActivationOutcome(StrEnum):
    INSTALLED = "installed"
    ALREADY_AVAILABLE = "already_available"
    NOT_APPLICABLE = "not_applicable"
    DECLINED = "declined"
    GUIDANCE_ONLY = "guidance_only"
    VALIDATION_FAILED = "validation_failed"
    INSTALLATION_FAILED = "installation_failed"
    VERIFICATION_FAILED = "verification_failed"


FATAL_OUTCOMES = frozenset(
    {
        ActivationOutcome.VALIDATION_FAILED,
        ActivationOutcome.INSTALLATION_FAILED,
        ActivationOutcome.VERIFICATION_FAILED,
    }
)


@dataclass(frozen=True)
class ActivationResult:
    outcome: ActivationOutcome
    manager: Manager | None
    reason: str
    manifest: str | None = None
    lockfile: str | None = None
    changed_files: tuple[str, ...] = ()
    attempted: bool = False

    @property
    def fatal(self) -> bool:
        return self.outcome in FATAL_OUTCOMES

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "manager": self.manager.value if self.manager else None,
            "reason": self.reason,
            "manifest": self.manifest,
            "lockfile": self.lockfile,
            "changed_files": list(self.changed_files),
            "attempted": self.attempted,
        }


@dataclass(frozen=True)
class ActivationDetection:
    result: ActivationResult | None
    manager: Manager | None
    manifest: str | None
    lockfile: str | None
    manifest_snapshot: FileSnapshot | None
    lockfile_snapshot: FileSnapshot | None


_PACKAGE_MANAGER_RE = re.compile(r"(npm|pnpm|yarn|bun)@(\S+)")
_STABLE_STAT_FIELDS = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
_EVIDENCE_SCAN_SECONDS = 1.0
_SUPPORTED_LOCKFILES = tuple(
    (lockfile, manager)
    for manager in Manager
    for lockfile in adapter_for(manager).lockfiles
)


class _EvidenceScanOutcome(StrEnum):
    FOUND = "found"
    ABSENT = "absent"
    LIMITED = "limited"
    UNSAFE = "unsafe"


def _is_reparse(details: os.stat_result) -> bool:
    attributes = getattr(details, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _is_contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path_is_lexically_present(path: Path) -> bool:
    try:
        return os.path.lexists(path)
    except (OSError, ValueError):
        return True


def _root_file_state(
    root: Path,
    relative_path: str,
    *,
    maximum_size: int | None = None,
) -> str:
    path = root / relative_path
    if not _path_is_lexically_present(path):
        return "missing"
    try:
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or _is_reparse(details):
            return "unsafe"
        if not stat.S_ISREG(details.st_mode):
            return "unsafe"
        if maximum_size is not None and details.st_size > maximum_size:
            return "oversized"
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return "unsafe"
    if canonical != path or not _is_contained(canonical, root):
        return "unsafe"
    return "safe"


def _evidence_entry_limit(value: int | None) -> int | None:
    if isinstance(value, bool) or (value is not None and not isinstance(value, int)):
        return None
    if value is not None and value < 0:
        return None
    return min(value or ACTIVATION_MAX_FILES, ACTIVATION_MAX_FILES)


def _configured_exclusions(root: Path, cfg: Config) -> tuple[tuple[str, ...], ...]:
    exclusions: set[tuple[str, ...]] = set()
    for configured in (cfg.output_dir, cfg.cache_dir):
        candidate = configured if configured.is_absolute() else root / configured
        try:
            relative = candidate.resolve(strict=False).relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if relative != Path("."):
            exclusions.add(tuple(os.path.normcase(part) for part in relative.parts))
    return tuple(sorted(exclusions))


def _is_excluded_path(
    parts: tuple[str, ...],
    exclusions: tuple[tuple[str, ...], ...],
) -> bool:
    normalized = tuple(os.path.normcase(part) for part in parts)
    return any(
        len(normalized) >= len(exclusion)
        and normalized[: len(exclusion)] == exclusion
        for exclusion in exclusions
    )


def _scan_typescript_evidence(
    root: Path,
    cfg: Config,
    entry_limit: int,
) -> _EvidenceScanOutcome:
    deadline = time.monotonic() + _EVIDENCE_SCAN_SECONDS
    exclusions = _configured_exclusions(root, cfg)
    stack: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
    visited = 0
    while stack:
        if time.monotonic() >= deadline:
            return _EvidenceScanOutcome.LIMITED
        directory, directory_parts = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > entry_limit or time.monotonic() >= deadline:
                        return _EvidenceScanOutcome.LIMITED
                    details = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(details.st_mode) or _is_reparse(details):
                        continue
                    parts = (*directory_parts, entry.name)
                    if stat.S_ISDIR(details.st_mode):
                        if (
                            entry.name in SKIP_DIRS
                            or (not cfg.include_dotfiles and entry.name.startswith("."))
                            or _is_excluded_path(parts, exclusions)
                        ):
                            continue
                        stack.append((Path(entry.path), parts))
                        continue
                    if not stat.S_ISREG(details.st_mode):
                        continue
                    if not cfg.include_dotfiles and entry.name.startswith("."):
                        continue
                    if Path(entry.name).suffix.lower() in {".ts", ".tsx"}:
                        return _EvidenceScanOutcome.FOUND
        except (OSError, RuntimeError, ValueError):
            return _EvidenceScanOutcome.UNSAFE
    return _EvidenceScanOutcome.ABSENT


def _read_stable_control_file(
    root: Path,
    relative_path: str,
) -> tuple[bytes, FileSnapshot] | None:
    path = root / relative_path
    descriptor = -1
    close_failed = False
    try:
        before_snapshot = snapshot_control_file(root, relative_path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_CONTROL_FILE_BYTES:
            return None
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_CONTROL_FILE_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, MAX_CONTROL_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        content = b"".join(chunks)
    except (OSError, RuntimeError, ValueError):
        return None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except (OSError, ValueError):
                close_failed = True
    if close_failed:
        return None
    try:
        after_snapshot = snapshot_control_file(root, relative_path)
    except ValueError:
        return None
    if (
        len(content) > MAX_CONTROL_FILE_BYTES
        or len(content) != before.st_size
        or any(getattr(before, field) != getattr(after, field) for field in _STABLE_STAT_FIELDS)
        or before_snapshot != after_snapshot
        or hashlib.sha256(content).hexdigest() != before_snapshot.sha256
    ):
        return None
    return content, before_snapshot


def _reject_json_constant(_value: str) -> None:
    raise ValueError("json_constant_invalid")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("json_duplicate_key")
        result[key] = value
    return result


def _parse_bounded_json_int(value: str) -> int:
    if len(value.lstrip("-")) > 1024:
        raise ValueError("json_integer_too_long")
    return int(value)


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("json_float_not_finite")
    return parsed


def _parse_manifest(content: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(
            content,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_int=_parse_bounded_json_int,
            parse_float=_parse_finite_json_float,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _terminal(
    outcome: ActivationOutcome,
    reason: str,
    *,
    manager: Manager | None = None,
    manifest: str | None = None,
    lockfile: str | None = None,
) -> ActivationDetection:
    return ActivationDetection(
        ActivationResult(outcome, manager, reason, manifest, lockfile),
        manager,
        manifest,
        lockfile,
        None,
        None,
    )


def detect_activation(
    root: Path,
    cfg: Config,
    *,
    local_typescript_available: bool,
) -> ActivationDetection:
    """Return ephemeral eligibility; revalidate it immediately before mutation."""
    try:
        canonical_root = root.resolve(strict=True)
        if not canonical_root.is_dir():
            raise OSError("repository root is not a directory")
    except (OSError, RuntimeError):
        return _terminal(ActivationOutcome.GUIDANCE_ONLY, "repository_unsafe")

    tsconfig_state = _root_file_state(canonical_root, "tsconfig.json")
    if tsconfig_state not in {"missing", "safe"}:
        return _terminal(
            ActivationOutcome.GUIDANCE_ONLY,
            "typescript_configuration_unsafe",
        )
    entry_limit = _evidence_entry_limit(cfg.max_files)
    if entry_limit is None:
        return _terminal(ActivationOutcome.GUIDANCE_ONLY, "invalid_file_limit")
    evidence = (
        _EvidenceScanOutcome.FOUND
        if tsconfig_state == "safe"
        else _scan_typescript_evidence(canonical_root, cfg, entry_limit)
    )
    if evidence is _EvidenceScanOutcome.LIMITED:
        return _terminal(ActivationOutcome.GUIDANCE_ONLY, "evidence_scan_limited")
    if evidence is _EvidenceScanOutcome.UNSAFE:
        return _terminal(ActivationOutcome.GUIDANCE_ONLY, "evidence_collection_failed")
    if evidence is _EvidenceScanOutcome.ABSENT:
        return _terminal(ActivationOutcome.NOT_APPLICABLE, "no_typescript_evidence")

    if local_typescript_available:
        return _terminal(
            ActivationOutcome.ALREADY_AVAILABLE,
            "local_typescript_available",
        )

    manifest_name = "package.json"
    manifest_state = _root_file_state(
        canonical_root,
        manifest_name,
        maximum_size=MAX_CONTROL_FILE_BYTES,
    )
    if manifest_state == "missing":
        return _terminal(ActivationOutcome.GUIDANCE_ONLY, "manifest_missing")
    if manifest_state == "oversized":
        return _terminal(
            ActivationOutcome.GUIDANCE_ONLY,
            "manifest_invalid",
            manifest=manifest_name,
        )
    if manifest_state != "safe":
        return _terminal(ActivationOutcome.GUIDANCE_ONLY, "manifest_unsafe")

    stable_manifest = _read_stable_control_file(canonical_root, manifest_name)
    if stable_manifest is None:
        return _terminal(ActivationOutcome.GUIDANCE_ONLY, "manifest_unsafe")
    manifest_bytes, manifest_snapshot = stable_manifest
    manifest_data = _parse_manifest(manifest_bytes)
    if manifest_data is None:
        return _terminal(
            ActivationOutcome.GUIDANCE_ONLY,
            "manifest_invalid",
            manifest=manifest_name,
        )

    present_lockfiles: list[tuple[str, Manager]] = []
    for lockfile_name, lockfile_manager in _SUPPORTED_LOCKFILES:
        state = _root_file_state(
            canonical_root,
            lockfile_name,
            maximum_size=MAX_CONTROL_FILE_BYTES,
        )
        if state == "missing":
            continue
        if state != "safe":
            return _terminal(
                ActivationOutcome.GUIDANCE_ONLY,
                "lockfile_unsafe",
                manifest=manifest_name,
            )
        present_lockfiles.append((lockfile_name, lockfile_manager))

    if not present_lockfiles:
        return _terminal(
            ActivationOutcome.GUIDANCE_ONLY,
            "lockfile_missing",
            manifest=manifest_name,
        )
    if len(present_lockfiles) != 1:
        return _terminal(
            ActivationOutcome.GUIDANCE_ONLY,
            "lockfile_ambiguous",
            manifest=manifest_name,
        )

    lockfile_name, manager = present_lockfiles[0]
    stable_lockfile = _read_stable_control_file(canonical_root, lockfile_name)
    if stable_lockfile is None:
        return _terminal(
            ActivationOutcome.GUIDANCE_ONLY,
            "lockfile_unsafe",
            manager=manager,
            manifest=manifest_name,
            lockfile=lockfile_name,
        )
    lockfile_bytes, lockfile_snapshot = stable_lockfile

    if "packageManager" in manifest_data:
        package_manager = manifest_data["packageManager"]
        if not isinstance(package_manager, str):
            return _terminal(
                ActivationOutcome.GUIDANCE_ONLY,
                "package_manager_invalid",
                manager=manager,
                manifest=manifest_name,
                lockfile=lockfile_name,
            )
        match = _PACKAGE_MANAGER_RE.fullmatch(package_manager)
        if match is None:
            return _terminal(
                ActivationOutcome.GUIDANCE_ONLY,
                "package_manager_invalid",
                manager=manager,
                manifest=manifest_name,
                lockfile=lockfile_name,
            )
        if Manager(match.group(1)) is not manager:
            return _terminal(
                ActivationOutcome.GUIDANCE_ONLY,
                "package_manager_conflict",
                manager=manager,
                manifest=manifest_name,
                lockfile=lockfile_name,
            )

    adapter = adapter_for(manager)
    if any(
        _path_is_lexically_present(canonical_root / path)
        for path in adapter.unsafe_root_files
    ):
        return _terminal(
            ActivationOutcome.GUIDANCE_ONLY,
            "manager_configuration_unsafe",
            manager=manager,
            manifest=manifest_name,
            lockfile=lockfile_name,
        )

    if not control_files_use_trusted_sources(manifest_bytes, lockfile_bytes):
        return _terminal(
            ActivationOutcome.GUIDANCE_ONLY,
            "dependency_source_unsafe",
            manager=manager,
            manifest=manifest_name,
            lockfile=lockfile_name,
        )

    if not adapter.automatic:
        return _terminal(
            ActivationOutcome.GUIDANCE_ONLY,
            "manager_guidance_only",
            manager=manager,
            manifest=manifest_name,
            lockfile=lockfile_name,
        )

    return ActivationDetection(
        None,
        manager,
        manifest_name,
        lockfile_name,
        manifest_snapshot,
        lockfile_snapshot,
    )


def revalidate_activation_detection(
    root: Path,
    cfg: Config,
    expected: ActivationDetection,
) -> bool:
    """Recheck ephemeral detection immediately before any repository mutation."""
    try:
        current = detect_activation(
            root,
            cfg,
            local_typescript_available=False,
        )
        return expected.result is None and current.result is None and current == expected
    except Exception:
        return False
