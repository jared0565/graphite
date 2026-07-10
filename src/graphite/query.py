"""Simple query engine over the graph."""
from __future__ import annotations

import re
from collections import deque
from typing import Any

import networkx as nx


_CALL_RELATIONS: frozenset[str] = frozenset({"calls", "references"})


def query(g: nx.DiGraph, q: str) -> dict[str, Any]:
    """Answer a simple query string.

    Supported patterns:
    - depends-on <node>         : nodes that <node> directly depends on (out-edges)
    - imported-by <node>          : nodes that directly point to <node> (in-edges)
    - callers <symbol>            : functions that call <symbol> (calls/references in-edges)
    - calls <symbol> (callees)    : functions/symbols that <symbol> calls (calls/references out-edges)
    - reaches <a> -> <b>          : directed path from a to b over call/reference edges only
    - path <a> -> <b>             : shortest directed path from a to b
    - community-of <node>       : cluster/community label for a node
    - stats                       : basic graph statistics
    """
    tokens = q.strip().lower().split()
    if not tokens:
        return {"error": "empty query"}

    verb = tokens[0]

    if verb in ("callers", "called-by", "called_by"):
        node_id = _find_node(g, " ".join(tokens[1:]))
        if not node_id:
            return _not_found(g, " ".join(tokens[1:]))
        callers = [
            _node_view(g, p)
            for p in sorted(g.predecessors(node_id))
            if g[p][node_id].get("relation") in _CALL_RELATIONS
        ]
        return {"node": node_id, "count": len(callers), "callers": callers}

    if verb in ("calls", "callees"):
        node_id = _find_node(g, " ".join(tokens[1:]))
        if not node_id:
            return _not_found(g, " ".join(tokens[1:]))
        callees = [
            _node_view(g, s)
            for s in sorted(g.successors(node_id))
            if g[node_id][s].get("relation") in _CALL_RELATIONS
        ]
        return {"node": node_id, "count": len(callees), "calls": callees}

    if verb == "reaches":
        # reaches <a> -> <b> over call/reference edges only.
        try:
            arrow = tokens.index("->")
            a = " ".join(tokens[1:arrow])
            b = " ".join(tokens[arrow + 1 :])
        except ValueError:
            return {"error": "reaches query format: reaches <a> -> <b>"}
        src = _find_node(g, a)
        dst = _find_node(g, b)
        if not src:
            return _not_found(g, a, label="source")
        if not dst:
            return _not_found(g, b, label="target")
        p = _restricted_call_path(g, src, dst)
        if p is None:
            return {"error": f"no call path from {src} to {dst}"}
        return {
            "source": src,
            "target": dst,
            "length": len(p) - 1,
            "path": [_node_view(g, n) for n in p],
        }

    if verb == "stats":
        kinds: dict[str, int] = {}
        for _n, data in g.nodes(data=True):
            kind = data.get("kind", "unknown")
            kinds[kind] = kinds.get(kind, 0) + 1
        relations: dict[str, int] = {}
        for _u, _v, data in g.edges(data=True):
            relation = data.get("relation", "unknown")
            relations[relation] = relations.get(relation, 0) + 1
        communities = {
            data.get("community") for _n, data in g.nodes(data=True) if data.get("community") is not None
        }
        top_in = sorted(g.in_degree(), key=lambda item: item[1], reverse=True)[:5]
        top_out = sorted(g.out_degree(), key=lambda item: item[1], reverse=True)[:5]
        return {
            "node_count": g.number_of_nodes(),
            "edge_count": g.number_of_edges(),
            "density": nx.density(g),
            "community_count": len(communities),
            "nodes_by_kind": dict(sorted(kinds.items(), key=lambda item: item[1], reverse=True)),
            "edges_by_relation": dict(sorted(relations.items(), key=lambda item: item[1], reverse=True)),
            "top_incoming": [{**_node_view(g, n), "in_degree": d} for n, d in top_in],
            "top_outgoing": [{**_node_view(g, n), "out_degree": d} for n, d in top_out],
        }

    if verb in ("depends-on", "depends_on", "out"):
        node_id = _find_node(g, " ".join(tokens[1:]))
        if not node_id:
            return _not_found(g, " ".join(tokens[1:]))
        neighbors = sorted(g.successors(node_id))
        return {
            "node": node_id,
            "count": len(neighbors),
            "depends_on": [
                {"id": n, "name": g.nodes[n].get("name", n), "kind": g.nodes[n].get("kind", "unknown")}
                for n in neighbors
            ],
        }

    if verb in ("imported-by", "imported_by", "in"):
        node_id = _find_node(g, " ".join(tokens[1:]))
        if not node_id:
            return _not_found(g, " ".join(tokens[1:]))
        neighbors = sorted(g.predecessors(node_id))
        return {
            "node": node_id,
            "count": len(neighbors),
            "imported_by": [
                {"id": n, "name": g.nodes[n].get("name", n), "kind": g.nodes[n].get("kind", "unknown")}
                for n in neighbors
            ],
        }

    if verb == "path":
        # path <a> -> <b>
        try:
            arrow = tokens.index("->")
            a = " ".join(tokens[1:arrow])
            b = " ".join(tokens[arrow + 1 :])
        except ValueError:
            return {"error": "path query format: path <a> -> <b>"}
        src = _find_node(g, a)
        dst = _find_node(g, b)
        if not src:
            return _not_found(g, a, label="source")
        if not dst:
            return _not_found(g, b, label="target")
        try:
            p = nx.shortest_path(g, src, dst)
        except nx.NetworkXNoPath:
            return {"error": f"no path from {src} to {dst}"}
        return {
            "source": src,
            "target": dst,
            "length": len(p) - 1,
            "path": [
                {"id": n, "name": g.nodes[n].get("name", n), "kind": g.nodes[n].get("kind", "unknown")}
                for n in p
            ],
        }

    if verb in ("community-of", "community_of"):
        node_id = _find_node(g, " ".join(tokens[1:]))
        if not node_id:
            return _not_found(g, " ".join(tokens[1:]))
        return {
            "node": node_id,
            "community": g.nodes[node_id].get("community"),
            "name": g.nodes[node_id].get("name", node_id),
        }

    return {"error": f"unknown query verb: {verb}"}


