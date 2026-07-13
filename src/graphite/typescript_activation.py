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
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import Config
from .dependency_install import (
    ACTIVATION_MAX_FILES,
    MAX_CONTROL_FILE_BYTES,
    FileSnapshot,
    Manager,
    Runner,
    TRUSTED_REGISTRY,
    adapter_for,
    command_for,
    control_files_use_trusted_sources,
    probe_local_typescript,
    resolve_trusted_executable,
    resolve_trusted_file,
    resolve_windows_npm_prefix,
    run_install,
    run_manager_version,
    run_validator,
    snapshot_control_file,
)
from .probe_process import run_bounded_process
from .ingest import SKIP_DIRS
from .probe_workspace import (
    _close_raw_handle,
    _open_directory_handle,
    _windows_handle_final_path,
)


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


@dataclass(frozen=True)
class ActivationRequest:
    root: Path
    cfg: Config
    stdin_is_tty: bool
    stdout_is_tty: bool
    assume_yes: bool
    json_mode: bool
    timeout_seconds: float = 120.0


@dataclass
class ActivationDependencies:
    prompt: Callable[[str], str] = input
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    monotonic: Callable[[], float] = time.monotonic
    runner: Runner = run_bounded_process
    temporary_directory: Callable[[], Any] = tempfile.TemporaryDirectory


_ACTIVATION_LOCKS_GUARD = threading.Lock()
_ACTIVATION_LOCKS: dict[str, tuple[threading.Lock, int]] = {}


def _acquire_activation_lock(root: Path) -> tuple[str, threading.Lock] | None:
    key = os.path.normcase(str(root))
    with _ACTIVATION_LOCKS_GUARD:
        lock, users = _ACTIVATION_LOCKS.get(key, (threading.Lock(), 0))
        _ACTIVATION_LOCKS[key] = (lock, users + 1)
        if lock.acquire(blocking=False):
            return key, lock
        current_lock, current_users = _ACTIVATION_LOCKS[key]
        if current_users == 1:
            del _ACTIVATION_LOCKS[key]
        else:
            _ACTIVATION_LOCKS[key] = (current_lock, current_users - 1)
        return None


def _release_activation_lock(key: str, lock: threading.Lock) -> None:
    lock.release()
    with _ACTIVATION_LOCKS_GUARD:
        current_lock, users = _ACTIVATION_LOCKS[key]
        if users == 1:
            del _ACTIVATION_LOCKS[key]
        else:
            _ACTIVATION_LOCKS[key] = (current_lock, users - 1)


class _DeadlineExpired(Exception):
    pass


@dataclass(frozen=True)
class _Deadline:
    expires_at: float
    monotonic: Callable[[], float]

    def remaining(self) -> float:
        remaining = self.expires_at - self.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise _DeadlineExpired
        return remaining


_PACKAGE_MANAGER_RE = re.compile(r"(npm|pnpm|yarn|bun)@(\S+)")
_STABLE_STAT_FIELDS = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
_EVIDENCE_SCAN_SECONDS = 1.0
_EVIDENCE_MAX_DEPTH = 128
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


@dataclass(frozen=True)
class _ScanDirectory:
    path: Path
    handle: int
    identity: tuple[int, int]


@dataclass
class _EvidenceScanState:
    entry_limit: int
    deadline: float
    visited: int = 0


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


def _windows_identity(raw_identity: tuple[int, int, int]) -> tuple[int, int]:
    return raw_identity[0], (raw_identity[1] << 32) | raw_identity[2]


