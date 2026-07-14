"""Bounded, validated reads for untrusted Graphite graph artifacts."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Final

from .graph import graph_from_json
from .validation import validate_graph_bundle

MAX_GRAPH_BYTES: Final = 128 * 1024 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_ERROR_CODES = frozenset(
    {
        "graph_root_invalid",
        "graph_outside_root",
        "graph_missing",
        "graph_reparse",
        "graph_not_regular",
        "graph_too_large",
        "graph_changed",
        "graph_unreadable",
        "graph_invalid_utf8",
        "graph_invalid_json",
        "graph_invalid",
    }
)


class GraphReadError(RuntimeError):
    """A stable, path-free graph-read failure."""

    def __init__(self, code: str) -> None:
        if code not in _ERROR_CODES:
            code = "graph_unreadable"
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


def _resolve_root(root: Path) -> Path:
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise GraphReadError("graph_root_invalid") from exc
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
        raise GraphReadError("graph_root_invalid")
    if not stat.S_ISDIR(metadata.st_mode):
        raise GraphReadError("graph_root_invalid")
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise GraphReadError("graph_root_invalid") from exc


def _resolve_candidate(path: Path, root: Path) -> Path:
    candidate = path if path.is_absolute() else root / path
    try:
        lexical_relative = candidate.absolute().relative_to(root)
    except ValueError as exc:
        raise GraphReadError("graph_outside_root") from exc
    if ".." in lexical_relative.parts:
        raise GraphReadError("graph_outside_root")
    current = root
    for part in lexical_relative.parts[:-1]:
        current /= part
        try:
            parent_metadata = current.lstat()
        except FileNotFoundError as exc:
            raise GraphReadError("graph_missing") from exc
        except OSError as exc:
            raise GraphReadError("graph_unreadable") from exc
        if stat.S_ISLNK(parent_metadata.st_mode) or _is_reparse_point(parent_metadata):
            raise GraphReadError("graph_reparse")
    try:
        lexical_metadata = candidate.lstat()
    except FileNotFoundError as exc:
        raise GraphReadError("graph_missing") from exc
    except OSError as exc:
        raise GraphReadError("graph_unreadable") from exc
    if stat.S_ISLNK(lexical_metadata.st_mode) or _is_reparse_point(lexical_metadata):
        raise GraphReadError("graph_reparse")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except ValueError as exc:
        raise GraphReadError("graph_outside_root") from exc
    except FileNotFoundError as exc:
        raise GraphReadError("graph_missing") from exc
    except OSError as exc:
        raise GraphReadError("graph_unreadable") from exc
    return resolved


def _read_stable_bytes(path: Path, *, max_bytes: int) -> bytes:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise GraphReadError("graph_missing") from exc
    except OSError as exc:
        raise GraphReadError("graph_unreadable") from exc
    if stat.S_ISLNK(before.st_mode) or _is_reparse_point(before):
        raise GraphReadError("graph_reparse")
    if not stat.S_ISREG(before.st_mode):
        raise GraphReadError("graph_not_regular")
    if before.st_size > max_bytes:
        raise GraphReadError("graph_too_large")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if _stable_signature(opened) != _stable_signature(before):
                raise GraphReadError("graph_changed")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except GraphReadError:
        raise
    except FileNotFoundError as exc:
        raise GraphReadError("graph_missing") from exc
    except OSError as exc:
        raise GraphReadError("graph_unreadable") from exc

    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise GraphReadError("graph_too_large")
    if _stable_signature(after) != _stable_signature(opened):
        raise GraphReadError("graph_changed")
    return data


def load_validated_graph_bundle(
    path: Path,
    *,
    root: Path,
    max_bytes: int = MAX_GRAPH_BYTES,
) -> tuple[dict[str, Any], Any]:
    """Read, validate, and construct a graph inside the selected root."""
    if isinstance(max_bytes, bool) or max_bytes <= 0 or max_bytes > MAX_GRAPH_BYTES:
        raise GraphReadError("graph_too_large")
    selected_root = _resolve_root(root)
    graph_path = _resolve_candidate(path, selected_root)
    raw = _read_stable_bytes(graph_path, max_bytes=max_bytes)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GraphReadError("graph_invalid_utf8") from exc
    try:
        bundle = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise GraphReadError("graph_invalid_json") from exc
    if not isinstance(bundle, dict):
        raise GraphReadError("graph_invalid")
    try:
        report = validate_graph_bundle(bundle)
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError) as exc:
        raise GraphReadError("graph_invalid") from exc
    if not report.get("ok"):
        raise GraphReadError("graph_invalid")
    try:
        graph = graph_from_json(bundle)
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError) as exc:
        raise GraphReadError("graph_invalid") from exc
    return bundle, graph
