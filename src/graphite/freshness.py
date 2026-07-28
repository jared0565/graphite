"""Graph artifact freshness checks shared by CLI and diagnostics."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config
from .engine_identity import EngineIdentityError, engine_identity
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


def graph_engine_changed(cfg: Config, current_engine: dict[str, Any]) -> bool:
    """True when a built graph records an engine identity other than the current one.

    Narrow by design, for the daemon (#18): it reads the manifest and nothing
    else. `check_graph_freshness` re-scans the whole repo, which is far too
    expensive to run per supervised project on every discovery cycle.

    A missing, unreadable, or engine-less manifest is deliberately NOT an engine
    change. Those are the initial-build path's job; reporting them here would
    queue a rebuild every cycle, forever, for any project without a graph.

    The comparison is on the WHOLE identity, not the fingerprint alone, so a
    `version` or `cache_version` bump counts as a change even when the packaged
    engine files are byte-identical. That is deliberate: it is exactly what
    `check_graph_freshness` treats as `engine_changed`, and the daemon reporting
    a different notion of staleness than `graphite check` is how the two drift
    apart.
    """
    manifest_path = cfg.output_dir / ".graphite_manifest.json"
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(previous, dict):
        return False
    recorded = previous.get("engine")
    if not recorded:
        return False
    return bool(recorded != current_engine)


def check_graph_freshness(
    root: Path,
    cfg: Config,
    *,
    max_manifest_bytes: int | None = None,
    ignore_engine: bool = False,
) -> dict[str, Any]:
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
    if not ignore_engine:
        try:
            current_engine = engine_identity(cfg.cache_version)
        except EngineIdentityError:
            return {
                "stale": True,
                "reason": "engine_identity_unavailable",
                "added": [],
                "changed": [],
                "removed": [],
            }
        if previous.get("engine") != current_engine:
            return {
                "stale": True,
                "reason": "engine_changed",
                "added": [],
                "changed": [],
                "removed": [],
            }
    current = {e.rel_path: e.content_hash for e in entries}
    added = sorted(set(current) - set(old))
    removed = sorted(set(old) - set(current))
    changed = sorted(p for p in set(current).intersection(old) if current[p] != old[p])
    return {"stale": bool(added or removed or changed), "file_count": len(current), "manifest_file_count": len(old), "added": added, "changed": changed, "removed": removed}