def _same_windows_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _open_root_scan_directory(root: Path) -> _ScanDirectory | None:
    handle = -1
    try:
        if os.name == "nt":
            handle, raw_identity = _open_directory_handle(root)
            final_path = _windows_handle_final_path(handle)
            if not _same_windows_path(final_path, root):
                raise OSError("directory_binding_changed")
            return _ScanDirectory(root, handle, _windows_identity(raw_identity))
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        handle = os.open(root, flags)
        os.set_inheritable(handle, False)
        details = os.fstat(handle)
        if not stat.S_ISDIR(details.st_mode):
            raise OSError("directory_invalid")
        return _ScanDirectory(root, handle, (details.st_dev, details.st_ino))
    except (OSError, RuntimeError, ValueError):
        if handle >= 0:
            _close_raw_handle(handle)
        return None


def _open_child_scan_directory(
    parent: _ScanDirectory,
    name: str,
    expected_identity: tuple[int, int],
    canonical_root: Path,
) -> _ScanDirectory | None:
    handle = -1
    child_path = parent.path / name
    try:
        if os.name == "nt":
            handle, raw_identity = _open_directory_handle(child_path)
            identity = _windows_identity(raw_identity)
            final_path = _windows_handle_final_path(handle)
            if (
                identity[1] != expected_identity[1]
                or not _is_contained(final_path, canonical_root)
                or not _same_windows_path(final_path, child_path)
            ):
                raise OSError("directory_binding_changed")
            return _ScanDirectory(child_path, handle, identity)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        handle = os.open(name, flags, dir_fd=parent.handle)
        os.set_inheritable(handle, False)
        details = os.fstat(handle)
        identity = (details.st_dev, details.st_ino)
        if not stat.S_ISDIR(details.st_mode) or identity != expected_identity:
            raise OSError("directory_binding_changed")
        return _ScanDirectory(child_path, handle, identity)
    except (OSError, RuntimeError, ValueError):
        if handle >= 0:
            _close_raw_handle(handle)
        return None


def _scan_open_directory(
    directory: _ScanDirectory,
    root: Path,
    cfg: Config,
    exclusions: tuple[tuple[str, ...], ...],
    state: _EvidenceScanState,
    *,
    depth: int,
) -> _EvidenceScanOutcome:
    if time.monotonic() >= state.deadline:
        return _EvidenceScanOutcome.LIMITED
    scan_target: int | Path = directory.path if os.name == "nt" else directory.handle
    try:
        with os.scandir(scan_target) as entries:
            for entry in entries:
                state.visited += 1
                if (
                    state.visited > state.entry_limit
                    or time.monotonic() >= state.deadline
                ):
                    return _EvidenceScanOutcome.LIMITED
                details = (
                    os.lstat(directory.path / entry.name)
                    if os.name == "nt"
                    else entry.stat(follow_symlinks=False)
                )
                if stat.S_ISLNK(details.st_mode) or _is_reparse(details):
                    continue
                parts = (*directory.path.relative_to(root).parts, entry.name)
                if stat.S_ISDIR(details.st_mode):
                    if (
                        entry.name in SKIP_DIRS
                        or (not cfg.include_dotfiles and entry.name.startswith("."))
                        or _is_excluded_path(parts, exclusions)
                    ):
                        continue
                    if depth >= _EVIDENCE_MAX_DEPTH:
                        return _EvidenceScanOutcome.LIMITED
                    child = _open_child_scan_directory(
                        directory,
                        entry.name,
                        (details.st_dev, details.st_ino),
                        root,
                    )
                    if child is None:
                        return _EvidenceScanOutcome.UNSAFE
                    try:
                        child_outcome = _scan_open_directory(
                            child,
                            root,
                            cfg,
                            exclusions,
                            state,
                            depth=depth + 1,
                        )
                    finally:
                        _close_raw_handle(child.handle)
                    if child_outcome is not _EvidenceScanOutcome.ABSENT:
                        return child_outcome
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


