"""Community detection using Louvain (and optional Leiden)."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

import community as community_louvain
import networkx as nx
from pathlib import Path


def detect_communities(g: nx.DiGraph, seed: int = 42, weight: str = "weight") -> dict[str, Any]:
    """Run Louvain on an undirected projection of the graph."""
    # Build deterministic undirected projection manually.
    ug = nx.Graph()
    for n in sorted(g.nodes()):
        ug.add_node(n)
    # Aggregate weights for both directions deterministically.
    edge_weights: dict[tuple[str, str], float] = {}
    for u, v, d in sorted(g.edges(data=True), key=lambda e: (e[0], e[1], e[2].get("relation", ""))):
        key = tuple(sorted((u, v)))
        edge_weights[key] = edge_weights.get(key, 0.0) + d.get("weight", 1.0)
    for (u, v), w in sorted(edge_weights.items()):
        ug.add_edge(u, v, weight=w)

    partition = community_louvain.best_partition(ug, random_state=seed, weight=weight)

    communities: dict[int, set[str]] = defaultdict(set)
    for node, comm in partition.items():
        communities[comm].add(node)

    # Build cluster metadata.
    clusters = []
    for cid, members in sorted(communities.items()):
        file_nodes = [n for n in members if g.nodes[n].get("kind") == "file"]
        func_nodes = [n for n in members if g.nodes[n].get("kind") in ("function", "method")]
        class_nodes = [n for n in members if g.nodes[n].get("kind") == "class"]
        labels = _label_cluster(g, members)
        clusters.append({
            "id": cid,
            "members": sorted(members),
            "size": len(members),
            "file_count": len(file_nodes),
            "function_count": len(func_nodes),
            "class_count": len(class_nodes),
            "labels": labels,
        })

    return {
        "node_to_community": {k: partition[k] for k in sorted(partition)},
        "clusters": sorted(clusters, key=lambda c: c["size"], reverse=True),
        "count": len(clusters),
    }


def _label_cluster(g: nx.DiGraph, members: Iterable[str]) -> list[str]:
    """Generate zero-LLM cluster labels from shared directory and common node kinds.

    Iterates in sorted order because the caller passes a `set`, whose order
    `PYTHONHASHSEED` randomizes per process. That reached the artifact: `max()`
    returns the FIRST maximum, so a tie between two kinds resolved to whichever
    the set happened to yield first. Measured -- one cluster's labels were
    `['src/graphite']` in one build of a commit and `['src/graphite',
    'functions']` in the next, because `unknown` and `function` tied and
    `unknown` suppresses the label entirely.
    """
    labels: list[str] = []
    ordered = sorted(members)

    # Shared parent directory from file nodes.
    dirs: list[str] = []
    for n in ordered:
        sf = g.nodes[n].get("source_file")
        if sf:
            dirs.append(Path(sf).parent.as_posix())
    if dirs:
        common = _common_prefix(dirs)
        if common and common != ".":
            labels.append(common)

    # Most common kind, ties broken by name so the winner is a property of the
    # cluster rather than of this process's hash seed.
    kinds: dict[str, int] = defaultdict(int)
    for n in ordered:
        kinds[g.nodes[n].get("kind", "unknown")] += 1
    top_kind = min(kinds.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    if top_kind != "unknown":
        labels.append(f"{top_kind}s")

    # Dedupe while preserving order.
    seen = set()
    out = []
    for label in labels:
        if label not in seen:
            out.append(label)
            seen.add(label)
    return out


def _common_prefix(paths: list[str]) -> str:
    if not paths:
        return ""
    parts = [p.split("/") for p in paths]
    prefix = []
    for segments in zip(*parts):
        if len(set(segments)) == 1:
            prefix.append(segments[0])
        else:
            break
    return "/".join(prefix)
