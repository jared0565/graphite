"""Resolution-health ("trust") signal computed from the canonical graph.

Pure arithmetic over the loaded graph — no inference, no I/O in
resolution_health itself. persisted_resolution is the fail-open reader for
consumers that must not pay a full graph load (check, strict hook).
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, Final

import networkx as nx

RESOLUTION_HEALTHY_RATIO: Final = 0.8

_COUNTED_RELATIONS: Final = ("calls", "imports")

_EXTENSION_LANGUAGES: Final = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
}

_MAX_ANALYSIS_BYTES: Final = 64 * 1024 * 1024


def _edge_language(source_file: object) -> str:
    if not isinstance(source_file, str) or not source_file:
        return "other"
    suffix = PurePosixPath(source_file).suffix.lower()
    return _EXTENSION_LANGUAGES.get(suffix, "other")


def _cell(bound: int, total: int) -> dict[str, Any]:
    return {
        "total": total,
        "bound": bound,
        "ratio": None if total == 0 else round(bound / total, 3),
    }


def resolution_health(g: nx.DiGraph) -> dict[str, Any]:
    """Measured resolver health: bound-edge ratios per relation and language."""
    node_total = g.number_of_nodes()
    unknown_nodes = sum(
        1 for _n, data in g.nodes(data=True) if data.get("kind", "unknown") == "unknown"
    )
    relation_counts = {rel: [0, 0] for rel in _COUNTED_RELATIONS}  # [bound, total]
    language_counts: dict[str, dict[str, list[int]]] = {}
    for _u, v, data in g.edges(data=True):
        relation = data.get("relation")
        counts = relation_counts.get(relation)
        if counts is None:
            continue
        bound = int(g.nodes[v].get("kind", "unknown") != "unknown")
        counts[0] += bound
        counts[1] += 1
        language = _edge_language(data.get("source_file"))
        buckets = language_counts.setdefault(
            language, {rel: [0, 0] for rel in _COUNTED_RELATIONS}
        )
        buckets[relation][0] += bound
        buckets[relation][1] += 1
    by_relation = {rel: _cell(c[0], c[1]) for rel, c in relation_counts.items()}
    by_language = {
        language: {rel: _cell(c[0], c[1]) for rel, c in buckets.items()}
        for language, buckets in sorted(language_counts.items())
    }
    ratios = [cell["ratio"] for cell in by_relation.values() if cell["ratio"] is not None]
    return {
        "schema": 1,
        "placeholder_nodes": {
            "total": node_total,
            "unknown": unknown_nodes,
            "share": None if node_total == 0 else round(unknown_nodes / node_total, 3),
        },
        "by_relation": by_relation,
        "by_language": by_language,
        "healthy": all(ratio >= RESOLUTION_HEALTHY_RATIO for ratio in ratios),
        "threshold": RESOLUTION_HEALTHY_RATIO,
    }


def ratio_percent(block: dict[str, Any], relation: str) -> str:
    """Human rendering of one relation's bound ratio: '4.6%' or 'n/a'."""
    try:
        ratio = block["by_relation"][relation]["ratio"]
    except (KeyError, TypeError):
        return "n/a"
    if not isinstance(ratio, (int, float)):
        return "n/a"
    return f"{ratio * 100:.1f}%"


def persisted_resolution(root: Path) -> dict[str, Any] | None:
    """Fail-open read of the persisted block from graph-out/.graphite_analysis.json."""
    path = root / "graph-out" / ".graphite_analysis.json"
    try:
        if not path.is_file() or path.stat().st_size > _MAX_ANALYSIS_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    block = data.get("resolution")
    return block if isinstance(block, dict) else None
