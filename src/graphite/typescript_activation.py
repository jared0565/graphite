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
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ._cleanup_worker import _windows_directory_identity
from .config import Config
from .dependency_install import (
    ACTIVATION_MAX_FILES,
    MAX_CONTROL_FILE_BYTES,
    FileSnapshot,
    Manager,
    Runner,
    StepResult,
    TRUSTED_REGISTRY,
    TrustedFile,
    _trusted_identity_launcher,
    adapter_for,
    command_for,
    control_files_use_trusted_sources,
    probe_local_typescript,
    revalidate_trusted_file,
    resolve_trusted_executable,
    resolve_trusted_file,
    resolve_windows_npm_prefix,
    run_install,
    run_manager_version,
    run_validator,
    snapshot_control_file,
)
from .probe_process import (
    ProbeProcessError,
    run_bounded_process,
    sanitized_probe_environment,
)
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


@dataclass(frozen=True)
class _TemporaryDirectoryLease:
    name: str
    identity: tuple[int, int]

    def __fspath__(self) -> str:
        return self.name


# Fixed worker protocol disposition; mirrored by
# ``_cleanup_worker.EXIT_POSIX_ROOT_RETAINED`` without importing package code
# into the isolated worker process.
_POSIX_CLEANUP_ROOT_RETAINED_EXIT = 75


def _new_temporary_directory() -> _TemporaryDirectoryLease:
    temporary_parent = Path(tempfile.gettempdir()).resolve(strict=True)
    path = Path(
        tempfile.mkdtemp(prefix="graphite-typescript-", dir=temporary_parent)
    ).absolute()
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode) or _is_reparse(details):
        raise OSError("temporary_directory_invalid")
    return _TemporaryDirectoryLease(
        str(path), _temporary_directory_identity(path, details)
    )


def _temporary_directory_identity(
    path: Path, details: os.stat_result | None = None
) -> tuple[int, int]:
    if os.name == "nt":
        return _windows_directory_identity(path)
    selected = details if details is not None else path.lstat()
    return selected.st_dev, selected.st_ino


def _rename_no_replace(source: Path, destination: Path) -> bool:
    """Atomically rename one path without clobbering an existing recovery path."""
    try:
        if os.name == "nt":
            os.rename(source, destination)
            return True
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        source_bytes = os.fsencode(source)
        destination_bytes = os.fsencode(destination)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            return renameat2(-100, source_bytes, -100, destination_bytes, 1) == 0
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is not None:
            renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            renamex_np.restype = ctypes.c_int
            return renamex_np(source_bytes, destination_bytes, 4) == 0
    except (OSError, TypeError, ValueError):
        return False
    return False


def _trusted_running_interpreter(root: Path) -> TrustedFile | None:
    """Pin the running interpreter, preferring the name it was invoked as.

    A POSIX virtual environment's ``bin/python`` is a symlink, and Python
    locates a virtual environment from the executable it was *invoked as* -- so
    canonicalising that name first launches the base installation under a
    different ``sys.prefix``, which is not the runtime that decided to run this
    worker. Validate the symlink route instead, and keep the given name.

    Three steps, narrowing deliberately:

    1. The full symlink route, path-trusted end to end. Best evidence, so first.
    2. Failing that, the target's identity and content, keeping the name -- see
       `_trusted_identity_launcher`. A hosted CI runner ships its whole Python
       toolchain mode 0777, so step 1 refuses all seven components there while
       `_trusted_file` accepts the very same binary; without this step that gap
       silently became "launch a different interpreter".
    3. Only then the canonical target, which drops the name. Reached when the
       launcher is one the selected repository controls -- a virtual environment
       inside it, say -- where refusing the name is the entire point.

    A previous version of this docstring claimed the canonical fallback was "no
    weaker than what this did". That was wrong, and measuring is what showed it:
    `_trusted_posix_launcher` enforces path trust while `_trusted_file` enforces
    identity and content, so falling from the first to the second is a change of
    threat model, not a narrowing of one. Step 2 exists so that trade is made
    once, on purpose, for the interpreter already executing this process.
    """
    executable = Path(sys.executable)
    reference = resolve_trusted_file(executable, root, executable=True, follow_launcher=True)
    if reference is not None:
        return reference
    reference = _trusted_identity_launcher(executable, root)
    if reference is not None:
        return reference
    try:
        canonical = executable.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolve_trusted_file(canonical, root, executable=True)


