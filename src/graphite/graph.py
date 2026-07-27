"""Graph construction and normalization."""
from __future__ import annotations

import unicodedata
from typing import Any

import networkx as nx


def normalize_id(value: str) -> str:
    """Stable, portable node ID. Same rules as extract.ast._make_id."""
    text = unicodedata.normalize("NFKC", value)
    cleaned = "".join(c if c.isalnum() or c in ("_", ".") else "_" for c in text)
    cleaned = cleaned.strip("_.")
    cleaned = cleaned.replace(".", "_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.casefold()


def edge_relations(data: dict[str, Any]) -> tuple[str, ...]:
    """Every relation an edge carries.

    A DiGraph holds one edge per node pair, so when edges with different
    relations share a pair they are merged and `relations` lists all of them.
    Read this rather than `relation` alone whenever the question is "does this
    pair have relation X" -- `relation` is only the first-sorted value (#1).
    """
    relations = data.get("relations")
    if relations:
        return tuple(r for r in relations if r)
    relation = data.get("relation")
    return (relation,) if relation else ()


def build_graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> nx.DiGraph:
    """Build a normalized directed graph from extraction results."""
    g = nx.DiGraph()

    # Deterministic order.
    sorted_nodes = sorted(nodes, key=lambda n: n.get("id", ""))
    for n in sorted_nodes:
        nid = normalize_id(n["id"])
        attrs = dict(n)
        attrs["id"] = nid
        attrs["label"] = attrs.get("name", nid)
        g.add_node(nid, **attrs)

    sorted_edges = sorted(
        edges,
        key=lambda e: (e.get("source", ""), e.get("target", ""), e.get("relation", "")),
    )
    for e in sorted_edges:
        src = normalize_id(e["source"])
        tgt = normalize_id(e["target"])
        if not src or not tgt or src == tgt:
            continue
        if not g.has_node(src):
            g.add_node(src, id=src, kind="unknown", name=src, label=src)
        if not g.has_node(tgt):
            g.add_node(tgt, id=tgt, kind="unknown", name=tgt, label=tgt)
        if g.has_edge(src, tgt):
            data = g[src][tgt]
            # Fold the incoming edge's own weight; a flat +1.0 discarded it
            # (extraction-level _merge emits weights >= 1.0).
            data["weight"] = data.get("weight", 1.0) + float(e.get("weight", 1.0) or 1.0)
            # A DiGraph cannot hold parallel edges, so a second edge between the
            # same pair with a DIFFERENT relation used to be dropped outright,
            # losing its relation entirely (#1). Record it instead: `relation`
            # keeps the first-sorted value for display, `relations` carries every
            # relation this pair actually has.
            relation = e.get("relation")
            if relation and relation != data.get("relation"):
                relations = data.setdefault("relations", [data.get("relation")])
                if relation not in relations:
                    relations.append(relation)
        else:
            attrs = dict(e)
            attrs["source"] = src
            attrs["target"] = tgt
            attrs["weight"] = attrs.get("weight", 1.0)
            g.add_edge(src, tgt, **attrs)

    return g


def graph_to_json(g: nx.DiGraph) -> dict[str, Any]:
    """Convert networkx graph to graphify-compatible JSON."""
    return {
        "nodes": [
            {"id": n, **{k: v for k, v in data.items() if k != "id"}}
            for n, data in g.nodes(data=True)
        ],
        "edges": [
            {
                "source": u,
                "target": v,
                **{k: val for k, val in data.items() if k not in ("source", "target")},
            }
            for u, v, data in g.edges(data=True)
        ],
        "metadata": {
            "node_count": g.number_of_nodes(),
            "edge_count": g.number_of_edges(),
        },
    }


def graph_from_json(data: dict[str, Any]) -> nx.DiGraph:
    """Load graph from JSON."""
    g = nx.DiGraph()
    for n in data.get("nodes", []):
        nid = n.get("id")
        g.add_node(nid, **n)
    for e in data.get("edges", []):
        g.add_edge(e["source"], e["target"], **e)
    return g
