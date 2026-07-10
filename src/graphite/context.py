"""Agent-focused compact graph context for selected files or nodes."""
from __future__ import annotations

from collections import deque
from typing import Any

import networkx as nx

from .query import _find_node

_TEST_SUFFIXES = (
    ".test.ts",
    ".spec.ts",
    ".test.tsx",
    ".spec.tsx",
    ".test.js",
    ".spec.js",
    ".test.py",
    ".spec.py",
)


def build_context(
    g: nx.DiGraph,
    inputs: list[str],
    *,
    depth: int = 2,
    neighbor_limit: int = 20,
) -> dict[str, Any]:
    """Build compact context for code agents without dumping the full graph."""
    if depth < 0:
        raise ValueError("context depth must be zero or greater")
    if neighbor_limit <= 0:
        raise ValueError("context neighbor limit must be greater than zero")

    matched: list[dict[str, Any]] = []
    missing: list[str] = []
    start_nodes: list[str] = []
    for item in inputs:
        node = _find_node(g, item)
        if node:
            start_nodes.append(node)
            matched.append({"input": item, "node": _node_summary(g, node)})
        else:
            missing.append(item)

    impact = _reverse_impact(g, start_nodes, depth)
    direct_dependencies = {node: _neighbors(g, node, outgoing=True, limit=neighbor_limit) for node in start_nodes}
    direct_dependents = {node: _neighbors(g, node, outgoing=False, limit=neighbor_limit) for node in start_nodes}
    communities = _community_peers(g, start_nodes, limit=neighbor_limit)
    risk = [_risk_summary(g, node) for node in start_nodes]

    return {
        "metadata": {
            "node_count": g.number_of_nodes(),
            "edge_count": g.number_of_edges(),
            "density": nx.density(g),
        },
        "inputs": inputs,
        "matched": matched,
        "missing": missing,
        "depth": depth,
        "direct_dependencies": direct_dependencies,
        "direct_dependents": direct_dependents,
        "impact": impact,
        "communities": communities,
        "risk": risk,
    }


def format_context_markdown(context: dict[str, Any]) -> str:
    """Render compact context as Markdown for humans and agents."""
    meta = context["metadata"]
    lines = [
        "# Graphite Context",
        "",
        f"- Nodes: {meta['node_count']}",
        f"- Edges: {meta['edge_count']}",
        f"- Depth: {context['depth']}",
    ]
    if context["missing"]:
        lines.append(f"- Missing inputs: {', '.join(context['missing'])}")

    lines.extend(["", "## Matched"])
    for item in context["matched"]:
        node = item["node"]
        lines.append(
            f"- `{item['input']}` -> `{node['id']}` "
            f"({node['kind']}, in={node['in_degree']}, out={node['out_degree']})"
        )

    lines.extend(["", "## Impact"])
    impact = context["impact"]
    if impact["impacted_files"]:
        lines.append("Impacted files:")
        lines.extend(f"- `{path}`" for path in impact["impacted_files"][:30])
    else:
        lines.append("Impacted files: none found")
    if impact["likely_tests"]:
        lines.append("Likely tests:")
        lines.extend(f"- `{path}`" for path in impact["likely_tests"][:30])

    lines.extend(["", "## Direct Dependents"])
    _append_neighbor_section(lines, context["direct_dependents"])

    lines.extend(["", "## Direct Dependencies"])
    _append_neighbor_section(lines, context["direct_dependencies"])

    lines.extend(["", "## Risk Signals"])
    for item in context["risk"]:
        flags = ", ".join(item["flags"]) if item["flags"] else "none"
        lines.append(f"- `{item['id']}`: {flags}")

    return "\n".join(lines) + "\n"


def _append_neighbor_section(lines: list[str], groups: dict[str, list[dict[str, Any]]]) -> None:
    if not groups:
        lines.append("none")
        return
    for node, neighbors in groups.items():
        lines.append(f"- `{node}`")
        if not neighbors:
            lines.append("  - none")
            continue
        for neighbor in neighbors[:20]:
            sf = f" [{neighbor['source_file']}]" if neighbor.get("source_file") else ""
            lines.append(f"  - `{neighbor['id']}` ({neighbor['kind']}){sf}")


def _node_summary(g: nx.DiGraph, node: str) -> dict[str, Any]:
    attrs = g.nodes[node]
    return {
        "id": node,
        "name": attrs.get("name", node),
        "kind": attrs.get("kind", "unknown"),
        "source_file": attrs.get("source_file"),
        "community": attrs.get("community"),
        "in_degree": g.in_degree(node),
        "out_degree": g.out_degree(node),
    }


def _neighbors(g: nx.DiGraph, node: str, *, outgoing: bool, limit: int) -> list[dict[str, Any]]:
    ids = sorted(g.successors(node) if outgoing else g.predecessors(node))
    return [_node_summary(g, neighbor) for neighbor in ids[:limit]]


def _reverse_impact(g: nx.DiGraph, start_nodes: list[str], depth: int) -> dict[str, Any]:
    visited: set[str] = set(start_nodes)
    queue: deque[tuple[str, int]] = deque((node, 0) for node in start_nodes)
    impacted_nodes: set[str] = set()
    while queue:
        node, dist = queue.popleft()
        if dist >= depth:
            continue
        for pred in sorted(g.predecessors(node)):
            if pred in visited:
                continue
            visited.add(pred)
            impacted_nodes.add(pred)
            queue.append((pred, dist + 1))

    impacted_files: set[str] = set()
    likely_tests: set[str] = set()
    for node in impacted_nodes.union(start_nodes):
        source_file = g.nodes[node].get("source_file")
        if not source_file:
            continue
        if _is_test_file(source_file):
            likely_tests.add(source_file)
        elif node not in start_nodes:
            impacted_files.add(source_file)

    return {
        "matched_nodes": sorted(start_nodes),
        "impacted_files": sorted(impacted_files),
        "likely_tests": sorted(likely_tests),
        "impacted_node_count": len(impacted_nodes),
    }


def _community_peers(g: nx.DiGraph, start_nodes: list[str], *, limit: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for node in start_nodes:
        community = g.nodes[node].get("community")
        if community is None:
            continue
        peers = [n for n in g.nodes if n != node and g.nodes[n].get("community") == community]
        result[node] = {
            "community": community,
            "peer_count": len(peers),
            "sample": [_node_summary(g, peer) for peer in sorted(peers)[:limit]],
        }
    return result


def _risk_summary(g: nx.DiGraph, node: str) -> dict[str, Any]:
    in_degree = g.in_degree(node)
    out_degree = g.out_degree(node)
    flags: list[str] = []
    if in_degree >= 10:
        flags.append("high fan-in: many files depend on this")
    if out_degree >= 25:
        flags.append("high fan-out: broad dependency surface")
    if in_degree == 0:
        flags.append("no direct dependents found")
    if out_degree == 0:
        flags.append("no direct dependencies found")
    return {
        "id": node,
        "in_degree": in_degree,
        "out_degree": out_degree,
        "flags": flags,
    }


def _is_test_file(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return "/tests/" in f"/{normalized}" or normalized.endswith(_TEST_SUFFIXES)
