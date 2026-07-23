"""Provider-agnostic, hardened whole-file edit apply engine.

The single authority on edit-payload safety: path-traversal rejection,
symlink/reparse-point rejection, per-file and total byte caps, and atomic
replace with full rollback on any mid-set failure. Shared by every provider
executor (OpenRouter, z.ai); it consumes a validated payload and is agnostic
to how that payload was produced (JSON vs plain-text marker parse)."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final

from .claude_executor import AdapterError

MAX_EDIT_FILE_BYTES: Final = 1_048_576
MAX_EDIT_TOTAL_BYTES: Final = 1_073_741_824
MAX_EDIT_PATH_LENGTH: Final = 512
MAX_EDIT_SCOPE_FILES: Final = 1_000
EDIT_RESULT_MARKER: Final = "GRAPHITE_EDIT_OK"
_TEMP_SUFFIX: Final = ".graphite-tmp"
_REPARSE_POINT: Final = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attributes & _REPARSE_POINT)


def _validate_edit_path(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_EDIT_PATH_LENGTH
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or value.startswith("/")
        or value.endswith(_TEMP_SUFFIX)
    ):
        raise AdapterError("edit_scope_violation")
    segments = tuple(value.split("/"))
    if any(segment in ("", ".", "..") for segment in segments):
        raise AdapterError("edit_scope_violation")
    return segments


def _cleanup_temps(temps: list[tuple[Path, Path]]) -> None:
    for temp, _target in temps:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def apply_whole_file_edit(
    *,
    workspace: Path,
    payload: object,
    edit_scope: tuple[str, ...],
    max_total_bytes: int,
) -> tuple[str, ...]:
    """Validate a whole-file payload completely, then apply it atomically.

    A validation failure leaves the workspace byte-identical; a mid-set
    replace failure restores every already-replaced file from held originals.
    """
    if not isinstance(workspace, Path):
        raise AdapterError("edit_scope_violation")
    try:
        workspace_root = workspace.resolve(strict=True)
    except OSError:
        raise AdapterError("edit_scope_violation") from None
    if not workspace_root.is_dir() or workspace_root.is_symlink():
        raise AdapterError("edit_scope_violation")
    if (
        not isinstance(edit_scope, tuple)
        or not edit_scope
        or len(edit_scope) > MAX_EDIT_SCOPE_FILES
        or len(set(edit_scope)) != len(edit_scope)
    ):
        raise AdapterError("edit_scope_violation")
    if (
        isinstance(max_total_bytes, bool)
        or not isinstance(max_total_bytes, int)
        or not 1 <= max_total_bytes <= MAX_EDIT_TOTAL_BYTES
    ):
        raise AdapterError("edit_scope_violation")
    if not isinstance(payload, dict) or set(payload) != {"files", "result"}:
        raise AdapterError("edit_scope_violation")
    if payload["result"] != EDIT_RESULT_MARKER:
        raise AdapterError("edit_scope_violation")
    files = payload["files"]
    if not isinstance(files, list) or len(files) != len(edit_scope):
        raise AdapterError("edit_scope_violation")
    planned: dict[str, tuple[Path, tuple[str, ...], bytes]] = {}
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"content", "path"}:
            raise AdapterError("edit_scope_violation")
        raw_path = item["path"]
        segments = _validate_edit_path(raw_path)
        if raw_path in planned:
            raise AdapterError("edit_scope_violation")
        content = item["content"]
        if not isinstance(content, str):
            raise AdapterError("edit_scope_violation")
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError:
            raise AdapterError("edit_scope_violation") from None
        if len(encoded) > MAX_EDIT_FILE_BYTES:
            raise AdapterError("edit_scope_violation")
        total += len(encoded)
        target = workspace_root
        for segment in segments:
            target = target / segment
        planned[raw_path] = (target, segments, encoded)
    if total > max_total_bytes or set(planned) != set(edit_scope):
        raise AdapterError("edit_scope_violation")
    originals: dict[Path, bytes] = {}
    for raw_path in sorted(planned):
        target, segments, _encoded = planned[raw_path]
        component = workspace_root
        for segment in segments:
            component = component / segment
            if component.is_symlink() or _is_reparse_point(component):
                raise AdapterError("edit_scope_violation")
            if not component.exists():
                raise AdapterError("edit_scope_violation")
        if not target.is_file():
            raise AdapterError("edit_scope_violation")
        if target.with_name(target.name + _TEMP_SUFFIX).exists():
            raise AdapterError("edit_scope_violation")
        try:
            originals[target] = target.read_bytes()
        except OSError:
            raise AdapterError("edit_apply_failed") from None
    temps: list[tuple[Path, Path]] = []
    try:
        for raw_path in sorted(planned):
            target, _segments, encoded = planned[raw_path]
            temp = target.with_name(target.name + _TEMP_SUFFIX)
            temps.append((temp, target))
            temp.write_bytes(encoded)
    except OSError:
        _cleanup_temps(temps)
        raise AdapterError("edit_apply_failed") from None
    replaced: list[Path] = []
    try:
        for temp, target in temps:
            os.replace(temp, target)
            replaced.append(target)
    except OSError:
        for target in replaced:
            try:
                target.write_bytes(originals[target])
            except OSError:
                pass
        _cleanup_temps(temps)
        raise AdapterError("edit_apply_failed") from None
    return tuple(sorted(planned))