def _cleanup_isolated_home(
    lease: _TemporaryDirectoryLease,
    root: Path,
    timeout: float,
) -> StepResult:
    if not math.isfinite(timeout) or timeout <= 0:
        return StepResult(False, "cleanup_timeout")
    validated_lease = _validated_isolated_lease(lease, root)
    if validated_lease is None:
        return StepResult(False, "cleanup_failed")
    isolated_home = Path(validated_lease.name)
    cleanup_target = isolated_home.with_name(
        f".{isolated_home.name}.graphite-cleanup-recovery-"
        f"{lease.identity[0]:x}-{lease.identity[1]:x}"
    )
    if not _rename_no_replace(isolated_home, cleanup_target):
        return StepResult(False, "cleanup_failed")

    def fail_after_quarantine(reason: str = "cleanup_failed") -> StepResult:
        if _path_is_lexically_present(cleanup_target):
            _rename_no_replace(cleanup_target, isolated_home)
        return StepResult(False, reason)

    try:
        details = cleanup_target.lstat()
        if (
            _temporary_directory_identity(cleanup_target, details) != lease.identity
            or not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or _is_reparse(details)
            or cleanup_target.resolve(strict=True) != cleanup_target
        ):
            return fail_after_quarantine()
    except (OSError, RuntimeError, ValueError):
        return fail_after_quarantine()
    worker = Path(__file__).with_name("_cleanup_worker.py")
    worker_reference = resolve_trusted_file(worker, root, executable=False)
    python_reference = _trusted_running_interpreter(root)
    if worker_reference is None or python_reference is None:
        return fail_after_quarantine()
    if not revalidate_trusted_file(
        worker_reference, root, executable=False
    ) or not revalidate_trusted_file(python_reference, root, executable=True):
        return fail_after_quarantine()
    try:
        result = run_bounded_process(
            [
                str(python_reference.command_path),
                "-I",
                str(worker_reference.path),
                str(cleanup_target),
                str(lease.identity[0]),
                str(lease.identity[1]),
            ],
            cwd=cleanup_target.parent,
            stdin=None,
            timeout_seconds=timeout,
            max_output_bytes=4096,
            check=False,
            environment=sanitized_probe_environment(),
        )
    except ProbeProcessError as error:
        reason = "cleanup_timeout" if error.code == "timeout" else "cleanup_failed"
        return fail_after_quarantine(reason)
    except Exception:
        return fail_after_quarantine()
    # POSIX has no primitive that unlinks an open directory by descriptor.
    # Exit 75 means the trusted worker emptied the held Graphite-created lease
    # but intentionally retained its root name, which we leave untouched for
    # OS temporary-directory reclamation. In particular, do not inspect and
    # then remove that name: a same-UID actor could have replaced it meanwhile.
    if (
        os.name != "nt"
        and result.returncode == _POSIX_CLEANUP_ROOT_RETAINED_EXIT
    ):
        return StepResult(True, "cleanup_root_retained")
    if result.returncode != 0 or _path_is_lexically_present(cleanup_target):
        return fail_after_quarantine()
    return StepResult(True, "cleaned")


@dataclass
class ActivationDependencies:
    prompt: Callable[[str], str] = input
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    monotonic: Callable[[], float] = time.monotonic
    runner: Runner = run_bounded_process
    temporary_directory: Callable[[], Any] = _new_temporary_directory
    cleanup: Callable[[_TemporaryDirectoryLease, Path, float], StepResult] = (
        _cleanup_isolated_home
    )


_ACTIVATION_LOCKS_GUARD = threading.Lock()
_ACTIVATION_LOCKS: dict[str, tuple[threading.Lock, int]] = {}
_ACTIVE_CLEANUP_ROOTS: set[str] = set()
_CLEANUP_MAX_SECONDS = 1.0


def _acquire_activation_lock(root: Path) -> tuple[str, threading.Lock] | None:
    key = os.path.normcase(str(root))
    with _ACTIVATION_LOCKS_GUARD:
        if key in _ACTIVE_CLEANUP_ROOTS:
            return None
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


class _DependencyFailure(Exception):
    pass


