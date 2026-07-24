"""Canonical query plans: schema gate, deterministic construction, validation."""
from __future__ import annotations

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
        "options": {},
        "source": {"mode": "local-graph"},
    }
    assert build_plan("reaches main -> helper") == {
        "plan_version": 1,
        "operation": "reaches",
        "targets": [
            {"role": "source", "input": "main"},
            {"role": "target", "input": "helper"},
        ],
        "options": {},
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


def test_query_via_plan_matches_direct_execution() -> None:
    g = _graph()
    for q in ("callers helper", "reaches main -> helper", "stats", "community-of src_app"):
        assert query(g, q) == execute_plan(g, build_plan(q))