def _scan_typescript_evidence(
    root: Path,
    cfg: Config,
    entry_limit: int,
) -> _EvidenceScanOutcome:
    state = _EvidenceScanState(
        entry_limit=entry_limit,
        deadline=time.monotonic() + _EVIDENCE_SCAN_SECONDS,
    )
    exclusions = _configured_exclusions(root, cfg)
    root_directory = _open_root_scan_directory(root)
    if root_directory is None:
        return _EvidenceScanOutcome.UNSAFE
    try:
        return _scan_open_directory(
            root_directory,
            root,
            cfg,
            exclusions,
            state,
            depth=0,
        )
    finally:
        _close_raw_handle(root_directory.handle)


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


def _result_for_detection(
    detection: ActivationDetection,
    outcome: ActivationOutcome,
    reason: str,
    *,
    attempted: bool = False,
    changed_files: tuple[str, ...] = (),
) -> ActivationResult:
    return ActivationResult(
        outcome,
        detection.manager,
        reason,
        detection.manifest,
        detection.lockfile,
        changed_files,
        attempted,
    )


def _resolve_manager_command(
    root: Path,
    manager: Manager,
    path_source: str,
):
    if os.name == "nt" and manager is Manager.NPM:
        return resolve_windows_npm_prefix(root, path_source)
    executable = resolve_trusted_executable(manager.value, root, path_source)
    return command_for(executable) if executable is not None else None


