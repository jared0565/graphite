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
    for edge in edges:
        src, dst, relation, source_file = edge[:4]
        attrs = {"relation": relation, "source_file": source_file}
        if len(edge) > 4 and edge[4] is not None:
            attrs["confidence"] = edge[4]
        g.add_edge(src, dst, **attrs)
    return g


def test_empty_graph_is_healthy_with_null_ratios():
    block = resolution_health(nx.DiGraph())
    assert block["schema"] == 3
    assert block["healthy"] is True
    assert block["threshold"] == RESOLUTION_HEALTHY_RATIO
    assert block["placeholder_nodes"] == {"total": 0, "unknown": 0, "share": None}
    assert block["by_relation"]["calls"] == {"total": 0, "bound": 0, "ratio": None, "external": 0}
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
    assert block["by_relation"]["calls"] == {"total": 2, "bound": 1, "ratio": 0.5, "external": 0}
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
    assert block["by_language"]["python"]["calls"] == {"total": 1, "bound": 1, "ratio": 1.0, "external": 0}
    assert block["by_language"]["typescript"]["calls"] == {"total": 1, "bound": 0, "ratio": 0.0, "external": 0}
    assert block["by_language"]["other"]["imports"] == {"total": 1, "bound": 0, "ratio": 0.0, "external": 0}
    # languages appear only when they carry at least one counted edge
    assert "go" not in block["by_language"]


def test_missing_kind_counts_as_unknown():
    g = nx.DiGraph()
    g.add_node("a", kind="function")
    g.add_node("mystery")  # no kind attribute
    g.add_edge("a", "mystery", relation="calls", source_file="a.py")
    block = resolution_health(g)
    assert block["by_relation"]["calls"] == {"total": 1, "bound": 0, "ratio": 0.0, "external": 0}
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
        "total": 1, "bound": 0, "ratio": 0.0, "external": 0,
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
    assert analysis["resolution_health"]["schema"] == 3
    bundle = json.loads(
        (tmp_path / "graph-out" / "graph.json").read_text(encoding="utf-8")
    )
    assert bundle["analysis"]["resolution_health"]["schema"] == 3
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
    monkeypatch.setattr(cli, "_record_inconclusive", lambda *a, **k: None)
    args = argparse.Namespace(graph_json="graph-out/graph.json", files=["lonely"], depth=2, json=False)
    cli.cmd_impact(args)
    out = capsys.readouterr().out
    assert "INCONCLUSIVE" in out
    assert "confirm with grep" in out
    assert "Impacted files:\n" not in out  # empty listings are replaced, not printed
    # The scoped answer block computes a real meaning here, so the legacy
    # aggregate-ratio wording (which can self-contradict the scoped "answer
    # health:" line right below it) must not print.
    assert "% of import edges" not in out
    assert "no impacted files or tests reachable through bound edges" in out
    assert "answer health: " in out


def test_cmd_impact_human_inconclusive_line_fail_open_aggregate_wording(capsys, monkeypatch):
    """When build_answer_block fails open (returns None), the legacy
    aggregate-ratio INCONCLUSIVE wording is the only signal available and
    must still print — and no answer-health lines should appear."""
    import argparse

    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: _unhealthy_graph())
    monkeypatch.setattr(cli, "build_answer_block", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_record_inconclusive", lambda *a, **k: None)
    args = argparse.Namespace(graph_json="graph-out/graph.json", files=["lonely"], depth=2, json=False)
    cli.cmd_impact(args)
    out = capsys.readouterr().out
    assert "INCONCLUSIVE" in out
    assert "only" in out and "of import edges and" in out and "of call edges resolved in this" in out
    assert "confirm with grep" in out
    assert "answer health:" not in out


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
    monkeypatch.setattr(cli, "_record_inconclusive", lambda *a, **k: None)
    args = argparse.Namespace(graph_json="graph-out/graph.json", files=["target_file"], depth=2, json=False)
    cli.cmd_impact(args)
    out = capsys.readouterr().out
    assert "caller.py" in out
    assert "may be incomplete" in out
    # The impacted-files half is non-empty and must not be marked/replaced;
    # its empty sibling (likely_tests) is allowed to carry the grade-aware
    # INCONCLUSIVE marker now that this round makes it grade-aware (spec §5).
    assert "Impacted files: none found" not in out
    assert "INCONCLUSIVE" not in out.split("Likely tests:")[0]


def test_cmd_impact_human_unchanged_on_healthy_graph(capsys, monkeypatch):
    import argparse

    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: _healthy_graph())
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_record_inconclusive", lambda *a, **k: None)
    args = argparse.Namespace(graph_json="graph-out/graph.json", files=["lonely"], depth=2, json=False)
    cli.cmd_impact(args)
    out = capsys.readouterr().out
    assert "Impacted files:" in out
    assert "INCONCLUSIVE" not in out and "may be incomplete" not in out