@dataclass(frozen=True)
class _Deadline:
    expires_at: float
    monotonic: Callable[[], float]

    def remaining(self) -> float:
        try:
            remaining = self.expires_at - self.monotonic()
            if not math.isfinite(remaining):
                raise ValueError("clock_invalid")
        except Exception:
            raise _DependencyFailure from None
        if remaining <= 0:
            raise _DeadlineExpired
        return remaining


def _mark_cleanup_active(root: Path) -> None:
    key = os.path.normcase(str(root))
    with _ACTIVATION_LOCKS_GUARD:
        _ACTIVE_CLEANUP_ROOTS.add(key)


def _clear_cleanup_active(root: Path) -> None:
    key = os.path.normcase(str(root))
    with _ACTIVATION_LOCKS_GUARD:
        _ACTIVE_CLEANUP_ROOTS.discard(key)


def _bounded_temporary_cleanup(
    lease: _TemporaryDirectoryLease,
    deadline: _Deadline,
    root: Path,
    cleanup: Callable[[_TemporaryDirectoryLease, Path, float], StepResult],
) -> tuple[str, bool]:
    """Run terminable cleanup while lock acquisition observes its ownership."""
    expired_before_cleanup = False
    dependency_failed = False
    try:
        remaining = deadline.remaining()
    except _DeadlineExpired:
        remaining = _CLEANUP_MAX_SECONDS
        expired_before_cleanup = True
    except _DependencyFailure:
        remaining = _CLEANUP_MAX_SECONDS
        dependency_failed = True
    cleanup_timeout = min(_CLEANUP_MAX_SECONDS, max(0.001, remaining))
    _mark_cleanup_active(root)
    try:
        cleanup_result = cleanup(lease, root, cleanup_timeout)
    except Exception:
        cleanup_result = StepResult(False, "cleanup_failed")
    finally:
        _clear_cleanup_active(root)
    if dependency_failed:
        return "dependency_failed", expired_before_cleanup
    if not isinstance(cleanup_result, StepResult):
        return "cleanup_failed", expired_before_cleanup
    if not cleanup_result.ok:
        reason = (
            cleanup_result.reason
            if cleanup_result.reason in {"cleanup_failed", "cleanup_timeout"}
            else "cleanup_failed"
        )
        return reason, expired_before_cleanup
    try:
        deadline.remaining()
    except _DeadlineExpired:
        return "cleanup_timeout", expired_before_cleanup
    except _DependencyFailure:
        return "dependency_failed", expired_before_cleanup
    return "cleaned", expired_before_cleanup


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
    node: Any,
):
    if os.name == "nt" and manager is Manager.NPM:
        return resolve_windows_npm_prefix(root, path_source)
    executable = resolve_trusted_executable(manager.value, root, path_source)
    return command_for(executable, node) if executable is not None else None


