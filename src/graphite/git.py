"""Hardened Git process execution for Graphite."""
from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_GIT_STDOUT_MAX_BYTES = 16 * 1024 * 1024
_MINIMUM_GIT_VERSION = (2, 38, 0)
_GIT_VERSION_STDOUT_MAX_BYTES = 256
_READ_CHUNK_BYTES = 64 * 1024
_PROCESS_CLEANUP_TIMEOUT_SECONDS = 0.1

#: Sanitized operator-facing message per version-check failure `reason`.
#:
#: Every value is a module constant and none is derived from Git's output, so
#: looking a message up here can never leak a checkout path -- which is what
#: lets `review` surface an accurate cause without abandoning the sanitising
#: boundary it deliberately keeps between Git's text and a user's terminal.
#:
#: The table exists because there was only ever ONE message for three unrelated
#: conditions, and it asserted a version requirement in the two cases where no
#: version had been read. An operator acting on "Git 2.38 or newer is required"
#: after a probe timeout goes and upgrades a working Git.
_GIT_VERSION_FAILURE_MESSAGES = {
    "too_old": "Git 2.38 or newer is required",
    "unreadable": "Git version output could not be read",
    "probe_GitTimeoutError": "Git version check timed out",
    "probe_GitLaunchError": "Git version check could not be launched",
    "probe_GitOutputLimitError": "Git version check output limit exceeded",
}
#: For a reason this table does not know. Deliberately admits ignorance rather
#: than falling back to the version sentence, which is the failure being fixed:
#: a new probe failure must not silently inherit "upgrade your Git".
_GIT_VERSION_FAILURE_FALLBACK = "Git version could not be verified"


def git_version_failure_message(reason: str | None) -> str:
    """Map a version-check `reason` to its sanitized operator-facing message."""
    return _GIT_VERSION_FAILURE_MESSAGES.get(reason, _GIT_VERSION_FAILURE_FALLBACK)


@dataclass(frozen=True)
class GitResult:
    """Bounded output from one Git process."""

    returncode: int
    stdout: bytes


class GitError(RuntimeError):
    """Base class for sanitized Git execution failures.

    The MESSAGE is a path-free constant, deliberately: Git's own text routinely
    embeds a checkout path, which is why every raise site here is re-raised
    `from None` upstream. That sanitising works, and it also left `git_unavailable`
    carrying one bit of information -- the class name -- for faults with entirely
    different fixes.

    `diagnostic()` is the seam. It carries the step that failed and the OS error
    number beside the message rather than inside it, so `str(exc)` stays exactly
    the constant its tests assert. The vocabulary is fixed tokens and integers;
    an `OSError`'s `.filename` is a path and must never reach it.
    """

    reason: str | None = None
    os_error: int | None = None

    def diagnostic(self) -> str:
        """Path-free identity: which class, which step, which errno."""
        parts = [type(self).__name__]
        if self.reason:
            parts.append(self.reason)
        if self.os_error is not None:
            parts.append(f"os={self.os_error}")
        return ":".join(parts)


