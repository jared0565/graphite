"""Simple query engine over the graph."""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import networkx as nx

from .graph import edge_relations

from .answer_contract import GRADE_INCONCLUSIVE, build_answer_block, languages_for_nodes
from .health import resolution_health
from .query_plan import DEFAULT_MAX_DEPTH, DEFAULT_MAX_RESULTS, make_plan, plan_error


_CALL_RELATIONS: frozenset[str] = frozenset({"calls", "references"})


def _attach_resolution(result: dict[str, Any], g: nx.DiGraph) -> dict[str, Any]:
    """Trust signal + honest-empty marker for relation listings (spec 2026-07-25)."""
    health = resolution_health(g)
    result["resolution_health"] = health
    result["inconclusive"] = result.get("total", 0) == 0 and not health["healthy"]
    return result


def _capped_edge_listing(
    g: nx.DiGraph, token: str, options: dict[str, Any], *, key: str, incoming: bool
) -> dict[str, Any]:
    """Call/reference in- or out-edge listing, bounded by max_results."""
    cap = int(options.get("max_results", DEFAULT_MAX_RESULTS))
    detail = _find_node_detail(g, token)
    if not detail:
        return _not_found(g, token)
    node_id = detail[0]
    if incoming:
        full = [
            p for p in sorted(g.predecessors(node_id))
            if any(r in _CALL_RELATIONS for r in edge_relations(g[p][node_id]))
        ]
    else:
        full = [
            s for s in sorted(g.successors(node_id))
            if any(r in _CALL_RELATIONS for r in edge_relations(g[node_id][s]))
        ]
    shown = full[:cap]
    return _attach_resolution({
        "node": node_id,
        "match": _match_meta(token, detail),
        "count": len(shown),
        "total": len(full),
        "truncated": len(full) > cap,
        "limits": {"max_results": cap},
        key: [_node_view(g, n) for n in shown],
    }, g)


def _verb_callers(g: nx.DiGraph, inputs: list[str], options: dict[str, Any]) -> dict[str, Any]:
    return _capped_edge_listing(g, inputs[0], options, key="callers", incoming=True)


def _verb_calls(g: nx.DiGraph, inputs: list[str], options: dict[str, Any]) -> dict[str, Any]:
    return _capped_edge_listing(g, inputs[0], options, key="calls", incoming=False)


def _verb_reaches(g: nx.DiGraph, inputs: list[str], options: dict[str, Any]) -> dict[str, Any]:
    max_depth = int(options.get("max_depth", DEFAULT_MAX_DEPTH))
    a, b = inputs
    src_detail = _find_node_detail(g, a)
    dst_detail = _find_node_detail(g, b)
    if not src_detail:
        return _not_found(g, a, label="source")
    if not dst_detail:
        return _not_found(g, b, label="target")
    src, dst = src_detail[0], dst_detail[0]
    p, limited = _bounded_bfs_path(g, src, dst, max_depth, relations=_CALL_RELATIONS)
    if p is None:
        return {
            "error": f"no call path from {src} to {dst}",
            "error_code": "no_path",
            "truncated": limited,
            "limits": {"max_depth": max_depth},
        }
    return {
        "source": src,
        "target": dst,
        "match": {"source": _match_meta(a, src_detail), "target": _match_meta(b, dst_detail)},
        "length": len(p) - 1,
        "path": [_node_view(g, n) for n in p],
        "truncated": False,
        "limits": {"max_depth": max_depth},
    }


def _verb_stats(g: nx.DiGraph, inputs: list[str], options: dict[str, Any]) -> dict[str, Any]:
    del inputs, options
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
        "resolution_health": resolution_health(g),
    }