def _answer_graph(*, degraded_ts=False):
    g = nx.DiGraph()
    g.add_node("src_a", kind="file", name="a.py", source_file="src/a.py")
    g.add_node("src_b", kind="file", name="b.py", source_file="src/b.py")
    g.add_edge("src_b", "src_a", relation="imports", source_file="src/b.py")
    if degraded_ts:
        g.add_node("t", kind="file", name="t.ts", source_file="src/t.ts")
        for i in range(9):
            ph = f"tsph{i}"
            g.add_node(ph, kind="unknown")
            g.add_edge("t", ph, relation="calls", source_file="src/t.ts")
        # Keep the AGGREGATE calls ratio >= 0.8 (41 bound py + 9 unbound ts
        # = 41/50 = 0.82) so the test proves scoped grading sees what the
        # aggregate masks (the firescraper shape).
        g.add_node("py_callee", kind="function", name="callee", source_file="src/a.py")
        for i in range(41):
            fn = f"py_fn{i}"
            g.add_node(fn, kind="function", name=f"f{i}", source_file="src/a.py")
            g.add_edge(fn, "py_callee", relation="calls", source_file="src/a.py")
    return g


def test_impact_result_carries_answer_block():
    from graphite import cli

    g = _answer_graph()
    result = cli._impact(g, ["src/a.py"], 2)
    assert result["answer"]["relations"] == ["calls", "imports"]
    assert result["answer"]["grade"] == "decision_grade"
    assert result["impacted_files"] == ["src/b.py"]


def test_impact_on_non_code_file_is_not_inconclusive_despite_unhealthy_graph():
    """Same regression as test_context.py's mirror test, through cli._impact:
    a query whose only matched node has no applicable code language (e.g.
    README.md) must not inherit an unrelated file's degraded health."""
    from graphite import cli

    g = nx.DiGraph()
    g.add_node("src", kind="function", source_file="a.py")
    g.add_node("tgt", kind="unknown", source_file="b.py")
    g.add_edge("src", "tgt", relation="calls", source_file="a.py")  # unbound -> graph unhealthy
    g.add_node("readme", kind="file", name="README.md", source_file="README.md")

    result = cli._impact(g, ["README.md"], 2)
    assert result["resolution_health"]["healthy"] is False  # graph really is degraded
    assert result["inconclusive"] is False
    assert "answer" not in result


def test_impact_inconclusive_upgrades_to_scoped():
    from graphite import cli

    g = _answer_graph(degraded_ts=True)
    result = cli._impact(g, ["src/t.ts"], 2)
    assert result["impacted_files"] == [] and result["likely_tests"] == []
    assert result["answer"]["grade"] == "inconclusive"
    assert result["inconclusive"] is True
    assert result["resolution_health"]["healthy"] is True  # aggregate masks; scoped does not