def _isolated_home_path(temporary: Any, root: Path) -> Path | None:
    try:
        candidate = Path(temporary.name)
        if not candidate.is_absolute():
            return None
        lexical = candidate.absolute()
        details = lexical.lstat()
        canonical = lexical.resolve(strict=True)
        canonical_root = root.resolve(strict=True)
        return (
            canonical
            if canonical == lexical
            and canonical != canonical_root
            and not _is_contained(canonical, canonical_root)
            and stat.S_ISDIR(details.st_mode)
            and not stat.S_ISLNK(details.st_mode)
            and not _is_reparse(details)
            else None
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _postinstall_result(
    root: Path,
    cfg: Config,
    detection: ActivationDetection,
    node: Any,
    deadline: _Deadline,
    runner: Runner,
) -> ActivationResult:
    try:
        manifest_snapshot = snapshot_control_file(root, detection.manifest or "")
        lockfile_snapshot = snapshot_control_file(root, detection.lockfile or "")
    except ValueError:
        return _result_for_detection(
            detection,
            ActivationOutcome.INSTALLATION_FAILED,
            "control_file_unsafe",
            attempted=True,
        )
    changed = tuple(
        sorted(
            snapshot.relative_path
            for before, snapshot in (
                (detection.manifest_snapshot, manifest_snapshot),
                (detection.lockfile_snapshot, lockfile_snapshot),
            )
            if before != snapshot
        )
    )
    try:
        deadline.remaining()
    except _DeadlineExpired:
        return _result_for_detection(
            detection,
            ActivationOutcome.INSTALLATION_FAILED,
            "operation_timeout",
            attempted=True,
            changed_files=changed,
        )
    current = detect_activation(root, cfg, local_typescript_available=False)
    try:
        deadline.remaining()
    except _DeadlineExpired:
        return _result_for_detection(
            detection,
            ActivationOutcome.INSTALLATION_FAILED,
            "operation_timeout",
            attempted=True,
            changed_files=changed,
        )
    if (
        current.result is not None
        or current.manager is not detection.manager
        or current.manifest != detection.manifest
        or current.lockfile != detection.lockfile
    ):
        return _result_for_detection(
            detection,
            ActivationOutcome.INSTALLATION_FAILED,
            "project_state_changed",
            attempted=True,
            changed_files=changed,
        )
    verification_timed_out = False
    try:
        verified = probe_local_typescript(root, node, deadline.remaining(), runner)
        deadline.remaining()
    except _DeadlineExpired:
        verified = False
        verification_timed_out = True
    if not verified:
        return _result_for_detection(
            detection,
            ActivationOutcome.VERIFICATION_FAILED,
            "operation_timeout" if verification_timed_out else "typescript_not_available",
            attempted=True,
            changed_files=changed,
        )
    return _result_for_detection(
        detection,
        ActivationOutcome.INSTALLED,
        "installed",
        attempted=True,
        changed_files=changed,
    )


def _install_with_isolation(
    root: Path,
    cfg: Config,
    detection: ActivationDetection,
    node: Any,
    command: Any,
    deadline: _Deadline,
    dependencies: ActivationDependencies,
) -> ActivationResult:
    temporary = None
    result: ActivationResult | None = None
    try:
        temporary = dependencies.temporary_directory()
        isolated_home = _isolated_home_path(temporary, root)
        if isolated_home is None:
            return _result_for_detection(
                detection,
                ActivationOutcome.INSTALLATION_FAILED,
                "isolation_unavailable",
                attempted=True,
            )
        install = run_install(
            root,
            command,
            adapter_for(detection.manager),
            TRUSTED_REGISTRY,
            isolated_home,
            deadline.remaining(),
            dependencies.runner,
        )
        try:
            deadline.remaining()
        except _DeadlineExpired:
            return _result_for_detection(
                detection,
                ActivationOutcome.INSTALLATION_FAILED,
                "operation_timeout",
                attempted=True,
            )
        if not install.ok:
            return _result_for_detection(
                detection,
                ActivationOutcome.INSTALLATION_FAILED,
                install.reason,
                attempted=True,
            )
        result = _postinstall_result(
            root,
            cfg,
            detection,
            node,
            deadline,
            dependencies.runner,
        )
    except _DeadlineExpired:
        result = _result_for_detection(
            detection,
            ActivationOutcome.INSTALLATION_FAILED,
            "install_timeout",
            attempted=True,
        )
    except Exception:
        result = _result_for_detection(
            detection,
            ActivationOutcome.INSTALLATION_FAILED,
            "isolation_unavailable",
            attempted=True,
        )
    finally:
        if temporary is not None:
            try:
                temporary.cleanup()
            except Exception:
                result = _result_for_detection(
                    detection,
                    ActivationOutcome.INSTALLATION_FAILED,
                    "isolation_unavailable",
                    attempted=True,
                    changed_files=result.changed_files if result else (),
                )
            if result is not None and result.outcome is ActivationOutcome.INSTALLED:
                try:
                    deadline.remaining()
                except _DeadlineExpired:
                    result = _result_for_detection(
                        detection,
                        ActivationOutcome.INSTALLATION_FAILED,
                        "operation_timeout",
                        attempted=True,
                        changed_files=result.changed_files,
                    )
    return result or _result_for_detection(
        detection,
        ActivationOutcome.INSTALLATION_FAILED,
        "isolation_unavailable",
        attempted=True,
    )


def activate_typescript(
    request: ActivationRequest,
    dependencies: ActivationDependencies | None = None,
) -> ActivationResult:
    """Apply consent and execution policy to optional TypeScript activation."""
    deps = dependencies or ActivationDependencies()
    try:
        canonical_root = request.root.resolve(strict=True)
    except (OSError, RuntimeError):
        return ActivationResult(
            ActivationOutcome.GUIDANCE_ONLY,
            None,
            "repository_unsafe",
        )
    acquired = _acquire_activation_lock(canonical_root)
    if acquired is None:
        return ActivationResult(
            ActivationOutcome.GUIDANCE_ONLY,
            None,
            "activation_in_progress",
        )
    key, lock = acquired
    try:
        timeout = request.timeout_seconds
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            return ActivationResult(
                ActivationOutcome.GUIDANCE_ONLY,
                None,
                "invalid_timeout",
            )
        evidence = detect_activation(
            canonical_root,
            request.cfg,
            local_typescript_available=True,
        )
        if (
            evidence.result is not None
            and evidence.result.outcome is not ActivationOutcome.ALREADY_AVAILABLE
        ):
            return evidence.result
        path_source = deps.environ.get("PATH", "")
        if not isinstance(path_source, str):
            path_source = ""
        node = resolve_trusted_executable("node", canonical_root, path_source)
        local_available = bool(
            node is not None
            and probe_local_typescript(
                canonical_root,
                node,
                min(float(timeout), 10.0),
                deps.runner,
            )
        )
        if local_available:
            return evidence.result
        detection = detect_activation(
            canonical_root,
            request.cfg,
            local_typescript_available=False,
        )
        if detection.result is not None:
            return detection.result
        if (
            not request.stdin_is_tty
            or not request.stdout_is_tty
            or request.json_mode
            or request.assume_yes
        ):
            return _result_for_detection(
                detection,
                ActivationOutcome.GUIDANCE_ONLY,
                "non_interactive",
            )
        if node is None:
            return _result_for_detection(
                detection,
                ActivationOutcome.GUIDANCE_ONLY,
                "node_unavailable",
            )
        command = _resolve_manager_command(
            canonical_root,
            detection.manager,
            path_source,
        )
        if command is None:
            return _result_for_detection(
                detection,
                ActivationOutcome.GUIDANCE_ONLY,
                "manager_unavailable",
            )
        if os.name == "nt" and detection.manager is Manager.NPM:
            node = command.references[0]
        version = run_manager_version(
            command,
            canonical_root,
            min(float(timeout), 10.0),
            deps.runner,
        )
        if not version.ok:
            reason = {
                "manager_timeout": "manager_version_timeout",
                "manager_version_invalid": "manager_version_invalid",
            }.get(version.reason, "manager_version_unavailable")
            return _result_for_detection(
                detection,
                ActivationOutcome.GUIDANCE_ONLY,
                reason,
            )
        if not adapter_for(detection.manager).supports(version.version):
            return _result_for_detection(
                detection,
                ActivationOutcome.GUIDANCE_ONLY,
                "manager_version_unsupported",
            )
        question = (
            "Project-local TypeScript is missing. Install it with "
            f"{detection.manager.value} as a development dependency? [y/N]"
        )
        try:
            answer = deps.prompt(question)
        except EOFError:
            answer = ""
        if not isinstance(answer, str) or answer.strip().lower() not in {"y", "yes"}:
            return _result_for_detection(
                detection,
                ActivationOutcome.DECLINED,
                "user_declined",
            )
        deadline = _Deadline(deps.monotonic() + float(timeout), deps.monotonic)
        validator_value = deps.environ.get("GRAPHITE_PACKAGE_VALIDATOR")
        validator = (
            resolve_trusted_file(Path(validator_value), canonical_root, executable=False)
            if isinstance(validator_value, str) and validator_value
            else None
        )
        if validator is None:
            return _result_for_detection(
                detection,
                ActivationOutcome.VALIDATION_FAILED,
                "validator_invalid",
                attempted=True,
            )
        try:
            validation = run_validator(
                canonical_root,
                node,
                validator,
                deadline.remaining(),
                deps.runner,
            )
            deadline.remaining()
        except _DeadlineExpired:
            return _result_for_detection(
                detection,
                ActivationOutcome.VALIDATION_FAILED,
                "operation_timeout",
                attempted=True,
            )
        if not validation.ok:
            return _result_for_detection(
                detection,
                ActivationOutcome.VALIDATION_FAILED,
                validation.reason,
                attempted=True,
            )
        if not revalidate_activation_detection(canonical_root, request.cfg, detection):
            return _result_for_detection(
                detection,
                ActivationOutcome.INSTALLATION_FAILED,
                "project_state_changed",
                attempted=True,
            )
        try:
            deadline.remaining()
        except _DeadlineExpired:
            return _result_for_detection(
                detection,
                ActivationOutcome.INSTALLATION_FAILED,
                "operation_timeout",
                attempted=True,
            )
        return _install_with_isolation(
            canonical_root,
            request.cfg,
            detection,
            node,
            command,
            deadline,
            deps,
        )
    finally:
        _release_activation_lock(key, lock)
