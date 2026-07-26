"""Canonical query plans: schema gate, deterministic construction, validation."""
from __future__ import annotations

import json

import networkx as nx

from graphite.cli import main
from graphite.graph import build_graph
from graphite.query import QUERY_VERBS, build_plan, execute_plan, query
from graphite.query_plan import PLAN_SCHEMA, make_plan, plan_error
from graphite.routing.schema_validation import is_supported_schema


def _roles() -> dict[str, tuple[str, ...]]:
    return {spec.name: spec.roles for spec in QUERY_VERBS}


def _graph():
    nodes = [
        {"id": "src_app", "kind": "file", "name": "app.ts", "source_file": "src/app.ts"},
        {"id": "src_app_main", "kind": "function", "name": "main", "source_file": "src/app.ts"},
        {"id": "src_lib", "kind": "file", "name": "lib.ts", "source_file": "src/lib.ts"},
        {"id": "src_lib_helper", "kind": "function", "name": "helper", "source_file": "src/lib.ts"},
    ]
    edges = [
        {"source": "src_app", "target": "src_lib", "relation": "imports"},
        {"source": "src_app_main", "target": "src_lib_helper", "relation": "calls"},
    ]
    return build_graph(nodes, edges)


def test_plan_schema_is_supported_by_subset_validator() -> None:
    """Compile-check pin: the plan schema stays inside the fail-closed validator subset."""
    assert is_supported_schema(PLAN_SCHEMA) is True


def test_build_plan_golden_shapes() -> None:
    assert build_plan("callers helper") == {
        "plan_version": 1,
        "operation": "callers",
        "targets": [{"role": "node", "input": "helper"}],
        "options": {"max_results": 200},
        "source": {"mode": "local-graph"},
    }
    assert build_plan("reaches main -> helper") == {
        "plan_version": 1,
        "operation": "reaches",
        "targets": [
            {"role": "source", "input": "main"},
            {"role": "target", "input": "helper"},
        ],
        "options": {"max_depth": 32},
        "source": {"mode": "local-graph"},
    }
    assert build_plan("stats") == {
        "plan_version": 1,
        "operation": "stats",
        "targets": [],
        "options": {},
        "source": {"mode": "local-graph"},
    }


def test_build_plan_normalizes_aliases_to_canonical_operation() -> None:
    assert build_plan("called-by helper")["operation"] == "callers"
    assert build_plan("callees main")["operation"] == "calls"
    assert build_plan("in src_lib")["operation"] == "imported-by"
    assert build_plan("out src_app")["operation"] == "depends-on"


def test_build_plan_rejects_empty_targets_instead_of_guessing() -> None:
    """A verb without its target must error, not fuzzy-match an arbitrary node."""
    assert build_plan("callers") == {
        "error": "callers query format: callers <symbol>",
        "error_code": "invalid_query_format",
    }
    assert build_plan("reaches -> b")["error_code"] == "invalid_query_format"
    assert build_plan("path a ->")["error_code"] == "invalid_query_format"

    g = _graph()
    assert query(g, "depends-on")["error_code"] == "invalid_query_format"


def test_build_plan_error_passthrough_codes() -> None:
    assert build_plan("")["error_code"] == "empty_query"
    assert build_plan("how does pairing work")["error_code"] == "unknown_query_verb"
    assert build_plan("path src_app")["error_code"] == "invalid_query_format"


def test_plan_error_accepts_internally_built_plans_for_every_verb() -> None:
    roles = _roles()
    for spec in QUERY_VERBS:
        if spec.roles == ():
            q = spec.name
        elif spec.roles == ("node",):
            q = f"{spec.name} helper"
        else:
            q = f"{spec.name} a -> b"
        plan = build_plan(q)
        assert "error" not in plan, spec.name
        assert plan_error(plan, roles) is None, spec.name


def test_plan_error_rejects_malformed_plans() -> None:
    roles = _roles()
    good = make_plan("callers", [("node", "x")], {})
    assert plan_error(good, roles) is None

    assert plan_error({}, roles) is not None
    assert plan_error("not a plan", roles) is not None
    assert plan_error({**good, "plan_version": 2}, roles) is not None
    assert plan_error({**good, "extra": 1}, roles) is not None
    assert plan_error({**good, "source": {"mode": "remote"}}, roles) is not None
    assert plan_error(make_plan("nope", [("node", "x")], {}), roles) is not None
    assert plan_error(make_plan("callers", [("source", "x")], {}), roles) is not None
    assert plan_error(make_plan("callers", [], {}), roles) is not None
    assert plan_error(make_plan("path", [("source", "a")], {}), roles) is not None
    assert plan_error(make_plan("callers", [("node", "")], {}), roles) is not None
    assert plan_error(make_plan("callers", [("node", "x")], {"max_depth": 0}), roles) is not None
    assert plan_error(make_plan("callers", [("node", "x")], {"max_depth": "9"}), roles) is not None
    assert plan_error(make_plan("callers", [("node", "x")], {"bogus": 3}), roles) is not None


