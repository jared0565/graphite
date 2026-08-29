"""Graph analysis: god nodes, orphans, entry points, cycles."""
from __future__ import annotations

from typing import Any

import networkx as nx

from .health import resolution_health

_PROJECT_NODE_KINDS = frozenset({"file", "function", "class"})

#: The cycle report is bounded, and the bound is part of the answer (#64).
#: Longest cycle length the search enumerates. Cycles are reported shortest
#: first, so anything longer than this could only appear when a repository
#: has fewer than `top_n` cycles of length <= 8 -- and then the report says
#: how far the search got.
CYCLE_LENGTH_BOUND = 8
#: Total cycles the search may examine, across every component and length
#: level, before it stops and marks the answer as budget-bounded. Django 5.2's
#: project graph has more simple cycles than there are atoms in a mole; the
#: unbounded enumeration it replaces took 13 GB and did not finish.
CYCLE_SEARCH_BUDGET = 10_000


def _is_project_node(g: nx.DiGraph, node: str) -> bool:
    """Return true for nodes extracted from repository-owned source files."""
    return g.nodes[node].get("kind") in _PROJECT_NODE_KINDS


def _project_nodes(g: nx.DiGraph) -> list[str]:
    return [n for n in g.nodes() if _is_project_node(g, n)]


def _canonical_cycle(cycle: list[str]) -> list[str]:
    """One spelling per cycle: rotated to start at its smallest node.

    A cycle is the same cycle under rotation, so `[a, b, c]` and `[b, c, a]`
    would otherwise both appear depending on where the traversal entered.
    Nodes within a simple cycle are distinct, so the minimum is unique.
    """
    pivot = cycle.index(min(cycle))
    return cycle[pivot:] + cycle[:pivot]


def _cyclic_components(g: nx.DiGraph) -> list[list[str]]:
    """Strongly connected components that contain a cycle, smallest node first.

    Every simple cycle lies inside one strongly connected component, and
    finding the components is linear -- so this is where a search that must
    stay bounded starts. A single node counts only when it has a self-loop.
    """
    components: list[list[str]] = []
    for component in nx.strongly_connected_components(g):
        nodes = sorted(component)
        if len(nodes) > 1 or g.has_edge(nodes[0], nodes[0]):
            components.append(nodes)
    components.sort(key=lambda nodes: nodes[0])
    return components


def _ordered_subgraph(g: nx.DiGraph, nodes: list[str]) -> nx.DiGraph:
    """A copy whose adjacency iterates in sorted order, so what the search
    examines first -- which decides the answer once the budget bites -- is the
    same in every process, not whatever `PYTHONHASHSEED` made of a set."""
    sub = nx.DiGraph()
    sub.add_nodes_from(nodes)
    sub.add_edges_from(sorted((u, v) for u, v in g.subgraph(nodes).edges()))
    return sub


def _stable_cycles(g: nx.DiGraph, top_n: int) -> tuple[list[list[str]], dict[str, Any]]:
    """The `top_n` shortest cycles, found by a search that cannot run away.

    Cycles are enumerated one LENGTH LEVEL at a time -- every cycle of length
    1, then 2, then 3 ... up to `CYCLE_LENGTH_BOUND` -- and the search stops as
    soon as a completed level has produced at least `top_n` cycles. A completed
    level is exact: nothing shorter was missed. The whole search is also capped
    at `CYCLE_SEARCH_BUDGET` examined cycles, after which the answer is the best
    of what was examined and says so.

    This replaces `nx.simple_cycles(g)` with no bound, which enumerates every
    simple cycle of the graph before anything is sorted or truncated. On a
    repository whose test tree and package import each other densely (Django
    5.2, 2,930 sources) that enumeration is exponential in the size of the
    strongly connected component: 13 GB and over thirty minutes, unfinished
    (#64). Each half of that repository alone reported cycles in seconds.

    Ordering is fixed so two builds of one commit agree (`test_determinism`):
    components and their adjacency are visited in sorted order, each cycle is
    rotated to its smallest node, and the result is sorted shortest first,
    then lexicographically, so truncation keeps the tightest cycles.
    """
    components = _cyclic_components(g)
    seen: set[tuple[str, ...]] = set()
    canonical: list[list[str]] = []
    examined = 0
    budget_exhausted = False
    complete_through_length = 0
    subgraphs = [_ordered_subgraph(g, nodes) for nodes in components]
    for length in range(1, CYCLE_LENGTH_BOUND + 1):
        for sub in subgraphs:
            for cycle in nx.simple_cycles(sub, length_bound=length):
                examined += 1
                if len(cycle) == length:
                    key = tuple(_canonical_cycle(cycle))
                    if key not in seen:
                        seen.add(key)
                        canonical.append(list(key))
                if examined >= CYCLE_SEARCH_BUDGET:
                    budget_exhausted = True
                    break
            if budget_exhausted:
                break
        if budget_exhausted:
            break
        complete_through_length = length
        if len(canonical) >= top_n:
            break
    canonical.sort(key=lambda cycle: (len(cycle), cycle))
    search: dict[str, Any] = {
        "length_bound": CYCLE_LENGTH_BOUND,
        "budget": CYCLE_SEARCH_BUDGET,
        "examined": examined,
        "budget_exhausted": budget_exhausted,
        "complete_through_length": complete_through_length,
        "cyclic_components": len(components),
        "largest_component": max((len(nodes) for nodes in components), default=0),
    }
    return canonical[:top_n], search


def analyze(g: nx.DiGraph, top_n: int = 20) -> dict[str, Any]:
    """Run the full analysis suite."""
    project_subgraph = g.subgraph(_project_nodes(g)).copy()
    cycles, cycle_search = _stable_cycles(project_subgraph, top_n)
    return {
        "god_nodes": god_nodes(g, top_n),
        "orphans": orphan_nodes(g, top_n),
        "entry_points": entry_points(g, top_n),
        "surprising_connections": surprising_connections(g, top_n),
        "cycles": cycles,
        "cycle_search": cycle_search,
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