def _not_found(g: nx.DiGraph, token: str, *, label: str = "node") -> dict[str, Any]:
    """Not-found error with close candidates so agents can self-correct."""
    return {
        "error": f"{label} not found: {token}",
        "candidates": _candidates(g, token),
    }


def _candidates(g: nx.DiGraph, token: str, limit: int = 5) -> list[dict[str, Any]]:
    """Nodes whose id, name, or source file loosely matches any part of token."""
    token = token.strip().lower().strip("`")
    parts = [p for p in re.split(r"[^a-z0-9]+", token) if len(p) >= 3]
    if not parts:
        return []
    scored: list[tuple[int, str]] = []
    for n in g.nodes():
        haystack = " ".join(
            (n, g.nodes[n].get("name", ""), g.nodes[n].get("source_file", ""))
        ).lower().replace("\\", "/")
        score = sum(1 for p in parts if p in haystack)
        if score:
            scored.append((score, n))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [_node_view(g, n) for _score, n in scored[:limit]]


def _find_node(g: nx.DiGraph, token: str) -> str | None:
    """Match a node by exact id, name, or file path."""
    token = token.strip().lower().strip("`")
    # Exact id.
    if token in g:
        return token
    # Match by name.
    for n in g.nodes():
        if g.nodes[n].get("name", "").lower() == token:
            return n
    # Match by source_file suffix.
    for n in g.nodes():
        sf = g.nodes[n].get("source_file", "")
        if sf and sf.lower().endswith(token):
            return n
    # Fuzzy contains.
    for n in g.nodes():
        if token in n:
            return n
    return None


def _node_view(g: nx.DiGraph, n: str) -> dict[str, Any]:
    """Compact node descriptor for call-graph results."""
    data = g.nodes[n]
    return {
        "id": n,
        "name": data.get("name", n),
        "kind": data.get("kind", "unknown"),
        "source_file": data.get("source_file", ""),
    }


def _restricted_call_path(g: nx.DiGraph, src: str, dst: str) -> list[str] | None:
    """Shortest directed path from src to dst following only call/reference edges."""
    if src == dst:
        return [src]
    prev: dict[str, str | None] = {src: None}
    queue: deque[str] = deque([src])
    while queue:
        u = queue.popleft()
        for v in sorted(g.successors(u)):
            if v in prev:
                continue
            if g[u][v].get("relation") not in _CALL_RELATIONS:
                continue
            prev[v] = u
            if v == dst:
                path = [v]
                while prev[path[-1]] is not None:
                    path.append(prev[path[-1]])  # type: ignore[arg-type]
                path.reverse()
                return path
            queue.append(v)
    return None


def annotate_communities(g: nx.DiGraph, partition: dict[str, int]) -> None:
    """Write community id into each graph node attribute."""
    for n, comm in partition.items():
        if n in g.nodes:
            g.nodes[n]["community"] = comm