def test_execute_plan_validates_before_dispatch() -> None:
    g = _graph()

    out = execute_plan(g, {"plan_version": 1})
    assert out["error_code"] == "invalid_plan"
    assert "plan schema v1" in out["error"]

    out = execute_plan(g, make_plan("callers", [("node", "helper"), ("node", "x")], {}))
    assert out["error_code"] == "invalid_plan"

    ok = execute_plan(g, build_plan("callers helper"))
    assert ok["node"] == "src_lib_helper"


def test_neighbor_listings_cap_results_and_report_truncation() -> None:
    nodes = [
        {"id": f"caller_{i}", "kind": "function", "name": f"caller{i}", "source_file": "src/c.ts"}
        for i in range(3)
    ]
    nodes.append({"id": "hub", "kind": "function", "name": "hub", "source_file": "src/hub.ts"})
    edges = [{"source": f"caller_{i}", "target": "hub", "relation": "calls"} for i in range(3)]
    g = build_graph(nodes, edges)

    plan = build_plan("callers hub")
    assert plan["options"] == {"max_results": 200}
    unbounded = execute_plan(g, plan)
    assert unbounded["count"] == unbounded["total"] == 3
    assert unbounded["truncated"] is False

    capped = execute_plan(g, {**plan, "options": {"max_results": 2}})
    assert capped["count"] == 2 == len(capped["callers"])
    assert capped["total"] == 3
    assert capped["truncated"] is True
    assert capped["limits"] == {"max_results": 2}
    # deterministic: the cap keeps the first entries of the sorted listing
    assert [c["id"] for c in capped["callers"]] == ["caller_0", "caller_1"]

    imported = execute_plan(g, {**build_plan("imported-by hub"), "options": {"max_results": 1}})
    assert imported["count"] == 1
    assert imported["total"] == 3
    assert imported["truncated"] is True


def test_path_and_reaches_respect_max_depth_and_flag_truncation() -> None:
    nodes = [
        {"id": n, "kind": "function", "name": n, "source_file": "src/x.ts"}
        for n in ("a", "b", "c", "island")
    ]
    edges = [
        {"source": "a", "target": "b", "relation": "calls"},
        {"source": "b", "target": "c", "relation": "calls"},
    ]
    g = build_graph(nodes, edges)

    ok = query(g, "reaches a -> c")
    assert ok["length"] == 2
    assert ok["truncated"] is False
    assert ok["limits"] == {"max_depth": 32}

    shallow = execute_plan(g, {**build_plan("reaches a -> c"), "options": {"max_depth": 1}})
    assert shallow["error_code"] == "no_path"
    assert shallow["truncated"] is True

    unreachable = query(g, "reaches c -> a")
    assert unreachable["error_code"] == "no_path"
    assert unreachable["truncated"] is False

    p_shallow = execute_plan(g, {**build_plan("path a -> c"), "options": {"max_depth": 1}})
    assert p_shallow["error_code"] == "no_path"
    assert p_shallow["truncated"] is True

    isolated = query(g, "path island -> a")
    assert isolated["error_code"] == "no_path"
    assert isolated["truncated"] is False


def test_query_results_carry_schema_version_and_resolution() -> None:
    g = _graph()

    out = query(g, "callers helper")
    assert out["schema_version"] == 1
    assert out["resolution"] == [
        {"role": "node", "input": "helper", "node": "src_lib_helper", "type": "name"}
    ]

    arrow = query(g, "reaches main -> helper")
    assert [entry["role"] for entry in arrow["resolution"]] == ["source", "target"]
    assert arrow["resolution"][0]["node"] == "src_app_main"

    assert query(g, "stats")["resolution"] == []

    for q in ("", "nonsense verb", "callers zzqx", "path a"):
        result = query(g, q)
        assert result["schema_version"] == 1, q
        assert "resolution" not in result, q


