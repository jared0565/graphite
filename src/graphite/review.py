"""Safe collection of explicit and Git-reported review changes."""

from __future__ import annotations

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
            raise ReviewError(f"path is outside project root: {raw_path!r}") from exc

        if relative_path == Path("."):
            raise ReviewError("a change path cannot be the project root")
        normalized.add(relative_path.as_posix())

    return [Change(path, "explicit") for path in sorted(normalized)]


def discover_git_changes(root: Path, *, timeout_seconds: float = 5.0) -> list[Change]:
    """Collect normalized changes from Git porcelain output."""
    if timeout_seconds <= 0:
        raise ReviewError("Git status timeout must be greater than zero")

    resolved_root = root.resolve()
    command = ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
    try:
        result = subprocess.run(
            command,
            cwd=resolved_root,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise ReviewError("Git executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReviewError("Git status timed out") from exc
    except OSError as exc:
        raise ReviewError(f"unable to run Git status: {_bounded_text(str(exc))}") from exc

    if result.returncode != 0:
        detail = _bounded_text(result.stderr.decode("utf-8", errors="replace").strip())
        suffix = f": {detail}" if detail else ""
        raise ReviewError(f"not a Git worktree{suffix}")

    return _parse_porcelain(result.stdout)


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
        changes[path] = Change(path, status)
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
    try:
        path = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewError("malformed Git status output: path is not UTF-8") from exc
    return path.replace("\\", "/")


def _bounded_text(value: str, limit: int = 240) -> str:
    return value[:limit]