def test_cmd_impact_prints_epistemology_on_empty(capsys, monkeypatch):
    import argparse

    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: _answer_graph())
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_record_inconclusive", lambda *a, **k: None)
    args = argparse.Namespace(graph_json="graph-out/graph.json", files=["src/b.py"], depth=2, json=False)
    cli.cmd_impact(args)
    out = capsys.readouterr().out
    assert "answer health: " in out
    # An empty `impact` over `calls` is advisory, not decision-grade. "Nothing
    # depends on this" is exactly the claim an unmodelled callback
    # registration falsifies, and it is the claim that licenses a deletion --
    # so this surface is where the round-55 gap costs the most.
    assert "decision-grade" not in out
    # Not INCONCLUSIVE either: the cells are measured and healthy. What is
    # unsupported is the absence, not the measurement.
    assert "INCONCLUSIVE" not in out
    # The reader has to be TOLD why the absence is not proof. `cmd_impact`
    # keeps its own empty text ("through bound edges") rather than
    # `empty_marker`, so what carries the round-55 warning here is the
    # conditional caveat -- which must actually reach the printed output.
    assert "produces no call edge" in out


def test_cmd_impact_human_advisory_line_on_nonempty_degraded(capsys, monkeypatch):
    """Non-empty impact + a degraded scoped cell (via language union across
    the two queried files) grades advisory, not decision — and the normal
    impacted-files listing still prints, followed by the epistemology
    tail."""
    import argparse

    from graphite import cli

    g = _answer_graph(degraded_ts=True)
    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: g)
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_record_inconclusive", lambda *a, **k: None)
    args = argparse.Namespace(
        graph_json="graph-out/graph.json",
        files=["src/a.py", "src/t.ts"],
        depth=2,
        json=False,
    )
    cli.cmd_impact(args)
    out = capsys.readouterr().out
    assert "Impacted files:" in out
    assert "src/b.py" in out
    # The impacted-files half is non-empty and must not be marked/replaced;
    # its empty sibling (likely_tests) is allowed to carry the grade-aware
    # INCONCLUSIVE marker now that this round makes it grade-aware (spec §5).
    assert "Impacted files: none found" not in out
    assert "INCONCLUSIVE" not in out.split("Likely tests:")[0]
    assert "answer health: " in out
    assert "advisory" in out
    assert "known limits:" in out
    # Normal output first, THEN the epistemology tail (not interleaved).
    assert out.index("src/b.py") < out.index("answer health: ")


def test_answer_lines_two_cell_sorted_join_format():
    """The health-cell join is sorted by (relation, language), independent
    of dict insertion order."""
    from graphite import cli

    block = {
        "grade": "decision_grade",
        "health": {
            "imports": {"python": {"ratio": 0.80, "healthy": True}},
            "calls": {"python": {"ratio": 0.95, "healthy": True}},
        },
        "caveats": [],
    }
    lines = cli._answer_lines(block, empty=True)
    assert lines == ["answer health: calls (python) 0.95, imports (python) 0.80 — decision-grade"]


def test_cmd_impact_epistemology_absent_on_healthy_nonempty(capsys, monkeypatch):
    import argparse

    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: _answer_graph())
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_record_inconclusive", lambda *a, **k: None)
    args = argparse.Namespace(graph_json="graph-out/graph.json", files=["src/a.py"], depth=2, json=False)
    cli.cmd_impact(args)
    out = capsys.readouterr().out
    assert "answer health:" not in out


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
    assert block["schema"] == 3
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


def test_both_relations_carry_external_field():
    """Schema 3: `external` is unconditional on every cell, calls included
    (superseded schema-2 invariant: calls previously omitted the key)."""
    block = resolution_health(nx.DiGraph())
    assert block["by_relation"]["calls"]["external"] == 0
    assert block["by_relation"]["imports"]["external"] == 0


def test_neighbor_listing_empty_on_unhealthy_graph_is_inconclusive():
    from graphite.query import query

    result = query(_unhealthy_graph(), "depends-on lonely")
    assert result["total"] == 0
    assert result["inconclusive"] is True


def test_persisted_resolution_on_error_fires_for_malformed(tmp_path):
    from graphite.health import persisted_resolution

    out = tmp_path / "graph-out"
    out.mkdir()
    (out / ".graphite_analysis.json").write_text("{not valid json", encoding="utf-8")
    seen: list[Exception] = []
    assert persisted_resolution(tmp_path, on_error=seen.append) is None
    assert len(seen) == 1 and isinstance(seen[0], ValueError)


def test_persisted_resolution_on_error_silent_for_missing(tmp_path):
    from graphite.health import persisted_resolution

    seen: list[Exception] = []
    assert persisted_resolution(tmp_path, on_error=seen.append) is None
    assert seen == []


def test_is_degraded_reads_scoped_cells():
    from graphite.answer_contract import is_degraded

    assert is_degraded(None) is False
    assert is_degraded({}) is False
    assert is_degraded({"health": {}}) is False
    assert is_degraded(
        {"health": {"calls": {"python": {"ratio": 0.95, "healthy": True}}}}
    ) is False
    assert is_degraded(
        {"health": {"calls": {"typescript": {"ratio": 0.54, "healthy": False}}}}
    ) is True


def test_empty_marker_is_scoped_to_the_grade():
    """Spec §5: a bare 'none found' on a degraded answer is an overclaim."""
    from graphite.answer_contract import empty_marker

    healthy = {"health": {"calls": {"python": {"ratio": 0.95, "healthy": True}}}}
    degraded = {"health": {"calls": {"typescript": {"ratio": 0.54, "healthy": False}}}}

    assert empty_marker(healthy) == "none found"
    assert empty_marker(None) == "none found"
    assert empty_marker(degraded) == (
        "none found — INCONCLUSIVE: treat as unverified and confirm with grep"
    )


def test_answer_lines_render_at_column_zero():
    """R3: these lines must not share the two-space list indentation."""
    from graphite import cli

    block = {
        "grade": "decision_grade",
        "health": {
            "imports": {"python": {"ratio": 0.80, "healthy": True}},
            "calls": {"python": {"ratio": 0.95, "healthy": True}},
        },
        "caveats": [],
    }
    lines = cli._answer_lines(block, empty=True)
    assert lines == ["answer health: calls (python) 0.95, imports (python) 0.80 — decision-grade"]
    assert not lines[0].startswith(" ")


def _impact_args(files=("src/a.py",)):
    import argparse

    return argparse.Namespace(
        graph_json="graph-out/graph.json", files=list(files), depth=2, json=False
    )


def test_impact_empty_tests_half_is_marked_on_a_healthy_graph(capsys, monkeypatch):
    """Partial-empty answer: the header gets a body, and it claims nothing extra."""
    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: _answer_graph())
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_record_inconclusive", lambda *a, **k: None)
    cli.cmd_impact(_impact_args())
    out = capsys.readouterr().out

    assert "Likely tests:\n  - none found\n" in out
    assert "INCONCLUSIVE" not in out


def test_impact_empty_tests_half_is_marked_inconclusive_when_degraded(capsys, monkeypatch):
    """The firescraper shape (spec §5 falsifier): impacted > 0, tests == 0,
    degraded. Same shape as `_answer_graph(degraded_ts=True)` queried with
    both `src/a.py` and `src/t.ts` (see
    test_cmd_impact_human_advisory_line_on_nonempty_degraded): impacted_files
    == ["src/b.py"], likely_tests == [], grade advisory, typescript calls
    cell degraded.

    Without the grade-aware branch this renders a bare `- none found`, which
    asserts an absence the graph did not earn. Note: `files=["src/t.ts"]`
    alone would grade the whole answer `inconclusive` (both halves empty),
    routing through cmd_impact's separate top-level inconclusive branch
    instead of the listing branch this test targets — see
    test_impact_inconclusive_upgrades_to_scoped.
    """
    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: _answer_graph(degraded_ts=True))
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_record_inconclusive", lambda *a, **k: None)
    cli.cmd_impact(_impact_args(files=["src/a.py", "src/t.ts"]))
    out = capsys.readouterr().out

    assert "- none found — INCONCLUSIVE: treat as unverified and confirm with grep" in out


