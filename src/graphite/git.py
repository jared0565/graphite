"""Hardened Git process execution for Graphite."""
from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_GIT_STDOUT_MAX_BYTES = 16 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_PROCESS_CLEANUP_TIMEOUT_SECONDS = 0.1


@dataclass(frozen=True)
class GitResult:
    """Bounded output from one Git process."""

    returncode: int
    stdout: bytes


class GitError(RuntimeError):
    """Base class for sanitized Git execution failures."""


class GitUnavailableError(GitError):
    """Raised when no trusted external Git executable is available."""


class GitTimeoutError(GitError):
    """Raised when a Git command exceeds its deadline."""


class GitLaunchError(GitError):
    """Raised when the trusted Git process cannot be launched."""


class GitOutputLimitError(GitError):
    """Raised when Git stdout exceeds the configured bound."""


class GitRunner:
    """Run Git from one canonical external executable in an isolated environment."""

    def __init__(self, project_root: Path, *, platform_name: str | None = None) -> None:
        try:
            self.root = project_root.resolve()
        except OSError as exc:
            raise GitLaunchError("unable to run Git command") from exc
        self.executable = _resolve_git_executable(
            self.root, platform_name=platform_name
        )
        self._environment = _isolated_environment()

    def run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        max_stdout_bytes: int = DEFAULT_GIT_STDOUT_MAX_BYTES,
    ) -> GitResult:
        """Run one bounded Git command with fixed process security controls."""
        if max_stdout_bytes <= 0:
            raise GitOutputLimitError("Git command output limit exceeded")
        command = [
            str(self.executable),
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            *arguments,
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                env=self._environment,
            )
        except FileNotFoundError as exc:
            raise GitUnavailableError("Git executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitTimeoutError("Git command timeout") from exc
        except OSError as exc:
            raise GitLaunchError("unable to run Git command") from exc

        if process.stdout is None:
            _bounded_cleanup(process)
            raise GitLaunchError("unable to run Git command")

        stdout = bytearray()
        overflow = threading.Event()
        read_failed = threading.Event()

        def read_stdout() -> None:
            try:
                while True:
                    chunk = process.stdout.read(_READ_CHUNK_BYTES)
                    if not chunk:
                        return
                    remaining = max_stdout_bytes - len(stdout)
                    if len(chunk) > remaining:
                        stdout.extend(chunk[:remaining])
                        overflow.set()
                        try:
                            process.kill()
                        except OSError:
                            pass
                        return
                    stdout.extend(chunk)
            except OSError:
                read_failed.set()

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _bounded_cleanup(process, reader)
            raise GitTimeoutError("Git command timeout") from exc
        except OSError as exc:
            _bounded_cleanup(process, reader)
            raise GitLaunchError("unable to run Git command") from exc

        reader.join(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
        if reader.is_alive():
            _bounded_cleanup(process, reader)
            raise GitLaunchError("unable to run Git command")
        if overflow.is_set():
            _bounded_cleanup(process, reader)
            raise GitOutputLimitError("Git command output limit exceeded")
        if read_failed.is_set():
            _close_stdout(process)
            raise GitLaunchError("unable to run Git command")
        if not _close_stdout(process):
            raise GitLaunchError("unable to run Git command")
        return GitResult(returncode=returncode, stdout=bytes(stdout))


def _bounded_cleanup(
    process: subprocess.Popen[bytes], reader: threading.Thread | None = None
) -> bool:
    """Best-effort cleanup that never waits beyond the fixed cleanup deadline."""
    try:
        process.kill()
    except OSError:
        pass
    wait_confirmed = True
    try:
        process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        wait_confirmed = False
    reader_stopped = True
    if reader is not None:
        reader.join(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
        reader_stopped = not reader.is_alive()
    if not reader_stopped:
        return False
    return wait_confirmed and _close_stdout(process)


def _close_stdout(process: subprocess.Popen[bytes]) -> bool:
    if process.stdout is None:
        return True
    try:
        process.stdout.close()
    except OSError:
        return False
    return True


def _resolve_git_executable(
    resolved_project_root: Path, *, platform_name: str | None = None
) -> Path:
    selected_platform = os.name if platform_name is None else platform_name
    executable_name = "git.exe" if selected_platform == "nt" else "git"

    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(raw_directory)
        if not directory.is_absolute():
            continue
        candidate = directory / executable_name
        try:
            if not candidate.is_file():
                continue
            resolved_candidate = candidate.resolve(strict=True)
            if not resolved_candidate.is_file():
                continue
            if selected_platform != "nt" and not os.access(resolved_candidate, os.X_OK):
                continue
            try:
                resolved_candidate.relative_to(resolved_project_root)
            except ValueError:
                pass
            else:
                continue
        except OSError:
            continue
        return resolved_candidate

    raise GitUnavailableError("Git executable was not found")


def _isolated_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.casefold().startswith("git_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment
