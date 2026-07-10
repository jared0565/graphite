"""Tests for Graphite AI platform initialization."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.cli import main
from graphite.init import init_project, resolve_platform_selection


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_init_project_creates_shared_graphite_and_platform_files(tmp_path: Path) -> None:
    _write(tmp_path / ".gitignore", "/*\n!/.gitignore\n")

    first = init_project(tmp_path, platforms=["codex", "claude", "visual-studio"]).to_dict()
    second = init_project(tmp_path, platforms=["codex", "claude", "visual-studio"]).to_dict()

    assert first["graphite_doc"]["changed"] is True
    assert second["graphite_doc"]["changed"] is False
    graphite_doc = (tmp_path / "GRAPHITE.md").read_text(encoding="utf-8")
    assert "## Optional LLM Enrichment" in graphite_doc
    assert "GRAPHITE_LLM_API_KEY" in graphite_doc
    assert "Follow `GRAPHITE.md`" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "Follow `GRAPHITE.md`" in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Follow `GRAPHITE.md`" in (tmp_path / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")

    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "!/GRAPHITE.md" in gitignore
    assert "!/AGENTS.md" in gitignore
    assert "!/CLAUDE.md" in gitignore
    assert "!/.github/" in gitignore
    assert "!/.github/copilot-instructions.md" in gitignore


def test_init_project_preserves_existing_instruction_content(tmp_path: Path) -> None:
    _write(tmp_path / "CLAUDE.md", "# Existing Claude Notes\n\nKeep this line.\n")

    result = init_project(tmp_path, platforms=["claude"]).to_dict()
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    assert result["platform_files"][0]["changed"] is True
    assert "Keep this line." in text
    assert "Follow `GRAPHITE.md`" in text


def test_resolve_platform_selection_accepts_aliases_numbers_and_all() -> None:
    assert resolve_platform_selection(["1, claude-code, copilot"]) == ("codex", "claude", "visual-studio")
    assert "windsurf" in resolve_platform_selection(["all"])
    assert resolve_platform_selection(["gemini-cli"]) == ("gemini",)


def test_init_gemini_platform_writes_gemini_md(tmp_path: Path) -> None:
    result = init_project(tmp_path, platforms=["gemini"]).to_dict()

    text = (tmp_path / "GEMINI.md").read_text(encoding="utf-8")
    assert result["platforms"] == ["gemini"]
    assert "Follow `GRAPHITE.md`" in text


def test_graphite_doc_uses_shell_agnostic_invocation(tmp_path: Path) -> None:
    init_project(tmp_path, platforms=["claude"])
    doc = (tmp_path / "GRAPHITE.md").read_text(encoding="utf-8")

    assert "python -m graphite check ." in doc
    assert "python -m graphite context <target-file>" in doc
    # No machine-specific install path may leak into project files.
    assert "_tools" not in doc
    assert "F:\\Projects" not in doc


def test_init_cli_json_no_build(tmp_path: Path, capsys) -> None:
    result = main([
        "init",
        str(tmp_path),
        "--platform",
        "codex,claude",
        "--no-build",
        "--no-validate",
        "--json",
    ])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["platforms"] == ["codex", "claude"]
    assert payload["build"]["requested"] is False
    assert payload["validation"]["requested"] is False
    graphite_doc = (tmp_path / "GRAPHITE.md").read_text(encoding="utf-8")
    assert "## Optional LLM Enrichment" in graphite_doc
    assert "GRAPHITE_LLM_API_KEY" in graphite_doc
    assert (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / "CLAUDE.md").exists()


def test_init_cli_list_platforms(capsys) -> None:
    result = main(["init", "--list-platforms"])
    out = capsys.readouterr().out

    assert result == 0
    assert "codex" in out
    assert "claude" in out
    assert "visual-studio" in out
