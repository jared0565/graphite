"""Graph artifact freshness checks shared by CLI and diagnostics."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config
from .ingest import collect_files

_DOCTOR_FILE_LIMIT = 10_000


class FreshnessLimitError(RuntimeError):
    """A stable, path-free freshness resource-limit failure."""


def _manifest_size(path: Path) -> int:
    return path.stat().st_size


def _read_manifest_bounded(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise FreshnessLimitError("freshness manifest limit check failed") from exc
    if len(data) > limit:
        raise FreshnessLimitError("freshness manifest limit exceeded")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FreshnessLimitError("freshness manifest is not valid UTF-8") from exc


def manifest_map(manifest: dict[str, Any]) -> dict[str, str]:
    return {f["rel_path"]: f.get("hash", "") for f in manifest.get("files", []) if "rel_path" in f}


def check_graph_freshness(root: Path, cfg: Config, *, max_manifest_bytes: int | None = None) -> dict[str, Any]:
    manifest_path = cfg.output_dir / ".graphite_manifest.json"
    if not manifest_path.exists():
        return {"stale": True, "reason": "missing manifest", "manifest": manifest_path.as_posix(), "added": [], "changed": [], "removed": []}
    try:
        if max_manifest_bytes is None:
            manifest_text = manifest_path.read_text(encoding="utf-8")
        else:
            if max_manifest_bytes <= 0:
                raise FreshnessLimitError("freshness manifest limit exceeded")
            try:
                size = _manifest_size(manifest_path)
            except OSError as exc:
                raise FreshnessLimitError("freshness manifest limit check failed") from exc
            if size > max_manifest_bytes:
                raise FreshnessLimitError("freshness manifest limit exceeded")
            manifest_text = _read_manifest_bounded(manifest_path, max_manifest_bytes)
        previous = json.loads(manifest_text)
    except (OSError, json.JSONDecodeError) as exc:
        return {"stale": True, "reason": f"unreadable manifest: {exc}", "added": [], "changed": [], "removed": []}
    old = manifest_map(previous)
    if max_manifest_bytes is not None and len(old) > _DOCTOR_FILE_LIMIT:
        raise FreshnessLimitError("freshness file limit exceeded")
    entries = collect_files(root, cfg)
    if max_manifest_bytes is not None and len(entries) > _DOCTOR_FILE_LIMIT:
        raise FreshnessLimitError("freshness file limit exceeded")
    current = {e.rel_path: e.content_hash for e in entries}
    added = sorted(set(current) - set(old))
    removed = sorted(set(old) - set(current))
    changed = sorted(p for p in set(current).intersection(old) if current[p] != old[p])
    return {"stale": bool(added or removed or changed), "file_count": len(current), "manifest_file_count": len(old), "added": added, "changed": changed, "removed": removed}
