"""Safe collection of explicit and Git-reported review changes."""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


class ReviewError(ValueError):
    """Raised when review change evidence cannot be collected safely."""


@dataclass(frozen=True, order=True)
class Change:
    """A project-relative changed path and its normalized status."""

    path: str
    status: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def normalize_explicit_changes(root: Path, paths: Iterable[str]) -> list[Change]:
    """Normalize user-supplied paths while containing them within *root*."""
    resolved_root = root.resolve()
    normalized: set[str] = set()

    for raw_path in paths:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = resolved_root / candidate
        resolved_path = candidate.resolve()

        try:
            relative_path = resolved_path.relative_to(resolved_root)
        except ValueError as exc:
            raise ReviewError("path is outside project root") from exc

        if relative_path == Path("."):
            raise ReviewError("a change path cannot be the project root")
        normalized.add(relative_path.as_posix())

    return [Change(path, "explicit") for path in sorted(normalized)]


def discover_git_changes(root: Path, *, timeout_seconds: float = 5.0) -> list[Change]:
    """Collect normalized changes from Git porcelain output."""
    if timeout_seconds <= 0:
        raise ReviewError("Git status timeout must be greater than zero")

    try:
        resolved_root = root.resolve()
    except OSError as exc:
        raise ReviewError("unable to resolve project path") from exc

    top_level_result = _run_git(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=resolved_root,
        timeout_seconds=timeout_seconds,
    )
    if top_level_result.returncode != 0:
        raise ReviewError("not a Git worktree")

    try:
        git_root = Path(os.fsdecode(top_level_result.stdout).rstrip("\r\n")).resolve()
    except OSError as exc:
        raise ReviewError("unable to resolve Git worktree root") from exc
    if not top_level_result.stdout or git_root != resolved_root:
        raise ReviewError("project path must be the Git worktree root")

    status_result = _run_git(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=resolved_root,
        timeout_seconds=timeout_seconds,
    )
    if status_result.returncode != 0:
        raise ReviewError("not a Git worktree")

    return _parse_porcelain(status_result.stdout)


def _run_git(
    command: list[str], *, cwd: Path, timeout_seconds: float
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise ReviewError("Git executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReviewError("Git command timeout") from exc
    except OSError as exc:
        raise ReviewError("unable to run Git command") from exc


def _parse_porcelain(output: bytes) -> list[Change]:
    if not output:
        return []
    if not output.endswith(b"\0"):
        raise ReviewError("malformed Git status output: missing NUL terminator")

    records = output[:-1].split(b"\0")
    changes: dict[str, Change] = {}
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) < 4 or record[2:3] != b" " or not record[3:]:
            raise ReviewError("malformed Git status output")

        status_bytes = record[:2]
        if any(value not in b" MADRCUT?!" for value in status_bytes):
            raise ReviewError("malformed Git status output: invalid status")

        is_rename = b"R" in status_bytes or b"C" in status_bytes
        if is_rename:
            index += 1
            if index >= len(records) or not records[index]:
                raise ReviewError("malformed Git status output: missing rename source")

        path = _decode_git_path(record[3:])
        status = _normalize_status(status_bytes)
        change = Change(path, status)
        previous = changes.get(path)
        if previous is not None and previous != change:
            raise ReviewError("Git returned conflicting status data")
        changes[path] = change
        index += 1

    return sorted(changes.values())


def _normalize_status(status: bytes) -> str:
    if status == b"??":
        return "untracked"
    if b"D" in status:
        return "deleted"
    if b"A" in status:
        return "added"
    if b"R" in status or b"C" in status:
        return "renamed"
    return "modified"


def _decode_git_path(value: bytes) -> str:
    return os.fsdecode(value)
