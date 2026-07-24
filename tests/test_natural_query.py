"""Deterministic grammar-only natural-language query translation."""
from __future__ import annotations

import json

import pytest

from graphite.cli import main
from graphite.graph import build_graph
from graphite.natural_query import (
    NATURAL_RULES,
    answer_natural,
    natural_catalog,
    translate_natural,
)
from graphite.query import QUERY_VERBS, query
from graphite.query_plan import plan_error


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


@pytest.mark.parametrize(
    ("question", "operation", "inputs"),
    [
        ("who calls helper", "callers", ["helper"]),
        ("What calls Helper?", "callers", ["helper"]),
        ("callers of helper", "callers", ["helper"]),
        ("who uses the helper function", "callers", ["helper"]),
        ("what does main call", "calls", ["main"]),
        ("callees of main", "calls", ["main"]),
        ("what does app.ts depend on", "depends-on", ["app.ts"]),
        ("dependencies of app.ts", "depends-on", ["app.ts"]),
        ("what does app.ts import", "depends-on", ["app.ts"]),
        ("what depends on lib.ts", "imported-by", ["lib.ts"]),
        ("who imports lib.ts", "imported-by", ["lib.ts"]),
        ("dependents of lib.ts", "imported-by", ["lib.ts"]),
        ("where is lib.ts imported", "imported-by", ["lib.ts"]),
        ("can main reach helper", "reaches", ["main", "helper"]),
        ("is there a call path from main to helper", "reaches", ["main", "helper"]),
        ("does main eventually call helper", "reaches", ["main", "helper"]),
        ("path from app.ts to lib.ts", "path", ["app.ts", "lib.ts"]),
        ("is there a path from app.ts to lib.ts", "path", ["app.ts", "lib.ts"]),
        ("how does app.ts reach lib.ts", "path", ["app.ts", "lib.ts"]),
        ("graph stats", "stats", []),
        ("how big is the graph?", "stats", []),
    ],
)
def test_grammar_translates_questions_to_valid_plans(question, operation, inputs) -> None:
    translated = translate_natural(question)
    assert "error" not in translated, translated
    plan = translated["plan"]
    assert plan["operation"] == operation
    assert [t["input"] for t in plan["targets"]] == inputs
    roles = {spec.name: spec.roles for spec in QUERY_VERBS}
    assert plan_error(plan, roles) is None
    assert translated["natural"]["intent"] == operation
    assert translated["schema_version"] == 1


@pytest.mark.parametrize(
    ("question", "intent", "command", "target"),
    [
        ("what breaks if I change db.ts", "impact", "impact", "db.ts"),
        ("impact of changing db.ts", "impact", "impact", "db.ts"),
        ("blast radius of db.ts", "impact", "impact", "db.ts"),
        ("what tests cover db.ts", "tests", "impact", "db.ts"),
        ("tests for db.ts", "tests", "impact", "db.ts"),
        ("context for db.ts", "context", "context", "db.ts"),
        ("tell me about db.ts", "context", "context", "db.ts"),
    ],
)
def test_grammar_redirects_command_intents(question, intent, command, target) -> None:
    translated = translate_natural(question)
    assert translated["natural"]["intent"] == intent
    assert translated["suggestion"]["command"] == ["graphite", command, target]
    assert "plan" not in translated


def test_unmatched_question_falls_back_to_search_with_stripped_terms() -> None:
    translated = translate_natural("how does pairing work")
    assert translated["natural"]["intent"] == "search"
    assert translated["natural"]["pattern"] is None
    assert translated["suggestion"]["command"] == ["graphite", "search", "pairing work"]


def test_degenerate_capture_fails_closed_to_search() -> None:
    translated = translate_natural("who calls the")
    assert translated["natural"]["intent"] == "search"


def test_empty_and_termless_questions_return_stable_errors() -> None:
    assert translate_natural("")["error_code"] == "empty_query"
    assert translate_natural("   ?")["error_code"] == "empty_query"
    no_terms = translate_natural("what is this")
    assert no_terms["error_code"] == "natural_no_terms"
    assert any("capabilities" in s for s in no_terms["suggestions"])


def test_translation_is_deterministic() -> None:
    for q in ("who calls helper", "how does pairing work", "tests for db.ts"):
        assert translate_natural(q) == translate_natural(q)


def test_answer_natural_executes_verb_plans_like_structured_query() -> None:
    g = _graph()
    answered = answer_natural(g, "who calls helper?")
    structured = query(g, "callers helper")
    assert answered["natural"]["intent"] == "callers"
    assert answered["plan"]["operation"] == "callers"
    without_wrapper = {k: v for k, v in answered.items() if k not in ("natural", "plan")}
    assert without_wrapper == structured


def test_answer_natural_passes_through_resolution_errors_with_candidates() -> None:
    g = _graph()
    answered = answer_natural(g, "who calls zzqx")
    assert answered["error_code"] == "node_not_found"
    assert "candidates" in answered
    assert answered["natural"]["intent"] == "callers"
    assert answered["plan"]["operation"] == "callers"


def test_answer_natural_runs_search_fallback_as_clarification() -> None:
    g = _graph()
    answered = answer_natural(g, "something about the helper maybe")
    assert answered["natural"]["intent"] == "search"
    assert answered["search"]["ok"] is True
    assert any(r["id"] == "src_lib_helper" for r in answered["search"]["results"])


def test_answer_natural_does_not_execute_command_intents() -> None:
    g = _graph()
    answered = answer_natural(g, "what breaks if I change lib.ts")
    assert answered["suggestion"]["command"] == ["graphite", "impact", "lib.ts"]
    assert "impacted_files" not in answered


def test_cli_natural_translates_and_executes(tmp_path, monkeypatch, capsys) -> None:
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "app.ts").write_text(
        "export function helper() { return 1; }\n"
        "export function main() { return helper(); }\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["build", "."]) == 0
    capsys.readouterr()

    assert main(["query", "who calls helper?", "--natural"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["natural"]["intent"] == "callers"
    assert payload["plan"]["operation"] == "callers"
    assert "callers" in payload

    assert main(["query", "something about the helper maybe", "--natural"]) == 0
    fallback = json.loads(capsys.readouterr().out)
    assert fallback["natural"]["intent"] == "search"
    assert fallback["search"]["ok"] is True


def test_cli_natural_plan_only_and_suggestions_need_no_graph(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)  # deliberately no graph.json anywhere

    assert main(["query", "who calls helper", "--natural", "--plan-only"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan"]["operation"] == "callers"
    assert payload["natural"]["pattern"] == "who calls <symbol>"

    assert main(["query", "tests for db.ts", "--natural"]) == 0
    suggestion = json.loads(capsys.readouterr().out)
    assert suggestion["suggestion"]["command"] == ["graphite", "impact", "db.ts"]

    assert main(["query", "what is this", "--natural"]) == 0
    err = json.loads(capsys.readouterr().out)
    assert err["error_code"] == "natural_no_terms"


def test_cli_natural_stays_inside_canonical_gate(capsys) -> None:
    assert main(["--llm", "local", "query", "who calls x", "--natural"]) == 2
    capsys.readouterr()


def test_catalog_matches_rules_and_registry() -> None:
    catalog = natural_catalog()
    listed_templates = [t for entry in catalog for t in entry["templates"]]
    assert listed_templates == [rule.template for rule in NATURAL_RULES]
    verb_names = {spec.name for spec in QUERY_VERBS}
    for entry in catalog:
        if entry["kind"] == "operation":
            assert entry["intent"] in verb_names, entry["intent"]
        else:
            assert entry["intent"] in {"impact", "tests", "context"}