def _neighbor_listing(
    g: nx.DiGraph, token: str, options: dict[str, Any], *, key: str, incoming: bool
) -> dict[str, Any]:
    cap = int(options.get("max_results", DEFAULT_MAX_RESULTS))
    detail = _find_node_detail(g, token)
    if not detail:
        return _not_found(g, token)
    node_id = detail[0]
    neighbors = sorted(g.predecessors(node_id) if incoming else g.successors(node_id))
    shown = neighbors[:cap]
    return _attach_resolution({
        "node": node_id,
        "match": _match_meta(token, detail),
        "count": len(shown),
        "total": len(neighbors),
        "truncated": len(neighbors) > cap,
        "limits": {"max_results": cap},
        key: [_node_view(g, n) for n in shown],
    }, g)


def _verb_depends_on(g: nx.DiGraph, inputs: list[str], options: dict[str, Any]) -> dict[str, Any]:
    return _neighbor_listing(g, inputs[0], options, key="depends_on", incoming=False)


def _verb_imported_by(g: nx.DiGraph, inputs: list[str], options: dict[str, Any]) -> dict[str, Any]:
    return _neighbor_listing(g, inputs[0], options, key="imported_by", incoming=True)


def _verb_path(g: nx.DiGraph, inputs: list[str], options: dict[str, Any]) -> dict[str, Any]:
    max_depth = int(options.get("max_depth", DEFAULT_MAX_DEPTH))
    a, b = inputs
    src_detail = _find_node_detail(g, a)
    dst_detail = _find_node_detail(g, b)
    if not src_detail:
        return _not_found(g, a, label="source")
    if not dst_detail:
        return _not_found(g, b, label="target")
    src, dst = src_detail[0], dst_detail[0]
    p, limited = _bounded_bfs_path(g, src, dst, max_depth, relations=None)
    if p is None:
        return {
            "error": f"no path from {src} to {dst}",
            "error_code": "no_path",
            "truncated": limited,
            "limits": {"max_depth": max_depth},
        }
    return {
        "source": src,
        "target": dst,
        "match": {"source": _match_meta(a, src_detail), "target": _match_meta(b, dst_detail)},
        "length": len(p) - 1,
        "path": [
            {"id": n, "name": g.nodes[n].get("name", n), "kind": g.nodes[n].get("kind", "unknown")}
            for n in p
        ],
        "truncated": False,
        "limits": {"max_depth": max_depth},
    }


def _verb_community_of(g: nx.DiGraph, inputs: list[str], options: dict[str, Any]) -> dict[str, Any]:
    del options
    token = inputs[0]
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
    """One dispatchable query verb; the registry drives dispatch, help, plans, and capabilities."""

    name: str
    aliases: tuple[str, ...]
    arguments: str
    description: str
    handler: Callable[[nx.DiGraph, list[str], dict[str, Any]], dict[str, Any]]
    roles: tuple[str, ...]
    limits: tuple[tuple[str, int], ...] = ()
    relations: tuple[str, ...] = ()
    empty_meaning: str = ""


QUERY_VERBS: tuple[QueryVerb, ...] = (
    QueryVerb(
        "callers", ("called-by", "called_by"), "<symbol>",
        "Functions that call <symbol> (calls/references in-edges)", _verb_callers,
        ("node",), (("max_results", DEFAULT_MAX_RESULTS),),
        relations=("calls",), empty_meaning="no bound callers found",
    ),
    QueryVerb(
        "calls", ("callees",), "<symbol>",
        "Functions/symbols that <symbol> calls (calls/references out-edges)", _verb_calls,
        ("node",), (("max_results", DEFAULT_MAX_RESULTS),),
        relations=("calls",), empty_meaning="no bound callees found",
    ),
    QueryVerb(
        "reaches", (), "<a> -> <b>",
        "Directed path from a to b over call/reference edges only", _verb_reaches,
        ("source", "target"), (("max_depth", DEFAULT_MAX_DEPTH),),
        relations=("calls",), empty_meaning="no call path found within depth",
    ),
    QueryVerb(
        "path", (), "<a> -> <b>",
        "Shortest directed path from a to b over all edges", _verb_path,
        ("source", "target"), (("max_depth", DEFAULT_MAX_DEPTH),),
        relations=("calls", "imports"), empty_meaning="no path found within depth",
    ),
    QueryVerb(
        "depends-on", ("depends_on", "out"), "<node>",
        "Nodes that <node> directly depends on (out-edges)", _verb_depends_on,
        ("node",), (("max_results", DEFAULT_MAX_RESULTS),),
        relations=("calls", "imports"), empty_meaning="no bound dependencies found",
    ),
    QueryVerb(
        "imported-by", ("imported_by", "in"), "<node>",
        "Nodes that directly point to <node> (in-edges)", _verb_imported_by,
        ("node",), (("max_results", DEFAULT_MAX_RESULTS),),
        relations=("imports",), empty_meaning="no bound importers found",
    ),
    QueryVerb(
        "community-of", ("community_of",), "<node>",
        "Cluster/community label for a node", _verb_community_of,
        ("node",),
        relations=("calls", "imports"), empty_meaning="no community assigned",
    ),
    QueryVerb("stats", (), "", "Basic graph statistics", _verb_stats, ()),
)

