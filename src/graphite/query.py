"""Simple query engine over the graph."""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import networkx as nx


_CALL_RELATIONS: frozenset[str] = frozenset({"calls", "references"})


def _verb_callers(g: nx.DiGraph, rest: list[str]) -> dict[str, Any]:
    token = " ".join(rest)
    detail = _find_node_detail(g, token)
    if not detail:
        return _not_found(g, token)
    node_id = detail[0]
    callers = [
        _node_view(g, p)
        for p in sorted(g.predecessors(node_id))
        if g[p][node_id].get("relation") in _CALL_RELATIONS
    ]
    return {"node": node_id, "match": _match_meta(token, detail), "count": len(callers), "callers": callers}


def _verb_calls(g: nx.DiGraph, rest: list[str]) -> dict[str, Any]:
    token = " ".join(rest)
    detail = _find_node_detail(g, token)
    if not detail:
        return _not_found(g, token)
    node_id = detail[0]
    callees = [
        _node_view(g, s)
        for s in sorted(g.successors(node_id))
        if g[node_id][s].get("relation") in _CALL_RELATIONS
    ]
    return {"node": node_id, "match": _match_meta(token, detail), "count": len(callees), "calls": callees}


def _split_arrow(verb: str, rest: list[str]) -> tuple[str, str] | dict[str, Any]:
    try:
        arrow = rest.index("->")
    except ValueError:
        return {"error": f"{verb} query format: {verb} <a> -> <b>"}
    return " ".join(rest[:arrow]), " ".join(rest[arrow + 1 :])


def _verb_reaches(g: nx.DiGraph, rest: list[str]) -> dict[str, Any]:
    split = _split_arrow("reaches", rest)
    if isinstance(split, dict):
        return split
    a, b = split
    src_detail = _find_node_detail(g, a)
    dst_detail = _find_node_detail(g, b)
    if not src_detail:
        return _not_found(g, a, label="source")
    if not dst_detail:
        return _not_found(g, b, label="target")
    src, dst = src_detail[0], dst_detail[0]
    p = _restricted_call_path(g, src, dst)
    if p is None:
        return {"error": f"no call path from {src} to {dst}"}
    return {
        "source": src,
        "target": dst,
        "match": {"source": _match_meta(a, src_detail), "target": _match_meta(b, dst_detail)},
        "length": len(p) - 1,
        "path": [_node_view(g, n) for n in p],
    }


def _verb_stats(g: nx.DiGraph, rest: list[str]) -> dict[str, Any]:
    del rest
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


def _neighbor_listing(g: nx.DiGraph, rest: list[str], *, key: str, incoming: bool) -> dict[str, Any]:
    token = " ".join(rest)
    detail = _find_node_detail(g, token)
    if not detail:
        return _not_found(g, token)
    node_id = detail[0]
    neighbors = sorted(g.predecessors(node_id) if incoming else g.successors(node_id))
    return {
        "node": node_id,
        "match": _match_meta(token, detail),
        "count": len(neighbors),
        key: [
            {"id": n, "name": g.nodes[n].get("name", n), "kind": g.nodes[n].get("kind", "unknown")}
            for n in neighbors
        ],
    }


def _verb_depends_on(g: nx.DiGraph, rest: list[str]) -> dict[str, Any]:
    return _neighbor_listing(g, rest, key="depends_on", incoming=False)


def _verb_imported_by(g: nx.DiGraph, rest: list[str]) -> dict[str, Any]:
    return _neighbor_listing(g, rest, key="imported_by", incoming=True)


def _verb_path(g: nx.DiGraph, rest: list[str]) -> dict[str, Any]:
    split = _split_arrow("path", rest)
    if isinstance(split, dict):
        return split
    a, b = split
    src_detail = _find_node_detail(g, a)
    dst_detail = _find_node_detail(g, b)
    if not src_detail:
        return _not_found(g, a, label="source")
    if not dst_detail:
        return _not_found(g, b, label="target")
    src, dst = src_detail[0], dst_detail[0]
    try:
        p = nx.shortest_path(g, src, dst)
    except nx.NetworkXNoPath:
        return {"error": f"no path from {src} to {dst}"}
    return {
        "source": src,
        "target": dst,
        "match": {"source": _match_meta(a, src_detail), "target": _match_meta(b, dst_detail)},
        "length": len(p) - 1,
        "path": [
            {"id": n, "name": g.nodes[n].get("name", n), "kind": g.nodes[n].get("kind", "unknown")}
            for n in p
        ],
    }


def _verb_community_of(g: nx.DiGraph, rest: list[str]) -> dict[str, Any]:
    token = " ".join(rest)
    detail = _find_node_detail(g, token)
    if not detail:
        return _not_found(g, token)
    node_id = detail[0]
    return {
        "node": node_id,
        "match": _match_meta(token, detail),
        "community": g.nodes[node_id].get("community"),
        "name": g.nodes[node_id].get("name", node_id),
    }


