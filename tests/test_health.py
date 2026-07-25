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
    g = nx.DiGraph()
    for node_id, kind, source_file in nodes:
        g.add_node(node_id, kind=kind, source_file=source_file)
    for src, dst, relation, source_file in edges:
        g.add_edge(src, dst, relation=relation, source_file=source_file)
    return g


def test_empty_graph_is_healthy_with_null_ratios():
    block = resolution_health(nx.DiGraph())
    assert block["schema"] == 2
    assert block["healthy"] is True
    assert block["threshold"] == RESOLUTION_HEALTHY_RATIO
    assert block["placeholder_nodes"] == {"total": 0, "unknown": 0, "share": None}
    assert block["by_relation"]["calls"] == {"total": 0, "bound": 0, "ratio": None}
    assert block["by_relation"]["imports"] == {"total": 0, "bound": 0, "ratio": None, "external": 0}
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
            ("f2", "ghost", "imports", "b.py"),
        ],
    )
    block = resolution_health(g)
    assert block["by_relation"]["calls"] == {"total": 2, "bound": 1, "ratio": 0.5}
    assert block["by_relation"]["imports"] == {"total": 1, "bound": 0, "ratio": 0.0, "external": 0}
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
    assert block["by_language"]["other"]["imports"] == {"total": 1, "bound": 0, "ratio": 0.0, "external": 0}
    # languages appear only when they carry at least one counted edge
    assert "go" not in block["by_language"]


def test_missing_kind_counts_as_unknown():
    g = nx.DiGraph()
    g.add_node("a", kind="function")
    g.add_node("mystery")  # no kind attribute
    g.add_edge("a", "mystery", relation="calls", source_file="a.py")
    block = resolution_health(g)
    assert block["by_relation"]["calls"] == {"total": 1, "bound": 0, "ratio": 0.0}
    assert block["by_relation"]["imports"] == {"total": 0, "bound": 0, "ratio": None, "external": 0}
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
        json.dumps({"resolution_health": {"schema": 2, "healthy": False}}), encoding="utf-8"
    )
    block = persisted_resolution(tmp_path)
    assert block == {"schema": 2, "healthy": False}


def test_persisted_resolution_fails_open(tmp_path):
    assert persisted_resolution(tmp_path) is None  # no graph-out at all
    out = tmp_path / "graph-out"
    out.mkdir()
    assert persisted_resolution(tmp_path) is None  # no analysis file
    (out / ".graphite_analysis.json").write_text("{not json", encoding="utf-8")
    assert persisted_resolution(tmp_path) is None  # malformed
    (out / ".graphite_analysis.json").write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert persisted_resolution(tmp_path) is None  # key absent
    (out / ".graphite_analysis.json").write_text(json.dumps({"resolution_health": "nope"}), encoding="utf-8")
    assert persisted_resolution(tmp_path) is None  # wrong type


def test_analyze_includes_resolution_block():
    from graphite.analyze import analyze

    g = _graph(
        nodes=[("f1", "function", "a.py"), ("ghost", "unknown", None)],
        edges=[("f1", "ghost", "calls", "a.py")],
    )
    result = analyze(g)
    assert result["resolution_health"]["by_relation"]["calls"] == {
        "total": 1, "bound": 0, "ratio": 0.0,
    }
    assert result["resolution_health"]["by_relation"]["imports"] == {
        "total": 0, "bound": 0, "ratio": None, "external": 0,
    }
    assert result["resolution_health"]["healthy"] is False