_VERB_INDEX: dict[str, QueryVerb] = {
    alias: spec for spec in QUERY_VERBS for alias in (spec.name, *spec.aliases)
}

_EXPECTED_ROLES: dict[str, tuple[str, ...]] = {spec.name: spec.roles for spec in QUERY_VERBS}


def verb_catalog() -> list[dict[str, Any]]:
    """Machine-readable listing of every dispatchable query verb."""
    return [
        {
            "name": spec.name,
            "aliases": list(spec.aliases),
            "arguments": spec.arguments,
            "description": spec.description,
            "targets": list(spec.roles),
            "limits": dict(spec.limits),
        }
        for spec in QUERY_VERBS
    ]


def _format_error(spec: QueryVerb) -> dict[str, Any]:
    return {
        "error": f"{spec.name} query format: {spec.name} {spec.arguments}",
        "error_code": "invalid_query_format",
    }


def build_plan(q: str) -> dict[str, Any]:
    """Deterministically translate a query string into a plan document.

    Returns either a plan (see query_plan.PLAN_SCHEMA) or an error dict with a
    stable error_code; plans never carry an "error" key, so callers distinguish
    the two by that key alone.
    """
    tokens = q.strip().lower().split()
    if not tokens:
        return {"error": "empty query", "error_code": "empty_query"}

    verb = tokens[0]
    spec = _VERB_INDEX.get(verb)
    if spec is None:
        return {
            "error": f"unknown query verb: {verb}",
            "error_code": "unknown_query_verb",
            "suggestions": [
                'Use `graphite search "<symbol, path, or concept>"` to locate nodes',
                "Run `graphite capabilities --json` to list supported operations",
                f'Supported verbs: {", ".join(v.name for v in QUERY_VERBS)}',
            ],
        }
    rest = tokens[1:]
    if spec.roles == ():
        targets: list[tuple[str, str]] = []
    elif spec.roles == ("node",):
        token = " ".join(rest)
        if not token:
            return _format_error(spec)
        targets = [("node", token)]
    else:
        try:
            arrow = rest.index("->")
        except ValueError:
            return _format_error(spec)
        a, b = " ".join(rest[:arrow]), " ".join(rest[arrow + 1 :])
        if not a or not b:
            return _format_error(spec)
        targets = [("source", a), ("target", b)]
    return make_plan(spec.name, targets, dict(spec.limits))


RESULT_SCHEMA_VERSION = 1