class _StepGitError(GitError):
    """A `GitError` that knows which step raised it and what the OS said."""

    def __init__(
        self, message: str, *, reason: str | None = None, os_error: int | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
        # `None` stays `None`. Four launch sites hold no `OSError` at all, and an
        # `os=None` would read as an errno that simply is not one.
        self.os_error = os_error


class GitUnavailableError(GitError):
    """Raised when no trusted external Git executable is available."""


class GitTimeoutError(GitError):
    """Raised when a Git command exceeds its deadline."""


class GitLaunchError(_StepGitError):
    """Raised when the trusted Git process cannot be launched.

    Seven distinct sites raise this inside `_run_bounded` alone -- root
    resolution, spawn, wait, a missing stdout pipe, a reader thread that would
    not join, a read that failed, and a close that failed. They are the reason
    graphite#37's single sighting could not be taken past the bucket name, so
    each one passes its own `reason`.
    """


class GitOutputLimitError(GitError):
    """Raised when Git stdout exceeds the configured bound."""


class GitUnsupportedVersionError(_StepGitError):
    """Raised when Git's protected command configuration is unavailable.

    Three unrelated conditions land here, and the failure this class is NAMED
    for is the rarest of them. Git really being older than 2.38 is a permanent,
    obvious, one-machine fact. The other two are transient: the `--version`
    probe timed out, or it could not be launched at all. Both then widen into
    `git_unavailable` upstream, which is how graphite#37 got a CI log saying an
    installed, working Git was unavailable.

    The probe runs under the budget the caller gave its command. It used to
    have a fixed two seconds of its own -- the one number in this path tuned to
    a quiet machine, and #37's leading mechanism on a loaded Windows runner.
    `_version` is still cached per RUNNER -- `collect_diff_evidence`,
    `create_task_worktree` and `accept` each build their own -- so a single
    routing operation pays the probe several times over. `reason` is what lets
    a sighting say which of the three conditions it was instead of blaming the
    Git version.
    """


class GitRunner:
    """Run Git from one canonical external executable in an isolated environment."""

    def __init__(self, project_root: Path, *, platform_name: str | None = None) -> None:
        try:
            self.root = project_root.resolve()
        except OSError as exc:
            raise GitLaunchError(
                "unable to run Git command", reason="resolve", os_error=exc.errno
            ) from exc
        self.executable = _resolve_git_executable(
            self.root, platform_name=platform_name
        )
        self._environment = _isolated_environment()
        self._version: tuple[int, int, int] | None = None
        self._version_lock = threading.Lock()

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
        self._ensure_supported_version(timeout_seconds)
        command = [
            str(self.executable),
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"safe.directory={self.root}",
            *arguments,
        ]
        return self._run_bounded(
            command,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
        )

    def _ensure_supported_version(self, timeout_seconds: float) -> None:
        if self._version is not None:
            return
        with self._version_lock:
            if self._version is not None:
                return
            try:
                # The probe runs under the caller's own budget, not a fixed one
                # of its own. `--version` is Git's cheapest command, so a budget
                # the caller's real command can survive is one the probe can
                # survive -- a derivation, where the old two seconds was a
                # number tuned to a quiet machine (graphite#37). The two budgets
                # stay separate on purpose: sharing one deadline would make the
                # command's budget vary with probe latency.
                result = self._run_bounded(
                    [str(self.executable), "--version"],
                    timeout_seconds=timeout_seconds,
                    max_stdout_bytes=_GIT_VERSION_STDOUT_MAX_BYTES,
                )
            except (GitTimeoutError, GitLaunchError, GitOutputLimitError) as exc:
                # The probe never returned a version, so nothing here is
                # evidence about Git's version -- carry what actually happened.
                # `os_error` rides along when the inner failure had one; the
                # class name alone cannot separate "spawn outlived its budget"
                # from "spawn was refused".
                probe_reason = f"probe_{type(exc).__name__}"
                raise GitUnsupportedVersionError(
                    git_version_failure_message(probe_reason),
                    reason=probe_reason,
                    os_error=getattr(exc, "os_error", None),
                ) from exc
            version = _parse_git_version(result.stdout) if result.returncode == 0 else None
            if version is None:
                # Ran, and said something this parser does not recognise. Not the
                # same finding as a version that parsed and was too low.
                raise GitUnsupportedVersionError(
                    git_version_failure_message("unreadable"), reason="unreadable"
                )
            if version < _MINIMUM_GIT_VERSION:
                raise GitUnsupportedVersionError(
                    git_version_failure_message("too_old"), reason="too_old"
                )
            self._version = version

    def _run_bounded(
        self,
        command: list[str],
        *,
        timeout_seconds: float,
        max_stdout_bytes: int,
    ) -> GitResult:
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
            raise GitLaunchError(
                "unable to run Git command", reason="popen", os_error=exc.errno
            ) from exc

        if process.stdout is None:
            _bounded_cleanup(process)
            raise GitLaunchError("unable to run Git command", reason="no_stdout")

        stdout = bytearray()
        overflow = threading.Event()
        read_failed = threading.Event()
        # The reader runs on its own thread, so its exception cannot propagate --
        # it can only leave a trace here. An `Event` alone records THAT the read
        # failed and never why, which is how a bounded read fault and a pipe torn
        # down by the peer became the same log line. One slot, written once,
        # before the flag any waiter keys on.
        read_error: list[int | None] = []

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
            except OSError as exc:
                read_error.append(exc.errno)
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
            raise GitLaunchError(
                "unable to run Git command", reason="wait", os_error=exc.errno
            ) from exc

        reader.join(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
        if reader.is_alive():
            _bounded_cleanup(process, reader)
            raise GitLaunchError("unable to run Git command", reason="reader_hang")
        if overflow.is_set():
            _bounded_cleanup(process, reader)
            raise GitOutputLimitError("Git command output limit exceeded")
        if read_failed.is_set():
            _close_stdout(process)
            raise GitLaunchError(
                "unable to run Git command",
                reason="read",
                os_error=read_error[0] if read_error else None,
            )
        if not _close_stdout(process):
            raise GitLaunchError("unable to run Git command", reason="close")
        return GitResult(returncode=returncode, stdout=bytes(stdout))


def _parse_git_version(output: bytes) -> tuple[int, int, int] | None:
    match = re.fullmatch(
        rb"git version ([0-9]+)\.([0-9]+)(?:\.([0-9]+))?"
        rb"(?:\.windows\.[0-9]+| \(Apple Git-[0-9A-Za-z][0-9A-Za-z.-]*\))?"
        rb"\r?\n?",
        output,
    )
    if match is None:
        return None
    try:
        return (
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3) or b"0"),
        )
    except ValueError:
        return None


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
