"""Provider-neutral, bounded subprocess policy for authenticated development CLIs."""
from __future__ import annotations

import math
import hashlib
import os
import select
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from graphite.probe_process import (
    ProbeProcessError,
    ProbeProcessResult,
    run_bounded_process,
)

from .contracts import ProviderId

MAX_ARG_COUNT: Final = 128
MAX_ARG_LENGTH: Final = 8_192
MAX_CLI_INPUT_BYTES: Final = 4 * 1024 * 1024
MAX_CLI_OUTPUT_BYTES: Final = 4 * 1024 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_OS_ENVIRONMENT = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
)
_ERROR_MAP: Final = {
    "timeout": "timeout",
    "cancelled": "cancelled",
    "output_limit": "response_limit",
    "nonzero": "process_nonzero",
    "launch_failed": "process_launch_failed",
    "cleanup_failed": "process_containment_failed",
    "input_limit": "request_limit",
    "input_failed": "process_io_failed",
    "io_failed": "process_io_failed",
    "invalid_timeout": "request_invalid",
    "invalid_environment": "environment_invalid",
}


class CliProcessError(RuntimeError):
    """Stable, path-free CLI transport failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CliProcessResult:
    returncode: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    duration_seconds: float
    input_sha256: str
    stdout_sha256: str
    stderr_sha256: str


class ProcessRunner(Protocol):
    def __call__(self, argv: list[str], **kwargs: object) -> ProbeProcessResult: ...


def require_process_containment() -> None:
    """Fail before authority consumption when native descendant containment is absent."""
    if os.name == "nt":
        try:
            from graphite.windows_job import launch
        except (ImportError, OSError):
            raise CliProcessError("process_containment_unavailable") from None
        if not callable(launch):
            raise CliProcessError("process_containment_unavailable")
        return
    waitid_available = callable(getattr(os, "waitid", None))
    kqueue_available = sys.platform == "darwin" and hasattr(select, "kqueue")
    if not waitid_available and not kqueue_available:
        raise CliProcessError("process_containment_unavailable")


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _canonical_directory(path: Path, code: str) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CliProcessError(code) from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise CliProcessError(code)
    return resolved


def _canonical_executable(path: Path, workspace: Path) -> Path:
    if not path.is_absolute():
        raise CliProcessError("executable_invalid")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CliProcessError("executable_invalid") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise CliProcessError("executable_invalid")
    try:
        resolved.relative_to(workspace)
    except ValueError:
        return resolved
    raise CliProcessError("executable_invalid")


def build_cli_environment(
    *,
    provider: ProviderId | str,
    executable: Path,
    workspace: Path,
    credential_home: Path | None,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the complete child environment without copying ambient credentials."""
    try:
        normalized_provider = ProviderId(provider)
    except (TypeError, ValueError) as exc:
        raise CliProcessError("provider_invalid") from exc
    canonical_workspace = _canonical_directory(workspace, "workspace_invalid")
    canonical_executable = _canonical_executable(executable, canonical_workspace)
    ambient = dict(os.environ)
    if source is not None:
        for name in _OS_ENVIRONMENT:
            if name in source:
                ambient[name] = source[name]
            elif name in ambient and name in {"TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}:
                ambient.pop(name)
    environment = {
        name: ambient[name]
        for name in _OS_ENVIRONMENT
        if name in ambient and isinstance(ambient[name], str) and "\x00" not in ambient[name]
    }
    trusted_paths = [canonical_executable.parent]
    system_root = environment.get("SYSTEMROOT") or environment.get("WINDIR")
    if os.name == "nt" and system_root:
        trusted_paths.append(Path(system_root) / "System32")
    elif os.name != "nt":
        trusted_paths.extend((Path("/usr/bin"), Path("/bin")))
    environment["PATH"] = os.pathsep.join(dict.fromkeys(str(path) for path in trusted_paths))
    environment.update(
        {
            "NO_COLOR": "1",
            "TERM": "dumb",
            "CI": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    if credential_home is not None:
        canonical_credentials = _canonical_directory(credential_home, "credential_home_invalid")
        try:
            canonical_credentials.relative_to(canonical_workspace)
        except ValueError:
            pass
        else:
            raise CliProcessError("credential_home_invalid")
        key = "CODEX_HOME" if normalized_provider is ProviderId.CODEX else "CLAUDE_CONFIG_DIR"
        environment[key] = str(canonical_credentials)
    return environment


def _validate_argv(argv: tuple[str, ...], cwd: Path) -> tuple[list[str], Path]:
    if not isinstance(argv, tuple) or not argv or len(argv) > MAX_ARG_COUNT:
        raise CliProcessError("argv_invalid")
    if any(
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value) > MAX_ARG_LENGTH
        for value in argv
    ):
        raise CliProcessError("argv_invalid")
    workspace = _canonical_directory(cwd, "workspace_invalid")
    executable = _canonical_executable(Path(argv[0]), workspace)
    return [str(executable), *argv[1:]], workspace


def run_cli_process(
    *,
    argv: tuple[str, ...],
    cwd: Path,
    stdin: bytes,
    provider: ProviderId | str,
    credential_home: Path | None,
    timeout_seconds: float,
    max_input_bytes: int = MAX_CLI_INPUT_BYTES,
    max_output_bytes: int = MAX_CLI_OUTPUT_BYTES,
    cancelled: Callable[[], bool] | None = None,
    runner: ProcessRunner = run_bounded_process,
    source_environment: Mapping[str, str] | None = None,
) -> CliProcessResult:
    """Run exactly one fixed CLI command inside the shared contained transport."""
    command, workspace = _validate_argv(argv, cwd)
    if (
        not isinstance(stdin, bytes)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or isinstance(max_input_bytes, bool)
        or not isinstance(max_input_bytes, int)
        or not 1 <= max_input_bytes <= MAX_CLI_INPUT_BYTES
        or isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or not 1 <= max_output_bytes <= MAX_CLI_OUTPUT_BYTES
    ):
        raise CliProcessError("request_invalid")
    cancellation = cancelled or (lambda: False)
    try:
        if cancellation():
            raise CliProcessError("cancelled")
    except CliProcessError:
        raise
    except Exception:
        raise CliProcessError("cancelled") from None
    require_process_containment()
    environment = build_cli_environment(
        provider=provider,
        executable=Path(command[0]),
        workspace=workspace,
        credential_home=credential_home,
        source=source_environment,
    )
    try:
        result = runner(
            command,
            cwd=workspace,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            max_input_bytes=max_input_bytes,
            check=True,
            environment=environment,
            cancelled=cancellation,
        )
    except ProbeProcessError as exc:
        raise CliProcessError(_ERROR_MAP.get(exc.code, "process_failed")) from None
    except Exception:
        raise CliProcessError("process_failed") from None
    if not isinstance(result, ProbeProcessResult) or result.returncode != 0:
        raise CliProcessError("process_failed")
    return CliProcessResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_seconds=result.duration_seconds,
        input_sha256=hashlib.sha256(stdin).hexdigest(),
        stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),
        stderr_sha256=hashlib.sha256(result.stderr).hexdigest(),
    )


def decode_cli_output(value: bytes) -> str:
    """Decode one bounded provider output without reflecting malformed bytes."""
    if not isinstance(value, bytes):
        raise CliProcessError("provider_protocol")
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError:
        raise CliProcessError("provider_protocol") from None
    if "\x00" in decoded:
        raise CliProcessError("provider_protocol")
    return decoded
