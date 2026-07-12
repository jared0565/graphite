"""Tests for Graphify-to-Graphite replacement audit."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.bootstrap import bootstrap_project
from graphite.cli import main
from graphite.replacement_audit import audit_replacement


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _health_ok(base: Path, state_dir: Path | None) -> dict:
    return {
        "ok": True,
        "status": "ok",
        "summary": {"error_count": 0, "warning_count": 0},
        "errors": [],
        "warnings": [],
    }


def _daemon_status(base: Path, project: Path) -> None:
    state = base / ".graphite-daemon"
    state.mkdir(parents=True, exist_ok=True)
    (state / "status.json").write_text(
        json.dumps({
            "status": "ok",
            "project_count": 1,
            "projects": [{
                "root": str(project.resolve()),
                "build_count": 1,
                "failure_count": 0,
                "last_error": None,
                "file_count": 2,
            }],
        }),
        encoding="utf-8",
    )


def test_audit_replacement_clean_project_is_ready(tmp_path: Path, monkeypatch, capsys) -> None:
    _write(tmp_path / "src" / "app.ts", "export const app = 1;\n")
    bootstrap_project(tmp_path, daemon_base=tmp_path)
    _daemon_status(tmp_path, tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["build", "."]) == 0
    capsys.readouterr()

    report = audit_replacement(tmp_path, daemon_base=tmp_path, health_checker=_health_ok)

    assert report["ok"] is True
    assert report["replacement_ready"] is True
    assert report["blockers"] == []
    assert report["warnings"] == []


def test_audit_replacement_reports_graphify_remnants_as_warnings(tmp_path: Path, monkeypatch, capsys) -> None:
    _write(tmp_path / "src" / "app.ts", "export const app = 1;\n")
    _write(tmp_path / "graphify" / "legacy.txt", "old\n")
    _write(tmp_path / ".gitignore", "graphify-out/\ngraphify/\nscripts/graphify_*.py\n")
    bootstrap_project(tmp_path, daemon_base=tmp_path)
    _daemon_status(tmp_path, tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["build", "."]) == 0
    capsys.readouterr()

    report = audit_replacement(tmp_path, daemon_base=tmp_path, health_checker=_health_ok)
    warning_codes = {item["code"] for item in report["warnings"]}

    assert report["ok"] is True
    assert report["replacement_ready"] is False
    assert "graphify_paths_exist" in warning_codes
    assert "graphify_gitignore_entries" in warning_codes


def test_audit_replacement_reports_graphite_blockers(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app.ts", "export const app = 1;\n")

    report = audit_replacement(tmp_path, daemon_base=tmp_path, health_checker=_health_ok)
    blocker_codes = {item["code"] for item in report["blockers"]}

    assert report["ok"] is False
    assert "agents_not_configured" in blocker_codes
    assert "gitignore_not_configured" in blocker_codes
    assert "graph_missing" in blocker_codes
    assert "daemon_project_missing" in blocker_codes


def test_audit_replacement_cli_json_and_fail_on_blocker(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("graphite.cli.audit_replacement", lambda root, daemon_base, cfg: {
        "ok": False,
        "replacement_ready": False,
        "project_root": str(root),
        "blockers": [{"code": "graph_missing", "message": "missing"}],
        "warnings": [],
        "recommendations": [],
        "graphite": {},
        "graphify": {},
    })

    result = main(["audit-replacement", str(tmp_path), "--json", "--fail-on-blocker"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 1
    assert payload["blockers"][0]["code"] == "graph_missing"
