"""Hardened Git process execution for Graphite."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence


class GitError(RuntimeError):
    """Base class for sanitized Git execution failures."""


class GitUnavailableError(GitError):
    """Raised when no trusted external Git executable is available."""


class GitTimeoutError(GitError):
    """Raised when a Git command exceeds its deadline."""


class GitLaunchError(GitError):
    """Raised when the trusted Git process cannot be launched."""


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
        self, arguments: Sequence[str], *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one bounded Git command with fixed process security controls."""
        command = [
            str(self.executable),
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            *arguments,
        ]
        try:
            return subprocess.run(
                command,
                cwd=self.root,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                shell=False,
                env=self._environment,
            )
        except FileNotFoundError as exc:
            raise GitUnavailableError("Git executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitTimeoutError("Git command timeout") from exc
        except OSError as exc:
            raise GitLaunchError("unable to run Git command") from exc


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
