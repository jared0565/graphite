"""Graph artifact freshness checks shared by CLI and diagnostics."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config
from .ingest import collect_files


def manifest_map(manifest: dict[str, Any]) -> dict[str, str]:
    return {f["rel_path"]: f.get("hash", "") for f in manifest.get("files", []) if "rel_path" in f}


def check_graph_freshness(root: Path, cfg: Config) -> dict[str, Any]:
    manifest_path = cfg.output_dir / ".graphite_manifest.json"
    if not manifest_path.exists():
        return {"stale": True, "reason": "missing manifest", "manifest": manifest_path.as_posix(), "added": [], "changed": [], "removed": []}
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"stale": True, "reason": f"unreadable manifest: {exc}", "added": [], "changed": [], "removed": []}
    entries = collect_files(root, cfg)
    current = {e.rel_path: e.content_hash for e in entries}
    old = manifest_map(previous)
    added = sorted(set(current) - set(old))
    removed = sorted(set(old) - set(current))
    changed = sorted(p for p in set(current).intersection(old) if current[p] != old[p])
    return {"stale": bool(added or removed or changed), "file_count": len(current), "manifest_file_count": len(old), "added": added, "changed": changed, "removed": removed}