def _resolution(spec: QueryVerb, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Uniform per-target resolution listing, aligned with the plan's target roles."""
    if spec.roles == ("node",):
        return [{"role": "node", **result["match"]}]
    if spec.roles == ("source", "target"):
        return [
            {"role": "source", **result["match"]["source"]},
            {"role": "target", **result["match"]["target"]},
        ]
    return []


def _is_empty(spec: QueryVerb, result: dict[str, Any]) -> bool:
    if result.get("error_code") == "no_path":
        return True
    if "total" in result:
        return result["total"] == 0
    if spec.name == "community-of":
        return result.get("community") is None
    return False


def execute_plan(g: nx.DiGraph, plan: object) -> dict[str, Any]:
    """Validate a plan against schema v1 and the verb registry, then run it."""
    reason = plan_error(plan, _EXPECTED_ROLES)
    if reason is not None:
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "error": f"invalid query plan: {reason}",
            "error_code": "invalid_plan",
        }
    assert isinstance(plan, dict)  # narrowed by plan_error
    spec = _VERB_INDEX[plan["operation"]]
    inputs = [target["input"] for target in plan["targets"]]
    result = spec.handler(g, inputs, plan["options"])
    envelope = {"schema_version": RESULT_SCHEMA_VERSION, **result}
    is_error = "error" in result
    if not is_error:
        envelope["resolution"] = _resolution(spec, result)
    if spec.relations and (not is_error or result.get("error_code") == "no_path"):
        # Fail-open (spec R6): computation performed only to build the
        # `answer` block — seed derivation, language lookup, the block
        # itself — must never be able to error the query. On any failure
        # here the envelope is left exactly as it was before this block.
        try:
            seeds = [entry.get("node") for entry in envelope.get("resolution", [])]
            matched_languages = languages_for_nodes(g, seeds)
            block = build_answer_block(
                g,
                relations=spec.relations,
                languages=matched_languages,
                total=0 if _is_empty(spec, result) else 1,
                empty_meaning=spec.empty_meaning or None,
            )
            if block is not None:
                envelope["answer"] = block
                if "inconclusive" in envelope:
                    envelope["inconclusive"] = block["grade"] == GRADE_INCONCLUSIVE
            elif seeds and not matched_languages and "inconclusive" in envelope:
                # Matched real nodes, but none have an applicable code
                # language -- nothing to grade, not a resolution gap.
                envelope["inconclusive"] = False
        except Exception:
            pass
    return envelope


def plan_preview(q: str) -> dict[str, Any]:
    """Validated plan document for a query string, or an error dict.

    Needs no graph: this is the `query --plan-only` path, letting agents check
    query syntax offline before paying for a graph load.
    """
    plan = build_plan(q)
    if "error" in plan:
        return {"schema_version": RESULT_SCHEMA_VERSION, **plan}
    reason = plan_error(plan, _EXPECTED_ROLES)
    if reason is not None:  # defensive: internally built plans always validate
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "error": f"invalid query plan: {reason}",
            "error_code": "invalid_plan",
        }
    return plan


def query(g: nx.DiGraph, q: str) -> dict[str, Any]:
    """Answer a simple query string; see QUERY_VERBS for the supported patterns.

    Every query is first translated into a canonical plan (build_plan) and then
    executed (execute_plan); errors at either stage carry a stable error_code,
    and every response carries schema_version (successes also carry a uniform
    per-target resolution listing).
    """
    plan = build_plan(q)
    if "error" in plan:
        return {"schema_version": RESULT_SCHEMA_VERSION, **plan}
    return execute_plan(g, plan)


DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100
# Deterministic precedence tiers; index = rank, lower is better.
_SEARCH_MATCH_TYPES = ("exact-id", "name", "path-suffix", "id-substring", "name-substring", "tokens")


