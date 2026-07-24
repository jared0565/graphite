"""Published integration schemas stay in lockstep with live command outputs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite.cli import main
from graphite.graph import build_graph
from graphite.natural_query import answer_natural, translate_natural
from graphite.query import QUERY_VERBS, build_plan, query, search_graph
from graphite.query_plan import PLAN_SCHEMA
from graphite.routing.schema_validation import is_supported_schema, matches_schema

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "docs" / "schemas"
_METADATA_KEYS = ("$schema", "$id", "title", "description")
_ALL_SCHEMAS = (
    "query-plan.v1.schema.json",
    "query-result.v1.schema.json",
    "search-result.v1.schema.json",
    "capabilities.v1.schema.json",
)


def _load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


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


@pytest.mark.parametrize("name", _ALL_SCHEMAS)
def test_published_schemas_stay_inside_subset_validator(name: str) -> None:
    """Published schemas must validate with graphite's zero-dep fail-closed
    subset validator (no combinators), so consumers can reuse it verbatim."""
    assert is_supported_schema(_load(name)) is True


def test_published_plan_schema_matches_code_constant() -> None:
    published = _load("query-plan.v1.schema.json")
    body = {k: v for k, v in published.items() if k not in _METADATA_KEYS}
    assert body == PLAN_SCHEMA, "published plan schema drifted from query_plan.PLAN_SCHEMA"


def test_plans_for_every_verb_match_published_schema() -> None:
    schema = _load("query-plan.v1.schema.json")
    for spec in QUERY_VERBS:
        if spec.roles == ():
            q = spec.name
        elif spec.roles == ("node",):
            q = f"{spec.name} helper"
        else:
            q = f"{spec.name} a -> b"
        plan = build_plan(q)
        assert "error" not in plan, spec.name
        assert matches_schema(plan, schema) is True, spec.name


def test_query_outputs_match_published_result_schema() -> None:
    schema = _load("query-result.v1.schema.json")
    g = _graph()
    samples = (
        "callers helper",
        "calls main",
        "depends-on src_app",
        "imported-by src_lib",
        "reaches main -> helper",
        "path src_app -> src_lib",
        "community-of src_app",
        "stats",
        "",
        "bogus verb",
        "callers zzqx",
        "callers src/app",  # not found, but with non-empty clarification candidates
        "callers",
        "path src_app",
        "reaches src_lib -> src_app",
    )
    for q in samples:
        assert matches_schema(query(g, q), schema) is True, q


def test_natural_outputs_match_published_result_schema() -> None:
    result_schema = _load("query-result.v1.schema.json")
    search_schema = _load("search-result.v1.schema.json")
    g = _graph()
    answered = (
        "who calls helper?",
        "what breaks if I change lib.ts",
        "something about the helper maybe",
        "what is this",
        "who calls zzqx",
    )
    for q in answered:
        assert matches_schema(answer_natural(g, q), result_schema) is True, q
    for q in ("who calls helper", "tests for db.ts", "how does pairing work"):
        assert matches_schema(translate_natural(q), result_schema) is True, q

    fallback = answer_natural(g, "something about the helper maybe")
    assert matches_schema(fallback["search"], search_schema) is True


def test_search_outputs_match_published_schema() -> None:
    schema = _load("search-result.v1.schema.json")
    g = _graph()
    for text in ("helper", "src/app.ts", "pairing accept", "zzqx", "   "):
        assert matches_schema(search_graph(g, text), schema) is True, text


def test_capabilities_output_matches_published_schema(capsys) -> None:
    assert main(["capabilities", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert matches_schema(payload, _load("capabilities.v1.schema.json")) is True