def _validated_isolated_lease(
    temporary: Any,
    root: Path,
) -> _TemporaryDirectoryLease | None:
    try:
        candidate = Path(temporary.name)
        if not candidate.is_absolute():
            return None
        lexical = candidate.absolute()
        details = lexical.lstat()
        canonical = lexical.resolve(strict=True)
        canonical_root = root.resolve(strict=True)
        identity = _temporary_directory_identity(canonical, details)
        supplied_identity = getattr(temporary, "identity", None)
        return (
            _TemporaryDirectoryLease(str(canonical), identity)
            if (supplied_identity is None or supplied_identity == identity)
            and canonical == lexical
            and canonical != canonical_root
            and not _is_contained(canonical, canonical_root)
            and not _is_contained(canonical_root, canonical)
            and stat.S_ISDIR(details.st_mode)
            and not stat.S_ISLNK(details.st_mode)
            and not _is_reparse(details)
            else None
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _inspect_post_attempt(
    root: Path,
    cfg: Config,
    detection: ActivationDetection,
    deadline: _Deadline,
) -> tuple[ActivationResult | None, tuple[str, ...]]:
    changed_names: set[str] = set()
    control_unsafe = False
    for before, relative_path in (
        (detection.manifest_snapshot, detection.manifest),
        (detection.lockfile_snapshot, detection.lockfile),
    ):
        if relative_path is None:
            continue
        try:
            after = snapshot_control_file(root, relative_path)
        except ValueError:
            control_unsafe = True
            changed_names.add(relative_path)
        else:
            if before != after:
                changed_names.add(relative_path)
    changed = tuple(sorted(changed_names))
    try:
        deadline.remaining()
    except _DeadlineExpired:
        return (
            _result_for_detection(
                detection,
                ActivationOutcome.INSTALLATION_FAILED,
                "operation_timeout",
                attempted=True,
                changed_files=changed,
            ),
            changed,
        )
    except _DependencyFailure:
        return (
            _result_for_detection(
                detection,
                ActivationOutcome.INSTALLATION_FAILED,
                "dependency_failed",
                attempted=True,
                changed_files=changed,
            ),
            changed,
        )
    try:
        current = detect_activation(root, cfg, local_typescript_available=False)
    except Exception:
        current = None
    try:
        deadline.remaining()
    except _DeadlineExpired:
        return (
            _result_for_detection(
                detection,
                ActivationOutcome.INSTALLATION_FAILED,
                "operation_timeout",
                attempted=True,
                changed_files=changed,
            ),
            changed,
        )
    except _DependencyFailure:
        return (
            _result_for_detection(
                detection,
                ActivationOutcome.INSTALLATION_FAILED,
                "dependency_failed",
                attempted=True,
                changed_files=changed,
            ),
            changed,
        )
    if control_unsafe:
        return (
            _result_for_detection(
                detection,
                ActivationOutcome.INSTALLATION_FAILED,
                "control_file_unsafe",
                attempted=True,
                changed_files=changed,
            ),
            changed,
        )
    if (
        current is None
        or current.result is not None
        or current.manager is not detection.manager
        or current.manifest != detection.manifest
        or current.lockfile != detection.lockfile
    ):
        return (
            _result_for_detection(
                detection,
                ActivationOutcome.INSTALLATION_FAILED,
                "project_state_changed",
                attempted=True,
                changed_files=changed,
            ),
            changed,
        )
    return None, changed


def _verification_result(
    root: Path,
    detection: ActivationDetection,
    node: Any,
    deadline: _Deadline,
    runner: Runner,
    changed: tuple[str, ...],
) -> ActivationResult:
    verification_timed_out = False
    try:
        verified = probe_local_typescript(root, node, deadline.remaining(), runner)
        deadline.remaining()
    except _DeadlineExpired:
        verified = False
        verification_timed_out = True
    except _DependencyFailure:
        return _result_for_detection(
            detection,
            ActivationOutcome.VERIFICATION_FAILED,
            "dependency_failed",
            attempted=True,
            changed_files=changed,
        )
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
    isolated_lease: _TemporaryDirectoryLease | None = None
    result = _result_for_detection(
        detection,
        ActivationOutcome.INSTALLATION_FAILED,
        "isolation_unavailable",
        attempted=True,
    )
    try:
        temporary = dependencies.temporary_directory()
        isolated_lease = _validated_isolated_lease(temporary, root)
        if isolated_lease is None:
            result = _result_for_detection(
                detection,
                ActivationOutcome.INSTALLATION_FAILED,
                "isolation_unavailable",
                attempted=True,
            )
        elif not revalidate_activation_detection(root, cfg, detection):
            result = _result_for_detection(
                detection,
                ActivationOutcome.INSTALLATION_FAILED,
                "project_state_changed",
                attempted=True,
            )
        else:
            if detection.manager is None:
                # detect_activation only yields a detection without a terminal
                # result once it has settled on a manager.
                raise RuntimeError("activation detection without a manager")
            isolated_home = Path(isolated_lease.name)
            install = run_install(
                root,
                command,
                adapter_for(detection.manager),
                TRUSTED_REGISTRY,
                isolated_home,
                deadline.remaining(),
                dependencies.runner,
            )
            inspection, changed = _inspect_post_attempt(
                root,
                cfg,
                detection,
                deadline,
            )
            if inspection is not None:
                result = inspection
            elif not install.ok:
                result = _result_for_detection(
                    detection,
                    ActivationOutcome.INSTALLATION_FAILED,
                    install.reason,
                    attempted=True,
                    changed_files=changed,
                )
            else:
                result = _verification_result(
                    root,
                    detection,
                    node,
                    deadline,
                    dependencies.runner,
                    changed,
                )
    except _DeadlineExpired:
        result = _result_for_detection(
            detection,
            ActivationOutcome.INSTALLATION_FAILED,
            "operation_timeout",
            attempted=True,
        )
    except _DependencyFailure:
        result = _result_for_detection(
            detection,
            ActivationOutcome.INSTALLATION_FAILED,
            "dependency_failed",
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
        if isolated_lease is not None:
            cleanup_status, expired_before_cleanup = _bounded_temporary_cleanup(
                isolated_lease,
                deadline,
                root,
                dependencies.cleanup,
            )
            if cleanup_status == "cleanup_failed":
                result = _result_for_detection(
                    detection,
                    ActivationOutcome.INSTALLATION_FAILED,
                    "isolation_cleanup_failed",
                    attempted=True,
                    changed_files=result.changed_files,
                )
            elif (
                cleanup_status == "dependency_failed"
                and result.reason != "dependency_failed"
            ):
                result = _result_for_detection(
                    detection,
                    ActivationOutcome.INSTALLATION_FAILED,
                    "dependency_failed",
                    attempted=True,
                    changed_files=result.changed_files,
                )
            elif cleanup_status == "cleanup_timeout" and (
                not expired_before_cleanup
                or result.outcome is not ActivationOutcome.VERIFICATION_FAILED
                or result.reason not in {"operation_timeout", "dependency_failed"}
            ):
                result = _result_for_detection(
                    detection,
                    ActivationOutcome.INSTALLATION_FAILED,
                    "operation_timeout",
                    attempted=True,
                    changed_files=result.changed_files,
                )
    return result


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
        try:
            path_source = deps.environ.get("PATH", "")
        except Exception:
            return ActivationResult(
                ActivationOutcome.GUIDANCE_ONLY,
                None,
                "dependency_failed",
            )
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
            if evidence.result is None:
                # detect_activation(local_typescript_available=True) always
                # returns a terminal detection.
                raise RuntimeError("activation evidence without a result")
            return evidence.result
        detection = detect_activation(
            canonical_root,
            request.cfg,
            local_typescript_available=False,
        )
        if detection.result is not None:
            return detection.result
        manager = detection.manager
        if manager is None:
            # detect_activation only yields a detection without a terminal
            # result once it has settled on a manager.
            raise RuntimeError("activation detection without a manager")
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
            manager,
            path_source,
            node,
        )
        if command is None:
            return _result_for_detection(
                detection,
                ActivationOutcome.GUIDANCE_ONLY,
                "manager_unavailable",
            )
        if os.name == "nt" and manager is Manager.NPM:
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
        if not adapter_for(manager).supports(version.version):
            return _result_for_detection(
                detection,
                ActivationOutcome.GUIDANCE_ONLY,
                "manager_version_unsupported",
            )
        question = (
            "Project-local TypeScript is missing. Install it with "
            f"{manager.value} as a development dependency? [y/N]"
        )
        try:
            answer = deps.prompt(question)
            accepted = (
                isinstance(answer, str)
                and answer.strip().lower() in {"y", "yes"}
            )
        except EOFError:
            accepted = False
        except Exception:
            return _result_for_detection(
                detection,
                ActivationOutcome.DECLINED,
                "prompt_failed",
            )
        if not accepted:
            return _result_for_detection(
                detection,
                ActivationOutcome.DECLINED,
                "user_declined",
            )
        try:
            started = deps.monotonic()
            if not isinstance(started, (int, float)) or not math.isfinite(started):
                raise ValueError("clock_invalid")
            deadline = _Deadline(started + float(timeout), deps.monotonic)
        except Exception:
            return _result_for_detection(
                detection,
                ActivationOutcome.VALIDATION_FAILED,
                "dependency_failed",
                attempted=True,
            )
        try:
            validator_value = deps.environ.get("GRAPHITE_PACKAGE_VALIDATOR")
        except Exception:
            return _result_for_detection(
                detection,
                ActivationOutcome.VALIDATION_FAILED,
                "dependency_failed",
                attempted=True,
            )
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
        except _DependencyFailure:
            return _result_for_detection(
                detection,
                ActivationOutcome.VALIDATION_FAILED,
                "dependency_failed",
                attempted=True,
            )
        if not validation.ok:
            return _result_for_detection(
                detection,
                ActivationOutcome.VALIDATION_FAILED,
                validation.reason,
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
