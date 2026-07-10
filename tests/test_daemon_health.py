"""Tests for Graphite daemon health checks."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from graphite.cli import main
from graphite.daemon_health import HealthOptions, evaluate_daemon_health


def _write_status(base: Path, payload: dict) -> None:
    state = base / ".graphite-daemon"
    state.mkdir(parents=True, exist_ok=True)
    (state / "status.json").write_text(json.dumps(payload), encoding="utf-8")


def _status(updated_at: str, projects: list[dict] | None = None, status: str = "ok") -> dict:
    projects = projects or [{
        "root": "F:/Projects/app",
        "file_count": 2,
        "build_count": 1,
        "failure_count": 0,
        "last_success_at": "2026-06-23T12:00:00+00:00",
        "last_error": None,
        "needs_initial_build": False,
    }]
    return {
        "status": status,
        "updated_at": updated_at,
        "project_count": len(projects),
        "failing_projects": sum(1 for p in projects if p.get("last_error")),
        "pending_projects": sum(1 for p in projects if p.get("needs_initial_build")),
        "projects": projects,
    }


def test_daemon_health_ok_when_status_process_and_startup_are_good(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, 12, 1, tzinfo=timezone.utc)
    _write_status(tmp_path, _status("2026-06-23T12:00:30+00:00"))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(max_status_age_seconds=120, max_project_success_age_seconds=3600),
        now=now,
        process_checker=lambda base: {"checked": True, "running": True, "process_count": 1, "processes": []},
        startup_checker=lambda base, name: {"checked": True, "supported": True, "installed": True},
    )

    assert report["ok"] is True
    assert report["status"] == "ok"
    assert report["summary"]["error_count"] == 0
    assert report["summary"]["warning_count"] == 0


def test_daemon_health_reports_stale_status_and_missing_process(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, 12, 10, tzinfo=timezone.utc)
    _write_status(tmp_path, _status("2026-06-23T12:00:00+00:00"))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(max_status_age_seconds=60, require_startup=False),
        now=now,
        process_checker=lambda base: {"checked": True, "running": False, "process_count": 0, "processes": []},
    )

    codes = {issue["code"] for issue in report["errors"]}
    assert report["ok"] is False
    assert report["status"] == "degraded"
    assert "status_stale" in codes
    assert "daemon_process_not_running" in codes


def test_daemon_health_reports_failing_pending_and_old_projects(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    projects = [
        {
            "root": "F:/Projects/bad",
            "file_count": 2,
            "build_count": 2,
            "failure_count": 1,
            "last_success_at": "2026-06-23T11:59:00+00:00",
            "last_error": "boom",
            "needs_initial_build": False,
        },
        {
            "root": "F:/Projects/pending",
            "file_count": 2,
            "build_count": 0,
            "failure_count": 0,
            "last_success_at": None,
            "last_error": None,
            "needs_initial_build": True,
        },
        {
            "root": "F:/Projects/old",
            "file_count": 2,
            "build_count": 1,
            "failure_count": 0,
            "last_success_at": "2026-06-20T12:00:00+00:00",
            "last_error": None,
            "needs_initial_build": False,
        },
    ]
    _write_status(tmp_path, _status("2026-06-23T11:59:30+00:00", projects=projects))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(max_project_success_age_seconds=3600, require_process=False, require_startup=False),
        now=now,
    )

    error_codes = {issue["code"] for issue in report["errors"]}
    warning_codes = {issue["code"] for issue in report["warnings"]}
    assert "project_failing" in error_codes
    assert "project_pending_initial_build" in warning_codes
    assert "project_not_built_recently" in warning_codes
    assert report["summary"]["failing_count"] == 1
    assert report["summary"]["pending_count"] == 1
    assert report["summary"]["not_built_recently_count"] == 2


def test_daemon_health_cli_fail_on_error(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("graphite.cli.evaluate_daemon_health", lambda base, state_dir, options: {
        "ok": False,
        "status": "degraded",
        "base_path": str(base),
        "status_path": str(base / ".graphite-daemon" / "status.json"),
        "status_age_seconds": None,
        "summary": {"project_count": 0, "failing_count": 0, "pending_count": 0, "not_built_recently_count": 0},
        "errors": [{"code": "status_missing", "message": "missing"}],
        "warnings": [],
        "process": {"checked": False},
        "startup": {"checked": False},
    })

    result = main(["daemon-health", str(tmp_path), "--fail-on-error"])
    output = capsys.readouterr().out

    assert result == 1
    assert "daemon health: degraded" in output