def test_query_via_plan_matches_direct_execution() -> None:
    g = _graph()
    for q in ("callers helper", "reaches main -> helper", "stats", "community-of src_app"):
        assert query(g, q) == execute_plan(g, build_plan(q))


def test_cli_plan_only_needs_no_graph(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)  # deliberately no graph.json anywhere

    assert main(["query", "callers main", "--plan-only"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan_version"] == 1
    assert payload["operation"] == "callers"
    assert payload["targets"] == [{"role": "node", "input": "main"}]

    assert main(["query", "reaches a", "--plan-only"]) == 0
    err = json.loads(capsys.readouterr().out)
    assert err["error_code"] == "invalid_query_format"
    assert err["schema_version"] == 1


def test_cli_show_plan_includes_plan_in_result(tmp_path, monkeypatch, capsys) -> None:
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "app.ts").write_text("export function main() { return 1; }\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["build", "."]) == 0
    capsys.readouterr()

    assert main(["query", "callers main", "--show-plan"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan"]["operation"] == "callers"
    assert payload["schema_version"] == 1
    assert "resolution" in payload

    assert main(["query", "callers main"]) == 0
    bare = json.loads(capsys.readouterr().out)
    assert "plan" not in bare


def _contract_graph():
    """Python-healthy graph: one caller -> callee, plus an isolated leaf."""
    g = nx.DiGraph()
    g.add_node("a", kind="file", name="a.py", source_file="a.py")
    g.add_node("b", kind="file", name="b.py", source_file="b.py")
    g.add_node("a_fn", kind="function", name="fn", source_file="a.py")
    g.add_node("b_fn", kind="function", name="gn", source_file="b.py")
    g.add_edge("a", "b", relation="imports", source_file="a.py")
    g.add_edge("a_fn", "b_fn", relation="calls", source_file="a.py")
    return g


def test_execute_plan_attaches_answer_block():
    g = _contract_graph()
    result = execute_plan(g, make_plan("imported-by", [("node", "b")], {}))
    assert result["answer"]["schema"] == 1
    assert result["answer"]["relations"] == ["imports"]
    assert result["answer"]["grade"] == "decision_grade"
    assert "empty_meaning" not in result["answer"]


def test_execute_plan_empty_answer_carries_meaning():
    g = _contract_graph()
    result = execute_plan(g, make_plan("callers", [("node", "a_fn")], {}))
    assert result["total"] == 0
    assert result["answer"]["empty_meaning"] == "no bound callers found"


def test_stats_has_no_answer_block():
    g = _contract_graph()
    result = execute_plan(g, make_plan("stats", [], {}))
    assert "answer" not in result


def test_no_path_error_carries_answer_block():
    g = _contract_graph()
    result = execute_plan(g, make_plan("reaches", [("source", "b_fn"), ("target", "a_fn")], {}))
    assert result["error_code"] == "no_path"
    assert result["answer"]["relations"] == ["calls"]
    assert result["answer"]["empty_meaning"] == "no call path found within depth"


def test_neighbor_listing_entries_carry_source_file():
    g = _contract_graph()
    result = execute_plan(g, make_plan("imported-by", [("node", "b")], {}))
    entry = result["imported_by"][0]
    assert entry["source_file"] == "a.py"
    assert set(entry.keys()) == {"id", "name", "kind", "source_file"}


def test_legacy_inconclusive_upgrades_to_scoped(monkeypatch):
    """Empty answer on a degraded scoped cell => inconclusive even if
    aggregate healthy (firescraper regression, query path)."""
    g = _contract_graph()
    # Degrade typescript calls while python stays healthy.
    g.add_node("t", kind="function", name="t", source_file="t.ts")
    for i in range(9):
        ph = f"tsph{i}"
        g.add_node(ph, kind="unknown")
        g.add_edge("t", ph, relation="calls", source_file="t.ts")
    # Enough bound python calls to keep the AGGREGATE ratio >= 0.8
    # (9 unbound ts + 41 bound py -> calls 41/50 = 0.82): this is the
    # firescraper shape — aggregate healthy, ts cell degraded.
    for i in range(40):
        fn = f"py_fn{i}"
        g.add_node(fn, kind="function", name=f"f{i}", source_file="a.py")
        g.add_edge(fn, "b_fn", relation="calls", source_file="a.py")
    result = execute_plan(g, make_plan("callers", [("node", "t")], {}))
    assert result["total"] == 0
    assert result["answer"]["grade"] == "inconclusive"
    assert result["inconclusive"] is True
    assert result["resolution_health"]["healthy"] is True  # aggregate masks; scoped does not
