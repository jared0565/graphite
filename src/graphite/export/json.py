"""JSON export of the full graph bundle."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..io import atomic_write_json


def build_bundle(
    graph_data: dict[str, Any],
    clusters: dict[str, Any],
    analysis: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Build the public graph JSON bundle used by viewers and tools."""
    return {
        "nodes": graph_data.get("nodes", []),
        "edges": graph_data.get("edges", []),
        "clusters": clusters.get("clusters", []),
        "analysis": analysis,
        "metadata": {
            "node_count": graph_data.get("metadata", {}).get("node_count", 0),
            "edge_count": graph_data.get("metadata", {}).get("edge_count", 0),
            "community_count": clusters.get("count", 0),
            **{k: v for k, v in manifest.items() if k not in ("files",)},
        },
    }


def to_json(
    graph_data: dict[str, Any],
    clusters: dict[str, Any],
    analysis: dict[str, Any],
    manifest: dict[str, Any],
    output_path: Path,
) -> None:
    """Write the bundled graph JSON used by the HTML viewer and external tools."""
    atomic_write_json(output_path, build_bundle(graph_data, clusters, analysis, manifest), indent=2)
