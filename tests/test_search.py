"""Deterministic graph search and machine-readable capability discovery."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.cli import main
from graphite.graph import build_graph
from graphite.query import (
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    QUERY_VERBS,
    _VERB_INDEX,
    search_graph,
    verb_catalog,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _graph():
    nodes = [
        {"id": "src_app", "kind": "file", "name": "app.ts", "source_file": "src/app.ts"},
        {"id": "src_app_main", "kind": "function", "name": "main", "source_file": "src/app.ts"},
        {"id": "web_sync", "kind": "file", "name": "sync.js", "source_file": "web/sync.js"},
        {
            "id": "web_sync_acceptpairing",
            "kind": "function",
            "name": "acceptPairing",
            "source_file": "web/sync.js",
        },
        {
            "id": "web_sync_openscanner",
            "kind": "function",
            "name": "openScanner",
            "source_file": "web/sync.js",
        },
    ]
    edges = [{"source": "src_app", "target": "web_sync", "relation": "imports"}]
    return build_graph(nodes, edges)


def test_search_resolves_symbol_path_id_and_concept() -> None:
    g = _graph()

    by_name = search_graph(g, "acceptPairing")
    assert by_name["ok"] is True
    assert by_name["schema_version"] == 1
    assert by_name["results"][0]["id"] == "web_sync_acceptpairing"
    assert by_name["results"][0]["match_type"] == "name"
    assert by_name["results"][0]["score"] == 1.0

    by_path = search_graph(g, "web/sync.js")
    assert by_path["results"][0]["id"] == "web_sync"
    assert by_path["results"][0]["match_type"] == "path-suffix"
    # symbols in the file rank behind the file node itself
    trailing = {r["id"] for r in by_path["results"][1:]}
    assert "web_sync_acceptpairing" in trailing

    by_id = search_graph(g, "src_app_main")
    assert by_id["results"][0]["id"] == "src_app_main"
    assert by_id["results"][0]["match_type"] == "exact-id"

    concept = search_graph(g, "pairing accept")
    assert any(r["id"] == "web_sync_acceptpairing" for r in concept["results"])
    assert all("match_type" in r and "score" in r for r in concept["results"])

    windows_path = search_graph(g, "web\\sync.js")
    assert windows_path["results"][0]["id"] == "web_sync"


def test_search_is_deterministic_bounded_and_reports_truncation() -> None:
    g = _graph()

    first = search_graph(g, "sync")
    second = search_graph(g, "sync")
    assert first == second

    limited = search_graph(g, "sync", limit=2)
    assert limited["count"] == 2 == len(limited["results"])
    assert limited["total_matches"] > 2
    assert limited["truncated"] is True

    assert search_graph(g, "sync", limit=10_000)["count"] <= MAX_SEARCH_LIMIT


def test_search_no_match_and_empty_input() -> None:
    g = _graph()

    assert search_graph(g, "zzqx") == {
        "ok": True,
        "schema_version": 1,
        "query": "zzqx",
        "count": 0,
        "total_matches": 0,
        "truncated": False,
        "results": [],
    }

    empty = search_graph(g, "   ")
    assert empty["ok"] is False
    assert empty["error_code"] == "empty_search"


def test_search_cli_json_human_and_canonical_gate(tmp_path: Path, monkeypatch, capsys) -> None:
    _write(tmp_path / "src" / "app.ts", "export function main() { return 1; }\n")
    monkeypatch.chdir(tmp_path)
    assert main(["build", "."]) == 0
    capsys.readouterr()

    assert main(["search", "main", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["results"][0]["name"] == "main"

    assert main(["search", "main"]) == 0
    human = capsys.readouterr().out
    assert "main" in human

    assert main(["search", "zzqx"]) == 0
    assert "no matches" in capsys.readouterr().out

    # search is canonical: inference-free, rejects LLM flags outright.
    assert main(["--llm", "local", "search", "main"]) == 2
    capsys.readouterr()


def test_capabilities_json_lists_registry_kinds_and_limits(capsys) -> None:
    assert main(["capabilities", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert payload["schema_version"] == 1
    assert {v["name"] for v in payload["query_verbs"]} == {spec.name for spec in QUERY_VERBS}
    verbs = {v["name"]: v for v in payload["query_verbs"]}
    assert verbs["callers"]["aliases"] == ["called-by", "called_by"]
    assert verbs["stats"]["arguments"] == ""
    assert verbs["callers"]["limits"] == {"max_results": 200}
    assert verbs["reaches"]["limits"] == {"max_depth": 32}
    assert verbs["reaches"]["targets"] == ["source", "target"]
    assert verbs["stats"]["targets"] == []
    assert verbs["stats"]["limits"] == {}
    nl = payload["natural_language"]
    assert nl["available"] is True
    assert nl["mode"] == "deterministic-grammar"
    assert nl["flag"] == "--natural"
    assert nl["providers"] is False
    assert {e["intent"] for e in nl["intents"]} >= {"callers", "path", "impact", "tests", "context"}
    assert payload["search"] == {
        "default_limit": DEFAULT_SEARCH_LIMIT,
        "max_limit": MAX_SEARCH_LIMIT,
    }
    assert payload["query_limits"] == {
        "default_max_depth": 32,
        "default_max_results": 200,
    }
    assert payload["query_plans"] == {
        "plan_version": 1,
        "flags": ["--plan-only", "--show-plan"],
    }
    assert "search" in payload["commands"]
    assert "capabilities" in payload["commands"]
    assert "query" in payload["commands"]
    assert payload["node_kinds"] == ["class", "file", "function", "unknown"]
    assert payload["edge_relations"] == [
        "calls",
        "contains",
        "imports",
        "inherits",
        "references",
        "type_references",
    ]


def test_capabilities_registry_parity_with_dispatch() -> None:
    listed = {v["name"] for v in verb_catalog()}
    listed |= {alias for v in verb_catalog() for alias in v["aliases"]}
    assert listed == set(_VERB_INDEX)


def test_capabilities_human_output_and_canonical_gate(capsys) -> None:
    assert main(["capabilities"]) == 0
    out = capsys.readouterr().out
    assert "callers" in out
    assert "stats" in out
    assert "search" in out

    assert main(["--llm", "local", "capabilities"]) == 2
    capsys.readouterr()