def test_build_persists_resolution_in_artifacts(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text(
        "from b import helper\n\ndef f():\n    helper()\n", encoding="utf-8"
    )
    (tmp_path / "b.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    from graphite.cli import _build_project
    from graphite.config import Config

    _build_project(tmp_path, Config())
    analysis = json.loads(
        (tmp_path / "graph-out" / ".graphite_analysis.json").read_text(encoding="utf-8")
    )
    assert analysis["resolution_health"]["schema"] == 2
    bundle = json.loads(
        (tmp_path / "graph-out" / "graph.json").read_text(encoding="utf-8")
    )
    assert bundle["analysis"]["resolution_health"]["schema"] == 2
    assert persisted_resolution(tmp_path) == analysis["resolution_health"]


def _unhealthy_graph():
    # lonely + one unbound call edge elsewhere -> calls ratio 0.0 -> unhealthy
    return _graph(
        nodes=[("lonely", "function", "a.py"), ("src", "function", "a.py"), ("ghost", "unknown", None)],
        edges=[("src", "ghost", "calls", "a.py")],
    )


def _healthy_graph():
    return _graph(
        nodes=[("lonely", "function", "a.py"), ("src", "function", "a.py"), ("dst", "function", "b.py")],
        edges=[("src", "dst", "calls", "a.py")],
    )


def test_stats_includes_resolution():
    from graphite.query import query

    result = query(_unhealthy_graph(), "stats")
    assert result["resolution_health"]["healthy"] is False


def test_callers_empty_on_unhealthy_graph_is_inconclusive():
    from graphite.query import query

    result = query(_unhealthy_graph(), "callers lonely")
    assert result["total"] == 0
    assert result["inconclusive"] is True
    assert result["resolution_health"]["healthy"] is False


def test_callers_empty_on_healthy_graph_is_conclusive():
    from graphite.query import query

    result = query(_healthy_graph(), "callers lonely")
    assert result["total"] == 0
    assert result["inconclusive"] is False


def test_callers_nonempty_not_inconclusive_even_when_unhealthy():
    from graphite.query import query

    result = query(_unhealthy_graph(), "imported-by ghost")
    assert result["total"] >= 1
    assert result["inconclusive"] is False


def test_not_found_result_has_no_inconclusive_field():
    from graphite.query import query

    result = query(_unhealthy_graph(), "callers does_not_exist_anywhere")
    assert result["error_code"] == "node_not_found"
    assert "inconclusive" not in result


def test_query_envelope_keeps_match_resolution_and_health():
    """The published v1 contract's `resolution` match-metadata list and the new
    `resolution_health` trust block are additive, not a collision: a successful
    query result carries both, under their own distinct keys."""
    from graphite.query import query

    result = query(_unhealthy_graph(), "callers ghost")
    assert result["resolution"] == [
        {"role": "node", "input": "ghost", "node": "ghost", "type": "exact-id"}
    ]
    assert isinstance(result["resolution_health"], dict)
    assert result["resolution_health"]["healthy"] is False


def test_impact_json_inconclusive_on_unhealthy_graph():
    from graphite.cli import _impact

    g = _unhealthy_graph()
    result = _impact(g, ["lonely"], depth=2)
    assert result["impacted_files"] == [] and result["likely_tests"] == []
    assert result["inconclusive"] is True
    assert result["resolution_health"]["healthy"] is False


def test_impact_json_conclusive_on_healthy_graph():
    from graphite.cli import _impact

    result = _impact(_healthy_graph(), ["lonely"], depth=2)
    assert result["inconclusive"] is False


def test_cmd_impact_human_inconclusive_line(capsys, monkeypatch):
    import argparse

    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: _unhealthy_graph())
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    args = argparse.Namespace(graph_json="graph-out/graph.json", files=["lonely"], depth=2, json=False)
    cli.cmd_impact(args)
    out = capsys.readouterr().out
    assert "INCONCLUSIVE" in out
    assert "confirm with grep" in out
    assert "Impacted files:\n" not in out  # empty listings are replaced, not printed


def test_cmd_impact_human_note_when_nonempty_but_unhealthy(capsys, monkeypatch):
    import argparse

    from graphite import cli

    g = _graph(
        nodes=[
            ("caller_file", "file", "caller.py"),
            ("target_file", "file", "target.py"),
            ("ghost", "unknown", None),
        ],
        edges=[
            ("caller_file", "target_file", "imports", "caller.py"),
            ("caller_file", "ghost", "calls", "caller.py"),
        ],
    )
    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: g)
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    args = argparse.Namespace(graph_json="graph-out/graph.json", files=["target_file"], depth=2, json=False)
    cli.cmd_impact(args)
    out = capsys.readouterr().out
    assert "caller.py" in out
    assert "may be incomplete" in out
    assert "INCONCLUSIVE" not in out


def test_cmd_impact_human_unchanged_on_healthy_graph(capsys, monkeypatch):
    import argparse

    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: _healthy_graph())
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    args = argparse.Namespace(graph_json="graph-out/graph.json", files=["lonely"], depth=2, json=False)
    cli.cmd_impact(args)
    out = capsys.readouterr().out
    assert "Impacted files:" in out
    assert "INCONCLUSIVE" not in out and "may be incomplete" not in out


def test_cmd_check_json_resolution_passthrough(tmp_path, capsys, monkeypatch):
    import argparse

    from graphite import cli

    monkeypatch.setattr(
        cli, "check_graph_freshness", lambda *a, **k: {"stale": False}
    )
    out_dir = tmp_path / "graph-out"
    out_dir.mkdir()
    (out_dir / ".graphite_analysis.json").write_text(
        json.dumps({"resolution_health": {"schema": 2, "healthy": True}}), encoding="utf-8"
    )
    args = argparse.Namespace(path=str(tmp_path), json=True, ignore_engine=False)
    cli.cmd_check(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["resolution_health"] == {"schema": 2, "healthy": True}


def test_cmd_check_json_resolution_null_when_absent(tmp_path, capsys, monkeypatch):
    import argparse

    from graphite import cli

    monkeypatch.setattr(
        cli, "check_graph_freshness", lambda *a, **k: {"stale": False}
    )
    args = argparse.Namespace(path=str(tmp_path), json=True, ignore_engine=False)
    cli.cmd_check(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["resolution_health"] is None


def test_external_imports_excluded_from_ratio():
    g = nx.DiGraph()
    g.add_node("f", kind="file", source_file="a.py")
    g.add_node("dep", kind="file", source_file="b.py")
    g.add_node("pathlib", kind="unknown")
    g.add_edge("f", "dep", relation="imports", source_file="a.py", confidence="EXACT_IMPORT")
    g.add_edge("f", "pathlib", relation="imports", source_file="a.py", confidence="EXTERNAL_IMPORT")
    block = resolution_health(g)
    assert block["schema"] == 2
    assert block["by_relation"]["imports"] == {
        "total": 1, "bound": 1, "ratio": 1.0, "external": 1,
    }
    assert block["healthy"] is True
    assert block["by_language"]["python"]["imports"]["external"] == 1


def test_untagged_import_edges_still_count():
    g = nx.DiGraph()
    g.add_node("f", kind="file", source_file="a.py")
    g.add_node("ghost", kind="unknown")
    g.add_edge("f", "ghost", relation="imports", source_file="a.py")  # no confidence
    block = resolution_health(g)
    assert block["by_relation"]["imports"] == {
        "total": 1, "bound": 0, "ratio": 0.0, "external": 0,
    }
    assert block["healthy"] is False


def test_calls_cells_have_no_external_field():
    block = resolution_health(nx.DiGraph())
    assert "external" not in block["by_relation"]["calls"]
    assert block["by_relation"]["imports"]["external"] == 0


def test_neighbor_listing_empty_on_unhealthy_graph_is_inconclusive():
    from graphite.query import query

    result = query(_unhealthy_graph(), "depends-on lonely")
    assert result["total"] == 0
    assert result["inconclusive"] is True
