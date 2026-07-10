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
