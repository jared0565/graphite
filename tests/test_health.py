"""Unit tests for the resolution-health trust signal."""
from __future__ import annotations

import json

import networkx as nx

from graphite.health import (
    RESOLUTION_HEALTHY_RATIO,
    persisted_resolution,
    ratio_percent,
    resolution_health,
)


def _graph(nodes, edges):
    g = nx.MultiDiGraph()
    for node_id, kind, source_file in nodes:
        g.add_node(node_id, kind=kind, source_file=source_file)
    for src, dst, relation, source_file in edges:
        g.add_edge(src, dst, relation=relation, source_file=source_file)
    return g


def test_empty_graph_is_healthy_with_null_ratios():
    block = resolution_health(nx.DiGraph())
    assert block["schema"] == 1
    assert block["healthy"] is True
    assert block["threshold"] == RESOLUTION_HEALTHY_RATIO
    assert block["placeholder_nodes"] == {"total": 0, "unknown": 0, "share": None}
    assert block["by_relation"]["calls"] == {"total": 0, "bound": 0, "ratio": None}
    assert block["by_relation"]["imports"] == {"total": 0, "bound": 0, "ratio": None}
    assert block["by_language"] == {}


def test_bound_and_unbound_edges_counted_per_relation():
    g = _graph(
        nodes=[
            ("f1", "function", "a.py"),
            ("f2", "function", "b.py"),
            ("ghost", "unknown", None),
        ],
        edges=[
            ("f1", "f2", "calls", "a.py"),
            ("f1", "ghost", "calls", "a.py"),
            ("f1", "ghost", "imports", "a.py"),
        ],
    )
    block = resolution_health(g)
    assert block["by_relation"]["calls"] == {"total": 2, "bound": 1, "ratio": 0.5}
    assert block["by_relation"]["imports"] == {"total": 1, "bound": 0, "ratio": 0.0}
    assert block["healthy"] is False
    assert block["placeholder_nodes"] == {"total": 3, "unknown": 1, "share": 0.333}


def test_structural_relations_ignored():
    g = _graph(
        nodes=[("file", "file", "a.py"), ("f1", "function", "a.py")],
        edges=[("file", "f1", "contains", "a.py")],
    )
    block = resolution_health(g)
    assert block["by_relation"]["calls"]["total"] == 0
    assert block["by_relation"]["imports"]["total"] == 0
    assert block["healthy"] is True  # vacuously: nothing to distrust


def test_threshold_boundary_exact_is_healthy():
    nodes = [("t", "function", "a.py"), ("ghost", "unknown", None)]
    edges = [(f"s{i}", "t", "calls", "a.py") for i in range(8)]
    edges += [(f"s{i}", "ghost", "calls", "a.py") for i in range(8, 10)]
    for i in range(10):
        nodes.append((f"s{i}", "function", "a.py"))
    block = resolution_health(_graph(nodes, edges))
    assert block["by_relation"]["calls"]["ratio"] == 0.8
    assert block["healthy"] is True


def test_language_attribution_from_edge_source_file():
    g = _graph(
        nodes=[("a", "function", "x.py"), ("b", "function", "y.ts"), ("ghost", "unknown", None)],
        edges=[
            ("a", "b", "calls", "src/x.py"),
            ("b", "ghost", "calls", "src/app.ts"),
            ("a", "ghost", "imports", None),  # missing source_file -> other
        ],
    )
    block = resolution_health(g)
    assert block["by_language"]["python"]["calls"] == {"total": 1, "bound": 1, "ratio": 1.0}
    assert block["by_language"]["typescript"]["calls"] == {"total": 1, "bound": 0, "ratio": 0.0}
    assert block["by_language"]["other"]["imports"] == {"total": 1, "bound": 0, "ratio": 0.0}
    # languages appear only when they carry at least one counted edge
    assert "go" not in block["by_language"]


def test_missing_kind_counts_as_unknown():
    g = nx.DiGraph()
    g.add_node("a", kind="function")
    g.add_node("mystery")  # no kind attribute
    g.add_edge("a", "mystery", relation="calls", source_file="a.py")
    block = resolution_health(g)
    assert block["by_relation"]["calls"] == {"total": 1, "bound": 0, "ratio": 0.0}
    assert block["placeholder_nodes"]["unknown"] == 1


def test_ratio_percent_formats_and_handles_null():
    block = resolution_health(nx.DiGraph())
    assert ratio_percent(block, "calls") == "n/a"
    g = _graph(
        nodes=[("a", "function", "a.py"), ("ghost", "unknown", None)],
        edges=[("a", "ghost", "imports", "a.py")],
    )
    assert ratio_percent(resolution_health(g), "imports") == "0.0%"
    assert ratio_percent({}, "calls") == "n/a"


def test_persisted_resolution_reads_block(tmp_path):
    out = tmp_path / "graph-out"
    out.mkdir()
    (out / ".graphite_analysis.json").write_text(
        json.dumps({"resolution": {"schema": 1, "healthy": False}}), encoding="utf-8"
    )
    block = persisted_resolution(tmp_path)
    assert block == {"schema": 1, "healthy": False}


def test_persisted_resolution_fails_open(tmp_path):
    assert persisted_resolution(tmp_path) is None  # no graph-out at all
    out = tmp_path / "graph-out"
    out.mkdir()
    assert persisted_resolution(tmp_path) is None  # no analysis file
    (out / ".graphite_analysis.json").write_text("{not json", encoding="utf-8")
    assert persisted_resolution(tmp_path) is None  # malformed
    (out / ".graphite_analysis.json").write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert persisted_resolution(tmp_path) is None  # key absent
    (out / ".graphite_analysis.json").write_text(json.dumps({"resolution": "nope"}), encoding="utf-8")
    assert persisted_resolution(tmp_path) is None  # wrong type
