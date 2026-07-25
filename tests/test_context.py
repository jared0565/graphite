"""Tests for Graphite agent context summaries."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.cli import main


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sample_project(root: Path) -> None:
    _write(root / "src" / "lib.ts", "export function add(a: number, b: number): number { return a + b; }\n")
    _write(root / "src" / "app.ts", "import { add } from './lib';\nexport const total = add(1, 2);\n")
    _write(root / "src" / "app.test.ts", "import { total } from './app';\ntest('total', () => expect(total).toBe(3));\n")


def test_context_cli_json_returns_dependencies_dependents_and_impact(tmp_path: Path, monkeypatch, capsys) -> None:
    _sample_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["build", "."]) == 0
    capsys.readouterr()

    result = main(["context", "src/lib.ts", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["missing"] == []
    assert output["matched"][0]["input"] == "src/lib.ts"
    matched_id = output["matched"][0]["node"]["id"]
    dependents = output["direct_dependents"][matched_id]
    assert any(item["source_file"] == "src/app.ts" for item in dependents)
    assert "src/app.ts" in output["impact"]["impacted_files"]
    assert "src/app.test.ts" in output["impact"]["likely_tests"]


def test_context_cli_markdown_is_compact_and_human_readable(tmp_path: Path, monkeypatch, capsys) -> None:
    _sample_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["build", "."]) == 0
    capsys.readouterr()

    result = main(["context", "src/app.ts"])
    output = capsys.readouterr().out

    assert result == 0
    assert "# Graphite Context" in output
    assert "## Impact" in output
    assert "## Direct Dependencies" in output
    assert "src/app.ts" in output


def _trust_graph(healthy: bool):
    import networkx as nx

    g = nx.DiGraph()
    g.add_node("lonely", kind="function", source_file="a.py")
    g.add_node("src", kind="function", source_file="a.py")
    target_kind = "function" if healthy else "unknown"
    g.add_node("tgt", kind=target_kind, source_file="b.py")
    g.add_edge("src", "tgt", relation="calls", source_file="a.py")
    return g


def test_context_marks_inconclusive_on_unhealthy_graph():
    from graphite.context import build_context, format_context_markdown

    context = build_context(_trust_graph(healthy=False), ["lonely"])
    assert context["resolution_health"]["healthy"] is False
    assert context["inconclusive"] is True
    text = format_context_markdown(context)
    assert "INCONCLUSIVE" in text
    assert "no direct dependents found — inconclusive (resolution health low)" in text
    assert "Impacted files: none found\n" not in text


def test_context_unchanged_on_healthy_graph():
    from graphite.context import build_context, format_context_markdown

    context = build_context(_trust_graph(healthy=True), ["lonely"])
    assert context["inconclusive"] is False
    text = format_context_markdown(context)
    assert "INCONCLUSIVE" not in text
    assert "Impacted files: none found" in text


def test_context_notes_incomplete_when_nonempty_but_unhealthy():
    import networkx as nx

    from graphite.context import build_context, format_context_markdown

    g = nx.DiGraph()
    g.add_node("caller_file", kind="file", source_file="caller.py")
    g.add_node("target_file", kind="file", source_file="target.py")
    g.add_node("ghost", kind="unknown")
    g.add_edge("caller_file", "target_file", relation="imports", source_file="caller.py")
    g.add_edge("caller_file", "ghost", relation="calls", source_file="caller.py")
    context = build_context(g, ["target_file"])
    text = format_context_markdown(context)
    assert "may be incomplete" in text
    assert "INCONCLUSIVE" not in text


def test_context_carries_full_health_block():
    import networkx as nx

    from graphite.context import build_context

    context = build_context(nx.DiGraph(), [])
    block = context["resolution_health"]
    assert set(block) >= {"schema", "placeholder_nodes", "by_relation", "by_language", "healthy", "threshold"}