def test_impact_never_prints_a_bare_header(capsys, monkeypatch):
    """R1, asserted structurally: no header line is the last line or is
    followed by a line that is not part of its body."""
    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: _answer_graph())
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_record_inconclusive", lambda *a, **k: None)
    cli.cmd_impact(_impact_args())
    lines = capsys.readouterr().out.splitlines()

    for index, line in enumerate(lines):
        if line.endswith(":") and not line.startswith(" "):
            assert index + 1 < len(lines), f"bare header at end of output: {line!r}"
            assert lines[index + 1].startswith("  "), f"bare header: {line!r}"


def test_external_call_edges_are_excluded_and_counted():
    g = _graph(
        nodes=[
            ("f1", "function", "a.ts"),
            ("f2", "function", "b.ts"),
            ("expect", "unknown", None),
        ],
        edges=[
            ("f1", "f2", "calls", "a.ts", "LOCAL_CALL"),
            ("f1", "expect", "calls", "a.ts", "EXTERNAL_CALL"),
        ],
    )
    block = resolution_health(g)
    assert block["by_relation"]["calls"] == {
        "total": 1, "bound": 1, "ratio": 1.0, "external": 1
    }
    assert block["by_language"]["typescript"]["calls"]["external"] == 1


def test_schema_is_three():
    assert resolution_health(nx.DiGraph())["schema"] == 3


def test_external_call_that_bound_is_counted_normally():
    """Externality only excuses an UNBOUND edge (spec §5).

    Guards the name-based matching in _EXTERNAL_GLOBALS: a repo that defines
    its own test()/process() must not have that real binding excluded.
    """
    g = _graph(
        nodes=[
            ("f1", "function", "a.ts"),
            ("mine", "function", "a.ts"),
        ],
        edges=[("f1", "mine", "calls", "a.ts", "EXTERNAL_CALL")],
    )
    block = resolution_health(g)
    assert block["by_relation"]["calls"] == {
        "total": 1, "bound": 1, "ratio": 1.0, "external": 0
    }


def test_calls_cell_with_only_external_edges_reports_null_ratio():
    g = _graph(
        nodes=[("f1", "function", "a.ts"), ("expect", "unknown", None)],
        edges=[("f1", "expect", "calls", "a.ts", "EXTERNAL_CALL")],
    )
    cell = resolution_health(g)["by_relation"]["calls"]
    assert cell == {"total": 0, "bound": 0, "ratio": None, "external": 1}


def test_external_import_that_bound_is_counted_normally():
    """Mirrors test_external_call_that_bound_is_counted_normally for the
    imports relation: the guard is uniform across _EXTERNAL_CONFIDENCE, not
    calls-only. Pre-Task-1, ANY EXTERNAL_IMPORT edge was excluded regardless
    of binding -- this pins the new, narrower guarded behavior."""
    g = _graph(
        nodes=[
            ("f1", "file", "a.py"),
            ("dep", "file", "b.py"),
        ],
        edges=[("f1", "dep", "imports", "a.py", "EXTERNAL_IMPORT")],
    )
    block = resolution_health(g)
    assert block["by_relation"]["imports"] == {
        "total": 1, "bound": 1, "ratio": 1.0, "external": 0
    }


def test_external_call_confidence_on_imports_relation_is_not_external():
    """The mapping is per-relation: EXTERNAL_CALL means nothing on imports."""
    g = _graph(
        nodes=[("f1", "file", "a.ts"), ("ghost", "unknown", None)],
        edges=[("f1", "ghost", "imports", "a.ts", "EXTERNAL_CALL")],
    )
    cell = resolution_health(g)["by_relation"]["imports"]
    assert cell == {"total": 1, "bound": 0, "ratio": 0.0, "external": 0}
