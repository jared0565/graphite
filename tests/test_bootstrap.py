"""Tests for Graphite project bootstrap."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite.bootstrap import bootstrap_project
from graphite.cli import main
from graphite.typescript_activation import ActivationOutcome, ActivationResult


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


def test_projects_root_env_overrides_defaults(tmp_path: Path, monkeypatch) -> None:
    from graphite.bootstrap import _default_daemon_base
    from graphite.config import default_projects_root

    root = tmp_path / "Workspace"
    monkeypatch.setenv("GRAPHITE_PROJECTS_ROOT", str(root))

    assert default_projects_root() == root
    assert _default_daemon_base(tmp_path / "some" / "app") == root


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


def test_bootstrap_json_includes_activation_and_yes_is_noninteractive(tmp_path, capsys, monkeypatch) -> None:
    calls = []
    expected = ActivationResult(ActivationOutcome.GUIDANCE_ONLY, None, "non_interactive")
    monkeypatch.setattr("graphite.cli.activate_typescript", lambda request: calls.append(request) or expected)

    result = main(["bootstrap", str(tmp_path), "--yes", "--no-build", "--no-validate", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["typescript_activation"] == expected.to_dict()
    assert calls[0].assume_yes is True


def test_bootstrap_orders_onboarding_activation_then_build(tmp_path, capsys, monkeypatch) -> None:
    events = []
    real_bootstrap = bootstrap_project
    monkeypatch.setattr("graphite.cli.bootstrap_project", lambda *a, **k: events.append("onboarding") or real_bootstrap(*a, **k))
    monkeypatch.setattr("graphite.cli.activate_typescript", lambda request: events.append("activation") or ActivationResult(ActivationOutcome.NOT_APPLICABLE, None, "no_typescript_evidence"))
    monkeypatch.setattr("graphite.cli._build_project", lambda *a, **k: events.append("build"))

    main(["bootstrap", str(tmp_path), "--no-validate", "--json"])
    capsys.readouterr()
    assert events == ["onboarding", "activation", "build"]


@pytest.mark.parametrize(
    "outcome",
    [
        ActivationOutcome.VALIDATION_FAILED,
        ActivationOutcome.INSTALLATION_FAILED,
        ActivationOutcome.VERIFICATION_FAILED,
    ],
)
def test_bootstrap_fatal_activation_still_builds_and_returns_one(
    tmp_path, capsys, monkeypatch, outcome
) -> None:
    built = []
    monkeypatch.setattr(
        "graphite.cli.activate_typescript",
        lambda request: ActivationResult(outcome, None, "dependency_failed"),
    )
    monkeypatch.setattr("graphite.cli._build_project", lambda *a, **k: built.append(True))

    result = main(["bootstrap", str(tmp_path), "--no-validate", "--json"])
    capsys.readouterr()
    assert result == 1
    assert built == [True]
    assert (tmp_path / "AGENTS.md").exists()


def test_bootstrap_human_guidance_is_fixed_and_path_free(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "graphite.cli.activate_typescript",
        lambda request: ActivationResult(ActivationOutcome.GUIDANCE_ONLY, None, "non_interactive"),
    )
    result = main(["bootstrap", str(tmp_path), "--yes", "--no-build", "--no-validate"])
    output = capsys.readouterr().out

    assert result == 0
    expected = [
        "    1. Set GRAPHITE_PACKAGE_VALIDATOR=<absolute-validator-path>.",
        "    2. Fail closed if GRAPHITE_PACKAGE_VALIDATOR is unset, relative, missing, or not a regular file.",
        "    3. Run: node <absolute-validator-path> typescript",
        "    4. With <project-manager>, add local dev dependency typescript with scripts disabled.",
        "    5. Rerun graphite doctor or onboarding to confirm detection.",
    ]
    assert [line for line in output.splitlines() if line.startswith("    ")] == expected
    assert "global install" not in output.lower()
    assert str(tmp_path) not in "\n".join(expected)


def test_bootstrap_ci_masks_terminal_capability(tmp_path, capsys, monkeypatch) -> None:
    calls = []
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("graphite.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("graphite.cli.sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(
        "graphite.cli.activate_typescript",
        lambda request: calls.append(request)
        or ActivationResult(ActivationOutcome.GUIDANCE_ONLY, None, "non_interactive"),
    )

    main(["bootstrap", str(tmp_path), "--no-build", "--no-validate", "--json"])
    capsys.readouterr()
    assert calls[0].stdin_is_tty is False
    assert calls[0].stdout_is_tty is False
