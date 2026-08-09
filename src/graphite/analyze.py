"""Graph analysis: god nodes, orphans, entry points, cycles."""
from __future__ import annotations

from typing import Any

import networkx as nx

from .health import resolution_health

_PROJECT_NODE_KINDS = frozenset({"file", "function", "class"})


def _is_project_node(g: nx.DiGraph, node: str) -> bool:
    """Return true for nodes extracted from repository-owned source files."""
    return g.nodes[node].get("kind") in _PROJECT_NODE_KINDS


def _project_nodes(g: nx.DiGraph) -> list[str]:
    return [n for n in g.nodes() if _is_project_node(g, n)]


def _stable_cycles(g: nx.DiGraph, top_n: int) -> list[list[str]]:
    """Cycles in a fixed order, each rotated to start at its smallest node.

    `nx.simple_cycles` yields in an order that follows set iteration, which
    `PYTHONHASHSEED` randomizes per process. That is not a cosmetic difference
    once `[:top_n]` truncates the result: two builds of one commit reported a
    *different set* of cycles and so disagreed about which cycles the repository
    contains. Measured on this repository -- `nodes`, `edges` and `metadata`
    were byte-identical between builds while `analysis.cycles` was not.

    A cycle is also the same cycle under rotation, so one spelling has to be
    chosen or `[a, b, c]` and `[b, c, a]` both appear depending on where the
    traversal entered. Nodes within a simple cycle are distinct, so the minimum
    is unique and rotating to it is canonical. Shortest first, then
    lexicographic, so the truncation keeps the tightest cycles.
    """
    canonical: list[list[str]] = []
    for cycle in nx.simple_cycles(g):
        pivot = cycle.index(min(cycle))
        canonical.append(cycle[pivot:] + cycle[:pivot])
    canonical.sort(key=lambda cycle: (len(cycle), cycle))
    return canonical[:top_n]


def analyze(g: nx.DiGraph, top_n: int = 20) -> dict[str, Any]:
    """Run the full analysis suite."""
    project_subgraph = g.subgraph(_project_nodes(g)).copy()
    return {
        "god_nodes": god_nodes(g, top_n),
        "orphans": orphan_nodes(g, top_n),
        "entry_points": entry_points(g, top_n),
        "surprising_connections": surprising_connections(g, top_n),
        "cycles": _stable_cycles(project_subgraph, top_n),
        "top_files_by_links": top_files_by_links(g, top_n),
        "resolution_health": resolution_health(g),
    }


def god_nodes(g: nx.DiGraph, top_n: int = 20) -> list[dict[str, Any]]:
    """Project-owned nodes with the highest total degree (in + out)."""
    degrees = [(n, g.degree(n)) for n in _project_nodes(g)]
    degrees.sort(key=lambda x: x[1], reverse=True)
    return [
        {
            "id": n,
            "name": g.nodes[n].get("name", n),
            "kind": g.nodes[n].get("kind", "unknown"),
            "degree": deg,
            "in_degree": g.in_degree(n),
            "out_degree": g.out_degree(n),
        }
        for n, deg in degrees[:top_n]
    ]


def orphan_nodes(g: nx.DiGraph, top_n: int = 20) -> list[dict[str, Any]]:
    """Project-owned nodes with zero connections."""
    orphans = [n for n in _project_nodes(g) if g.degree(n) == 0]
    return [
        {
            "id": n,
            "name": g.nodes[n].get("name", n),
            "kind": g.nodes[n].get("kind", "unknown"),
        }
        for n in orphans[:top_n]
    ]


def entry_points(g: nx.DiGraph, top_n: int = 20) -> list[dict[str, Any]]:
    """File nodes with many outgoing edges and few incoming edges."""
    file_nodes = [n for n in g.nodes() if g.nodes[n].get("kind") == "file"]
    scored = []
    for n in file_nodes:
        out_deg = g.out_degree(n)
        in_deg = g.in_degree(n)
        if out_deg > 0:
            scored.append((n, out_deg, in_deg, out_deg / max(in_deg, 1)))
    scored.sort(key=lambda x: x[3], reverse=True)
    return [
        {
            "id": n,
            "name": g.nodes[n].get("name", n),
            "out_degree": out_deg,
            "in_degree": in_deg,
            "ratio": ratio,
        }
        for n, out_deg, in_deg, ratio in scored[:top_n]
    ]


def surprising_connections(g: nx.DiGraph, top_n: int = 20) -> list[dict[str, Any]]:
    """Project-owned edges between nodes that share no common neighbours."""
    scored = []
    for u, v in g.edges():
        if not (_is_project_node(g, u) and _is_project_node(g, v)):
            continue
        common = len(set(g.predecessors(u)).intersection(g.predecessors(v))) + len(
            set(g.successors(u)).intersection(g.successors(v))
        )
        if common == 0:
            scored.append(
                {
                    "source": u,
                    "target": v,
                    "source_kind": g.nodes[u].get("kind", "unknown"),
                    "target_kind": g.nodes[v].get("kind", "unknown"),
                    "relation": g[u][v].get("relation", "unknown"),
                }
            )
    return scored[:top_n]


def top_files_by_links(g: nx.DiGraph, top_n: int = 20) -> list[dict[str, Any]]:
    """File nodes ranked by total degree."""
    file_nodes = [n for n in g.nodes() if g.nodes[n].get("kind") == "file"]
    ranked = sorted(file_nodes, key=lambda n: g.degree(n), reverse=True)
    return [
        {
            "id": n,
            "name": g.nodes[n].get("name", n),
            "degree": g.degree(n),
            "source_file": g.nodes[n].get("source_file"),
        }
        for n in ranked[:top_n]
    ]