def search_graph(g: nx.DiGraph, text: str, limit: int = DEFAULT_SEARCH_LIMIT) -> dict[str, Any]:
    """Deterministic ranked node search by id, name, path, or concept tokens."""
    raw = text.strip()
    token = raw.lower().strip("`")
    if not token:
        return {
            "ok": False,
            "schema_version": 1,
            "error": "empty search",
            "error_code": "empty_search",
        }
    limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    normalized_path = token.replace("\\", "/")
    parts = [p for p in re.split(r"[^a-z0-9]+", token) if len(p) >= 3]
    best: dict[str, tuple[int, float]] = {}

    def offer(node: str, tier: int, score: float) -> None:
        current = best.get(node)
        if current is None or (tier, -score) < (current[0], -current[1]):
            best[node] = (tier, score)

    for n, data in g.nodes(data=True):
        node_lower = n.lower()
        name = data.get("name", "").lower()
        source = data.get("source_file", "").lower().replace("\\", "/")
        if node_lower == token:
            offer(n, 0, 1.0)
        if name and name == token:
            offer(n, 1, 1.0)
        if source and source.endswith(normalized_path):
            offer(n, 2, 1.0 if data.get("kind") == "file" else 0.9)
        if token in node_lower and node_lower != token:
            offer(n, 3, round(len(token) / len(n), 3))
        if name and token in name and name != token:
            offer(n, 4, round(len(token) / len(name), 3))
        if parts:
            haystack = f"{node_lower} {name} {source}"
            hits = sum(1 for p in parts if p in haystack)
            if hits:
                offer(n, 5, round(hits / len(parts), 3))

    ordered = sorted(best.items(), key=lambda item: (item[1][0], -item[1][1], item[0]))
    results = [
        {**_node_view(g, node), "match_type": _SEARCH_MATCH_TYPES[tier], "score": score}
        for node, (tier, score) in ordered[:limit]
    ]
    return {
        "ok": True,
        "schema_version": 1,
        "query": raw,
        "count": len(results),
        "total_matches": len(ordered),
        "truncated": len(ordered) > limit,
        "results": results,
    }


def _not_found(g: nx.DiGraph, token: str, *, label: str = "node") -> dict[str, Any]:
    """Not-found error with close candidates so agents can self-correct."""
    return {
        "error": f"{label} not found: {token}",
        "error_code": "node_not_found",
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
            (n, g.nodes[n].get("name", "") or "", g.nodes[n].get("source_file", "") or "")
        ).lower().replace("\\", "/")
        score = sum(1 for p in parts if p in haystack)
        if score:
            scored.append((score, n))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [_node_view(g, n) for _score, n in scored[:limit]]


def _path_depth(g: nx.DiGraph, node_id: str) -> int:
    """Number of directory segments in a node's source file (0 = repo root)."""
    source_file = g.nodes[node_id].get("source_file") or ""
    normalized = source_file.replace("\\", "/").strip("/")
    return normalized.count("/") if normalized else 0


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

    # Multiple files can share a basename (README.md at root and under
    # hooks/, policy/, etc.) -- prefer the shallowest path, since a bare
    # basename query with no path segments almost always means the
    # repo-root file, not whichever id happened to sort first alphabetically
    # (found via operation-firewall dogfooding, 2026-07-31: `README.md`
    # matched `hooks/README.md` over the root file purely because
    # "hooks_readme" < "readme" as strings).
    name_hits = sorted(
        (n for n in g.nodes() if g.nodes[n].get("name", "").lower() == token),
        key=lambda n: (_path_depth(g, n), n),
    )
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


def _bounded_bfs_path(
    g: nx.DiGraph, src: str, dst: str, max_depth: int, *, relations: frozenset[str] | None
) -> tuple[list[str] | None, bool]:
    """Shortest directed path from src to dst within max_depth edges.

    Successors are visited in sorted order, so equal-length ties break
    deterministically. The second value reports whether the depth bound pruned
    any expansion — True means a longer path may exist beyond the bound, False
    means absence is proven.
    """
    if src == dst:
        return [src], False
    prev: dict[str, str | None] = {src: None}
    depth: dict[str, int] = {src: 0}
    queue: deque[str] = deque([src])
    limited = False
    while queue:
        u = queue.popleft()
        for v in sorted(g.successors(u)):
            if relations is not None and not any(r in relations for r in edge_relations(g[u][v])):
                continue
            if v in prev:
                continue
            if depth[u] >= max_depth:
                limited = True
                continue
            prev[v] = u
            depth[v] = depth[u] + 1
            if v == dst:
                path = [v]
                while prev[path[-1]] is not None:
                    path.append(prev[path[-1]])
                path.reverse()
                return path, False
            queue.append(v)
    return None, limited


def annotate_communities(g: nx.DiGraph, partition: dict[str, int]) -> None:
    """Write community id into each graph node attribute."""
    for n, comm in partition.items():
        if n in g.nodes:
            g.nodes[n]["community"] = comm
