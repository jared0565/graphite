"""Tests for Graphify-to-Graphite replacement audit."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite.bootstrap import bootstrap_project
from graphite.cli import main
from graphite.replacement_audit import audit_replacement, format_replacement_audit


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


def _report_with_health(status: str, *, ok: bool) -> dict:
    """A minimal report carrying one daemon-health verdict.

    `ok` is deliberately settable apart from `status`: reproducing #25 needs
    the warnings-only state the daemon actually produces, where `ok` is
    `not errors` (so True) while `status` is already "warning".
    """
    return {
        "replacement_ready": True,
        # Not a real path and deliberately not /tmp-shaped: it is only echoed
        # into a line these tests never assert on, and a temp-looking literal
        # trips ruff S108 (which stays enabled for tests/ on purpose).
        "project_root": "project-root",
        "blockers": [],
        "warnings": [],
        "recommendations": [],
        "graphite": {
            "graph": {"exists": True, "valid": True, "stale": False},
            "daemon": {"project_listed": True},
            "health": {"checked": True, "ok": ok, "status": status},
        },
        "graphify": {"existing_paths": [], "text_references": [], "gitignore_entries": []},
    }


def _health_line(status: str, *, ok: bool) -> str:
    rendered = format_replacement_audit(_report_with_health(status, ok=ok))
    return next(ln for ln in rendered.splitlines() if "daemon health:" in ln)


def test_warnings_only_daemon_health_line_does_not_contradict_itself() -> None:
    """#25: `ok` is `not errors`, so a warnings-only daemon rendered
    `daemon health: warning (ok)` -- the two halves disagreeing about whether
    anything needs attention."""
    line = _health_line("warning", ok=True)

    assert "(ok)" not in line
    assert "warning" in line


@pytest.mark.parametrize(
    ("status", "ok", "expected"),
    [
        ("ok", True, "  - daemon health: ok (no action needed)"),
        ("warning", True, "  - daemon health: warning (attention suggested)"),
        ("degraded", False, "  - daemon health: degraded (attention required)"),
    ],
)
def test_daemon_health_line_is_keyed_off_status(status: str, ok: bool, expected: str) -> None:
    assert _health_line(status, ok=ok) == expected


def test_unrecognised_daemon_health_status_does_not_read_as_fine() -> None:
    """An unknown status must not render as reassuring. Fail closed: a tier
    this code has never heard of is exactly when a human should look."""
    line = _health_line("some-future-tier", ok=True)

    assert "no action needed" not in line
    assert "attention required" in line
