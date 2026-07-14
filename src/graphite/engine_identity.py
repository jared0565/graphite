"""Deterministic, bounded identity for the installed Graphite engine."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Final

ENGINE_SCHEMA_VERSION: Final = "1"
MAX_ENGINE_FILES: Final = 512
MAX_ENGINE_FILE_BYTES: Final = 8 * 1024 * 1024
MAX_ENGINE_TOTAL_BYTES: Final = 64 * 1024 * 1024

_SOURCE_SUFFIXES = frozenset({".py", ".mjs"})
_EXCLUDED_DIRECTORIES = frozenset(
    {"__pycache__", ".cache", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class EngineIdentityError(RuntimeError):
    """A fixed, path-free engine inventory or read failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _is_reparse_point(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _stable_signature(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _contained_relative(path: Path, root: Path) -> str:
    try:
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise EngineIdentityError("engine_root_crossing") from exc
    normalized = relative.as_posix()
    if not normalized or normalized.startswith("../") or normalized.startswith("/"):
        raise EngineIdentityError("engine_root_crossing")
    return normalized


def _collect_engine_files(root: Path, *, max_files: int) -> list[Path]:
    if isinstance(max_files, bool) or max_files <= 0:
        raise EngineIdentityError("engine_file_limit")
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise EngineIdentityError("engine_root_invalid") from exc
    if _is_reparse_point(root_metadata) or stat.S_ISLNK(root_metadata.st_mode):
        raise EngineIdentityError("engine_root_reparse")
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise EngineIdentityError("engine_root_invalid")

    files: list[Path] = []
    pending = [root]
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    metadata = entry.stat(follow_symlinks=False)
                    if entry.is_symlink() or _is_reparse_point(metadata):
                        raise EngineIdentityError("engine_path_reparse")
                    entry_path = Path(entry.path)
                    if stat.S_ISDIR(metadata.st_mode):
                        if entry.name not in _EXCLUDED_DIRECTORIES:
                            pending.append(entry_path)
                        continue
                    if entry_path.suffix.lower() not in _SOURCE_SUFFIXES:
                        continue
                    if not stat.S_ISREG(metadata.st_mode):
                        raise EngineIdentityError("engine_non_regular")
                    files.append(entry_path)
                    if len(files) > max_files:
                        raise EngineIdentityError("engine_file_limit")
    except EngineIdentityError:
        raise
    except OSError as exc:
        raise EngineIdentityError("engine_file_unreadable") from exc
    return files


def _read_stable_file(path: Path, root: Path, limit: int) -> bytes:
    _contained_relative(path, root)
    try:
        before = path.lstat()
    except OSError as exc:
        raise EngineIdentityError("engine_file_unreadable") from exc
    if stat.S_ISLNK(before.st_mode) or _is_reparse_point(before):
        raise EngineIdentityError("engine_path_reparse")
    if not stat.S_ISREG(before.st_mode):
        raise EngineIdentityError("engine_non_regular")
    if before.st_size > limit:
        raise EngineIdentityError("engine_file_too_large")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if _stable_signature(opened) != _stable_signature(before):
                raise EngineIdentityError("engine_file_changed")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except EngineIdentityError:
        raise
    except OSError as exc:
        raise EngineIdentityError("engine_file_unreadable") from exc

    data = b"".join(chunks)
    if len(data) > limit:
        raise EngineIdentityError("engine_file_too_large")
    if _stable_signature(after) != _stable_signature(opened):
        raise EngineIdentityError("engine_file_changed")
    return data


def engine_identity(
    cache_version: str,
    *,
    package_root: Path | None = None,
    version: str | None = None,
    max_files: int = MAX_ENGINE_FILES,
    max_file_bytes: int = MAX_ENGINE_FILE_BYTES,
    max_total_bytes: int = MAX_ENGINE_TOTAL_BYTES,
) -> dict[str, str]:
    """Return a public, path-free identity for trusted packaged engine files."""
    if isinstance(max_file_bytes, bool) or max_file_bytes <= 0:
        raise EngineIdentityError("engine_file_too_large")
    if isinstance(max_total_bytes, bool) or max_total_bytes <= 0:
        raise EngineIdentityError("engine_total_too_large")
    if not isinstance(cache_version, str) or not cache_version:
        raise EngineIdentityError("engine_cache_version_invalid")

    if version is None:
        from . import __version__

        version = __version__
    if not isinstance(version, str) or not version:
        raise EngineIdentityError("engine_version_invalid")

    candidate_root = package_root if package_root is not None else Path(__file__).parent
    try:
        root = candidate_root.resolve(strict=True)
    except OSError as exc:
        raise EngineIdentityError("engine_root_invalid") from exc
    paths = _collect_engine_files(root, max_files=max_files)

    header = {
        "cache_version": cache_version,
        "schema_version": ENGINE_SCHEMA_VERSION,
        "version": version,
    }
    digest = hashlib.sha256()
    digest.update(json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    total_bytes = 0
    inventory: list[tuple[str, Path]] = [(_contained_relative(path, root), path) for path in paths]
    for relative, path in sorted(inventory, key=lambda item: item[0]):
        try:
            data = _read_stable_file(path, root, max_file_bytes)
        except EngineIdentityError:
            raise
        except OSError as exc:
            raise EngineIdentityError("engine_file_unreadable") from exc
        total_bytes += len(data)
        if total_bytes > max_total_bytes:
            raise EngineIdentityError("engine_total_too_large")
        encoded_name = relative.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    return {**header, "fingerprint": digest.hexdigest()}
