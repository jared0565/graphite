"""Shared, non-executing CLI identity primitives."""
from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path
from typing import Final, Pattern

MAX_EXECUTABLE_BYTES: Final = 512 * 1024 * 1024
_REPARSE_POINT: Final = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SEMANTIC_VERSION: Final = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)


class CliIdentityPrimitiveError(RuntimeError):
    """Stable local identity failure."""


def _is_reparse(metadata: object) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def canonical_executable(path: Path, *, workspace: Path | None = None) -> Path:
    """Return an absolute regular executable outside the workspace trust boundary."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise CliIdentityPrimitiveError("executable_invalid")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        resolved_metadata = resolved.stat()
        workspace_root = workspace.resolve(strict=True) if workspace is not None else None
    except (OSError, RuntimeError):
        raise CliIdentityPrimitiveError("executable_invalid") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or not stat.S_ISREG(resolved_metadata.st_mode)
        or _is_reparse(resolved_metadata)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_EXECUTABLE_BYTES
        or workspace_root is not None
        and resolved.is_relative_to(workspace_root)
    ):
        raise CliIdentityPrimitiveError("executable_invalid")
    return resolved


def executable_sha256(path: Path) -> str:
    """Hash a bounded regular file and reject concurrent identity drift."""
    digest = hashlib.sha256()
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_EXECUTABLE_BYTES:
            raise OSError
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        after = path.stat()
    except OSError:
        raise CliIdentityPrimitiveError("executable_invalid") from None
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise CliIdentityPrimitiveError("executable_changed")
    return digest.hexdigest()


def parse_semantic_version_output(text: str, pattern: Pattern[str]) -> str:
    """Extract and revalidate one exact semantic version match."""
    if not isinstance(text, str) or not isinstance(pattern, Pattern):
        raise CliIdentityPrimitiveError("version_invalid")
    match = pattern.fullmatch(text)
    if match is None or len(match.groups()) != 3:
        raise CliIdentityPrimitiveError("version_invalid")
    version = ".".join(match.groups())
    if _SEMANTIC_VERSION.fullmatch(version) is None:
        raise CliIdentityPrimitiveError("version_invalid")
    return version
