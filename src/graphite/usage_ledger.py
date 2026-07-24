"""Machine-local usage ledger backing the savings display.

Everything here is best-effort by contract: recording, toggling, and cursor
IO must never break the command or hook that calls them, so every public
function swallows its own errors. The ledger lives under ``.graphite/local/``,
which the standard gitignore lines already ignore (``**/.graphite/``).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .io import atomic_write_text

MAX_LEDGER_BYTES = 5 * 1024 * 1024
MAX_FILES_PER_ENTRY = 100
_FILE_KEYS = frozenset({"source_file", "file", "path"})
_FILE_LIST_KEYS = frozenset({"impacted_files", "likely_tests", "files"})


def local_dir(root: Path) -> Path:
    return root / ".graphite" / "local"


def ledger_path(root: Path) -> Path:
    return local_dir(root) / "usage.jsonl"


def _settings_path(root: Path) -> Path:
    return local_dir(root) / "settings.json"


def _cursor_path(root: Path) -> Path:
    return local_dir(root) / "stop-cursor.json"


def rotated_ledger_path(root: Path) -> Path:
    path = ledger_path(root)
    return path.parent / (path.name + ".1")


def collect_answer_files(result: Any, root: Path, cap: int = MAX_FILES_PER_ENTRY) -> list[dict[str, Any]]:
    """File paths named by an answer, with on-disk sizes; best-effort, capped."""
    seen: dict[str, None] = {}

    def _walk(value: Any) -> None:
        if len(seen) >= cap:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in _FILE_KEYS and isinstance(item, str) and item:
                    seen.setdefault(item)
                elif key in _FILE_LIST_KEYS and isinstance(item, list):
                    for path in item:
                        if isinstance(path, str) and path:
                            seen.setdefault(path)
                        if len(seen) >= cap:
                            return
                else:
                    _walk(item)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(result)
    files: list[dict[str, Any]] = []
    for path in list(seen)[:cap]:
        size = 0
        try:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = root / candidate
            size = candidate.stat().st_size
        except OSError:
            size = 0
        files.append({"path": path, "bytes": size})
    return files


def record_usage(root: Path, *, cmd: str, wall_ms: int, result: Any) -> None:
    """Append one usage record; rotate at the byte cap; never raise."""
    try:
        try:
            output_bytes = len(json.dumps(result, ensure_ascii=False))
        except (TypeError, ValueError):
            output_bytes = 0
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cmd": cmd,
            "wall_ms": int(wall_ms),
            "output_bytes": output_bytes,
            "files": collect_answer_files(result, root),
        }
        path = ledger_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_LEDGER_BYTES:
            os.replace(path, rotated_ledger_path(root))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        return


def iter_entries(root: Path) -> Iterator[dict[str, Any]]:
    """All entries, rotated generation first; corrupt lines skipped; never raises."""
    try:
        current = ledger_path(root)
        for path in (rotated_ledger_path(root), current):
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except Exception:
        return


def _read_local_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def savings_display_enabled(root: Path) -> bool:
    value = _read_local_json(_settings_path(root)).get("savings_display")
    return True if not isinstance(value, bool) else value


def set_savings_display(root: Path, enabled: bool) -> dict[str, Any]:
    settings = _read_local_json(_settings_path(root))
    settings["savings_display"] = bool(enabled)
    try:
        atomic_write_text(_settings_path(root), json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        pass
    return settings


def read_cursor(root: Path) -> dict[str, Any]:
    return _read_local_json(_cursor_path(root))


def write_cursor(root: Path, cursor: dict[str, Any]) -> None:
    try:
        atomic_write_text(_cursor_path(root), json.dumps(cursor, ensure_ascii=False) + "\n")
    except Exception:
        return