@dataclass(frozen=True)
class QueryVerb:
    """One dispatchable query verb; the registry drives dispatch, help, and capabilities."""

    name: str
    aliases: tuple[str, ...]
    arguments: str
    description: str
    handler: Callable[[nx.DiGraph, list[str]], dict[str, Any]]


QUERY_VERBS: tuple[QueryVerb, ...] = (
    QueryVerb(
        "callers", ("called-by", "called_by"), "<symbol>",
        "Functions that call <symbol> (calls/references in-edges)", _verb_callers,
    ),
    QueryVerb(
        "calls", ("callees",), "<symbol>",
        "Functions/symbols that <symbol> calls (calls/references out-edges)", _verb_calls,
    ),
    QueryVerb(
        "reaches", (), "<a> -> <b>",
        "Directed path from a to b over call/reference edges only", _verb_reaches,
    ),
    QueryVerb(
        "path", (), "<a> -> <b>",
        "Shortest directed path from a to b over all edges", _verb_path,
    ),
    QueryVerb(
        "depends-on", ("depends_on", "out"), "<node>",
        "Nodes that <node> directly depends on (out-edges)", _verb_depends_on,
    ),
    QueryVerb(
        "imported-by", ("imported_by", "in"), "<node>",
        "Nodes that directly point to <node> (in-edges)", _verb_imported_by,
    ),
    QueryVerb(
        "community-of", ("community_of",), "<node>",
        "Cluster/community label for a node", _verb_community_of,
    ),
    QueryVerb("stats", (), "", "Basic graph statistics", _verb_stats),
)

_VERB_INDEX: dict[str, QueryVerb] = {
    alias: spec for spec in QUERY_VERBS for alias in (spec.name, *spec.aliases)
}


def verb_catalog() -> list[dict[str, Any]]:
    """Machine-readable listing of every dispatchable query verb."""
    return [
        {
            "name": spec.name,
            "aliases": list(spec.aliases),
            "arguments": spec.arguments,
            "description": spec.description,
        }
        for spec in QUERY_VERBS
    ]


def query(g: nx.DiGraph, q: str) -> dict[str, Any]:
    """Answer a simple query string; see QUERY_VERBS for the supported patterns."""
    tokens = q.strip().lower().split()
    if not tokens:
        return {"error": "empty query"}

    verb = tokens[0]
    spec = _VERB_INDEX.get(verb)
    if spec is None:
        return {"error": f"unknown query verb: {verb}"}
    return spec.handler(g, tokens[1:])


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


def _find_node_detail(g: nx.DiGraph, token: str) -> tuple[str, str, list[str]] | None:
    """Match a node and report HOW it matched.

    Returns (node_id, match_type, alternates) where match_type is one of
    "exact-id", "name", "path-suffix", or "fuzzy", and alternates lists other
    nodes that matched equally well (so a silently-wrong pick is visible to the
    caller). Ties are broken deterministically instead of by insertion order.
    """
    token = token.strip().lower().strip("`")
    if token in g:
        return token, "exact-id", []

    name_hits = sorted(n for n in g.nodes() if g.nodes[n].get("name", "").lower() == token)
    if name_hits:
        return name_hits[0], "name", name_hits[1:4]

    normalized = token.replace("\\", "/")
    path_hits = sorted(
        n
        for n in g.nodes()
        if (sf := g.nodes[n].get("source_file", "")) and sf.lower().replace("\\", "/").endswith(normalized)
    )
    if path_hits:
        # Prefer the file node itself over symbols defined in the file.
        file_hits = [n for n in path_hits if g.nodes[n].get("kind") == "file"]
        chosen = file_hits[0] if file_hits else path_hits[0]
        return chosen, "path-suffix", [n for n in path_hits if n != chosen][:4]

    fuzzy_hits = sorted((n for n in g.nodes() if token in n), key=lambda n: (len(n), n))
    if fuzzy_hits:
        return fuzzy_hits[0], "fuzzy", fuzzy_hits[1:4]
    return None


def _find_node(g: nx.DiGraph, token: str) -> str | None:
    """Match a node by exact id, name, or file path."""
    detail = _find_node_detail(g, token)
    return detail[0] if detail else None


def _match_meta(token: str, detail: tuple[str, str, list[str]]) -> dict[str, Any]:
    """Query-response metadata describing how an input token was matched."""
    node_id, match_type, alternates = detail
    meta: dict[str, Any] = {"input": token, "node": node_id, "type": match_type}
    if alternates:
        meta["alternates"] = alternates
    return meta


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
