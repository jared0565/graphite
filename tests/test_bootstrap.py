"""Tests for Graphite project bootstrap."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.bootstrap import bootstrap_project
from graphite.cli import main


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_bootstrap_creates_gitignore_and_agents_idempotently(tmp_path: Path) -> None:
    _write(tmp_path / ".gitignore", "node_modules/\n")

    first = bootstrap_project(tmp_path).to_dict()
    second = bootstrap_project(tmp_path).to_dict()

    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")

    assert first["gitignore"]["changed"] is True
    assert first["agents"]["changed"] is True
    assert second["gitignore"]["changed"] is False
    assert second["agents"]["changed"] is False
    assert "node_modules/" in gitignore
    assert "graph-out/" in gitignore
    # unanchored form so nested workspace caches are ignored too
    assert "**/.cache/graphite/" in gitignore
    assert "## Automatic Graphite Consult" in agents
    # `python -m graphite` is the shell- and agent-agnostic invocation form.
    assert "python -m graphite context <likely-changed-file>" in agents
    # No machine-specific install path may leak into project files.
    assert "_tools" not in agents


def test_bootstrap_preserves_existing_agents_content(tmp_path: Path) -> None:
    _write(tmp_path / "AGENTS.md", "# Existing Notes\n\nKeep this line.\n")

    result = bootstrap_project(tmp_path).to_dict()
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")

    assert result["agents"]["changed"] is True
    assert "Keep this line." in text
    assert "## Automatic Graphite Consult" in text


def test_bootstrap_reports_daemon_visibility(tmp_path: Path) -> None:
    base = tmp_path / "Projects"
    project = base / "app"
    project.mkdir(parents=True)
    status_dir = base / ".graphite-daemon"
    status_dir.mkdir()
    (status_dir / "status.json").write_text(
        json.dumps({
            "status": "ok",
            "project_count": 1,
            "projects": [{
                "root": str(project.resolve()),
                "build_count": 2,
                "failure_count": 0,
                "last_error": None,
                "file_count": 3,
            }],
        }),
        encoding="utf-8",
    )

    result = bootstrap_project(project, daemon_base=base).to_dict()

    assert result["daemon"]["status_found"] is True
    assert result["daemon"]["project_listed"] is True
    assert result["daemon"]["project_status"]["build_count"] == 2


def test_bootstrap_cli_builds_and_validates_graph(tmp_path: Path, capsys) -> None:
    _write(tmp_path / "src" / "app.ts", "export const app = 1;\n")

    result = main(["bootstrap", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["gitignore"]["changed"] is True
    assert payload["agents"]["changed"] is True
    assert payload["build"]["ok"] is True
    assert payload["validation"]["ok"] is True
    assert (tmp_path / "graph-out" / "graph.json").exists()


def test_bootstrap_cli_no_build_skips_validation(tmp_path: Path, capsys) -> None:
    result = main(["bootstrap", str(tmp_path), "--no-build", "--no-validate", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["build"]["requested"] is False
    assert payload["validation"]["requested"] is False
    assert not (tmp_path / "graph-out").exists()

