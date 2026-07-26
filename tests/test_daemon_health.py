"""Tests for Graphite daemon health checks."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from graphite.cli import main
from graphite.daemon import read_daemon_status
from graphite.daemon_health import (
    HealthOptions,
    _normalize_status,
    evaluate_daemon_health,
    format_health_text,
)

MAX_DAEMON_STATUS_BYTES = 4 * 1024 * 1024


def _write_status(base: Path, payload: object) -> None:
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
        "failing_projects": sum(1 for p in projects if p.get("last_error") is not None),
        "pending_projects": sum(1 for p in projects if p.get("needs_initial_build")),
        "projects": projects,
    }


def _project(root: str, **overrides) -> dict:
    project = {
        "root": root,
        "file_count": 2,
        "build_count": 1,
        "failure_count": 0,
        "last_success_at": "2026-06-23T11:59:00+00:00",
        "last_error": None,
        "needs_initial_build": False,
    }
    project.update(overrides)
    return project


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


def test_daemon_health_warns_when_process_observation_is_unavailable(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, 12, 1, tzinfo=timezone.utc)
    _write_status(tmp_path, _status("2026-06-23T12:00:30+00:00"))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(
            max_status_age_seconds=120,
            max_project_success_age_seconds=3600,
            require_startup=False,
        ),
        now=now,
        process_checker=lambda base: {
            "checked": True,
            "supported": True,
            "running": False,
            "processes": [],
            "error": "process observation denied",
        },
    )

    assert report["ok"] is True
    assert report["status"] == "warning"
    assert report["summary"]["error_count"] == 0
    assert {issue["code"] for issue in report["warnings"]} == {
        "daemon_process_check_unavailable"
    }


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


def test_daemon_health_does_not_report_recovered_project_as_failing(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    recovered = _project("F:/Projects/recovered", failure_count=4)
    _write_status(tmp_path, _status("2026-06-23T11:59:30+00:00", projects=[recovered]))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(max_project_success_age_seconds=3600, require_process=False, require_startup=False),
        now=now,
    )

    assert report["projects"] == {"failing": [], "pending": [], "not_built_recently": []}
    assert report["summary"]["failing_count"] == 0
    assert report["status"] == "ok"
    assert {issue["code"] for issue in report["errors"]}.isdisjoint({"project_failing"})


def test_daemon_health_reports_last_error_as_active_failure_after_recent_success(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    failing = _project("F:/Projects/failing", failure_count=7, last_error="still broken")
    _write_status(tmp_path, _status("2026-06-23T11:59:30+00:00", projects=[failing]))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(max_project_success_age_seconds=3600, require_process=False, require_startup=False),
        now=now,
    )

    assert [project["root"] for project in report["projects"]["failing"]] == ["F:/Projects/failing"]
    assert report["projects"]["failing"][0]["failure_count"] == 7
    assert report["summary"]["failing_count"] == 1
    assert [issue["code"] for issue in report["errors"]] == ["project_failing"]


def test_daemon_health_treats_empty_last_error_as_active_failure(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    failing = _project("F:/Projects/empty-error", failure_count=2, last_error="")
    _write_status(tmp_path, _status("2026-06-23T11:59:30+00:00", projects=[failing]))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(max_project_success_age_seconds=3600, require_process=False, require_startup=False),
        now=now,
    )

    assert report["summary"]["failing_count"] == 1
    assert report["projects"]["failing"][0]["last_error"] == ""
    assert [issue["code"] for issue in report["errors"]] == ["project_failing"]


def test_daemon_health_reports_initial_pending_project_as_stale_but_not_failing(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    pending = _project(
        "F:/Projects/initial-pending",
        build_count=0,
        last_success_at=None,
        needs_initial_build=True,
    )
    _write_status(tmp_path, _status("2026-06-23T11:59:30+00:00", projects=[pending]))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(max_project_success_age_seconds=3600, require_process=False, require_startup=False),
        now=now,
    )

    assert report["projects"]["failing"] == []
    assert [project["root"] for project in report["projects"]["pending"]] == ["F:/Projects/initial-pending"]
    assert [project["root"] for project in report["projects"]["not_built_recently"]] == [
        "F:/Projects/initial-pending"
    ]
    assert report["summary"]["failing_count"] == 0
    assert report["summary"]["pending_count"] == 1
    assert report["summary"]["not_built_recently_count"] == 1
    assert [issue["code"] for issue in report["warnings"]] == [
        "project_pending_initial_build",
        "project_not_built_recently",
    ]


def test_daemon_health_text_matches_report_for_recovered_and_empty_error_projects(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    projects = [
        _project("F:/Projects/recovered", failure_count=4),
        _project("F:/Projects/empty-error", failure_count=1, last_error=""),
    ]
    _write_status(tmp_path, _status("2026-06-23T11:59:30+00:00", projects=projects))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(max_project_success_age_seconds=3600, require_process=False, require_startup=False),
        now=now,
    )
    text = format_health_text(report)

    assert report["summary"]["failing_count"] == 1
    assert report["projects"]["failing"][0]["root"] == "F:/Projects/empty-error"
    assert "1 failing" in text
    assert "project_failing: project build failing: F:/Projects/empty-error" in text
    assert "F:/Projects/recovered" not in "\n".join(issue["message"] for issue in report["errors"])
    assert "F:/Projects/recovered" not in text


def test_daemon_health_text_top_line_is_unambiguous_ok(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, 12, 1, tzinfo=timezone.utc)
    _write_status(tmp_path, _status("2026-06-23T12:00:30+00:00"))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(max_status_age_seconds=120, max_project_success_age_seconds=3600),
        now=now,
        process_checker=lambda base: {"checked": True, "running": True, "process_count": 1, "processes": []},
        startup_checker=lambda base, name: {"checked": True, "supported": True, "installed": True},
    )
    text = format_health_text(report)

    assert text.splitlines()[0] == "[graphite] daemon health: ok"


def test_daemon_health_text_counts_issues_and_groups_repeated_warning_codes(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    projects = [_project(f"F:/Projects/p{i}", build_count=0) for i in range(7)]
    _write_status(tmp_path, _status("2026-06-23T11:59:30+00:00", projects=projects))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(max_project_success_age_seconds=3600, require_process=False, require_startup=False),
        now=now,
    )
    text = format_health_text(report)
    warning_count = report["summary"]["warning_count"]

    assert report["status"] == "warning"
    assert "(ok)" not in text
    assert "attention required" not in text
    assert text.splitlines()[0] == (
        f"[graphite] daemon health: warning ({warning_count} warnings, no errors)"
    )
    assert "project_pending_initial_build (7):" in text
    assert text.count("project has not completed initial build") <= 5


def test_daemon_health_text_degraded_top_line_names_counts(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    projects = [_project("F:/Projects/broken", last_error="boom", failure_count=3)]
    _write_status(tmp_path, _status("2026-06-23T11:59:30+00:00", projects=projects))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(max_project_success_age_seconds=3600, require_process=False, require_startup=False),
        now=now,
    )
    text = format_health_text(report)
    first_line = text.splitlines()[0]

    assert report["status"] == "degraded"
    assert first_line.startswith("[graphite] daemon health: degraded (")
    assert "error" in first_line
    assert "attention required" not in text
    # A single occurrence of a code keeps the flat one-line form.
    assert "project_failing: project build failing: F:/Projects/broken" in text


def test_daemon_health_keeps_pending_and_stale_classification_independent(tmp_path: Path) -> None:
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    projects = [
        _project(
            "F:/Projects/pending-by-flag",
            build_count=2,
            needs_initial_build=True,
        ),
        _project("F:/Projects/pending-by-count", build_count=0),
        _project(
            "F:/Projects/old-recovered",
            failure_count=3,
            last_success_at="2026-06-20T12:00:00+00:00",
        ),
        _project(
            "F:/Projects/missing-success-recovered",
            failure_count=5,
            last_success_at=None,
        ),
        _project(
            "F:/Projects/missing-success-failing",
            failure_count=2,
            last_success_at=None,
            last_error="current error",
        ),
    ]
    _write_status(tmp_path, _status("2026-06-23T11:59:30+00:00", projects=projects))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(max_project_success_age_seconds=3600, require_process=False, require_startup=False),
        now=now,
    )

    assert [project["root"] for project in report["projects"]["failing"]] == [
        "F:/Projects/missing-success-failing"
    ]
    assert [project["root"] for project in report["projects"]["pending"]] == [
        "F:/Projects/pending-by-flag",
        "F:/Projects/pending-by-count",
    ]
    assert [project["root"] for project in report["projects"]["not_built_recently"]] == [
        "F:/Projects/old-recovered",
        "F:/Projects/missing-success-recovered",
        "F:/Projects/missing-success-failing",
    ]
    assert report["summary"]["failing_count"] == 1
    assert report["summary"]["pending_count"] == 2
    assert report["summary"]["not_built_recently_count"] == 3
    assert [issue["code"] for issue in report["errors"]] == ["project_failing"]
    assert [issue["code"] for issue in report["warnings"]].count("project_pending_initial_build") == 2
    assert [issue["code"] for issue in report["warnings"]].count("project_not_built_recently") == 3


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


@pytest.mark.parametrize("payload", [[], None, "invalid", 7])
def test_daemon_health_rejects_non_object_status_without_crashing(tmp_path: Path, payload: object) -> None:
    _write_status(tmp_path, payload)

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    assert report["ok"] is False
    assert report["status"] == "degraded"
    assert any(issue["code"] == "status_schema_invalid" for issue in report["errors"])
    assert report["projects"] == {"failing": [], "pending": [], "not_built_recently": []}


@pytest.mark.parametrize("projects", [None, {}, "invalid", 1, True])
def test_daemon_health_rejects_non_list_projects(tmp_path: Path, projects: object) -> None:
    payload = _status("2026-06-23T12:00:00+00:00")
    payload["projects"] = projects
    _write_status(tmp_path, payload)

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    issues = [issue for issue in report["errors"] if issue["code"] == "status_schema_invalid"]
    assert issues == [{"code": "status_schema_invalid", "message": "daemon status schema is invalid", "field": "projects"}]
    assert report["summary"]["project_count"] == 1


@pytest.mark.parametrize("entry", [None, [], "invalid", 3, True])
def test_daemon_health_rejects_non_mapping_project_entries(tmp_path: Path, entry: object) -> None:
    payload = _status("2026-06-23T12:00:00+00:00")
    payload["projects"] = [entry]
    payload["project_count"] = 1
    _write_status(tmp_path, payload)

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    schema_issues = [issue for issue in report["errors"] if issue["code"] == "status_schema_invalid"]
    assert schema_issues == [{"code": "status_schema_invalid", "message": "daemon status schema is invalid", "field": "project", "index": 0}]
    assert report["projects"] == {"failing": [], "pending": [], "not_built_recently": []}


@pytest.mark.parametrize("field", ["build_count", "file_count", "failure_count"])
@pytest.mark.parametrize("value", ["1", True, -1, [], {}, None])
def test_daemon_health_rejects_invalid_project_counts(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    project = _project("F:/Projects/secret-sentinel")
    project[field] = value
    _write_status(tmp_path, _status("2026-06-23T12:00:00+00:00", projects=[project]))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )
    encoded = json.dumps(report)

    issue = next(issue for issue in report["errors"] if issue["code"] == "status_schema_invalid")
    assert issue == {"code": "status_schema_invalid", "message": "daemon status schema is invalid", "field": field, "index": 0}
    assert "secret-sentinel" not in encoded
    assert report["summary"]["project_count"] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("root", ""),
        ("root", 1),
        ("needs_initial_build", 0),
        ("needs_initial_build", "false"),
        ("last_error", 1),
        ("last_error", []),
        ("last_success_at", 1),
        ("last_success_at", []),
    ],
)
def test_daemon_health_rejects_invalid_project_field_types(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    project = _project("F:/Projects/private")
    project[field] = value
    _write_status(tmp_path, _status("2026-06-23T12:00:00+00:00", projects=[project]))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    assert {issue["field"] for issue in report["errors"] if issue["code"] == "status_schema_invalid"} == {field}
    assert report["projects"] == {"failing": [], "pending": [], "not_built_recently": []}


@pytest.mark.parametrize("project_count", ["1", True, -1, [], {}, None])
def test_daemon_health_invalid_project_count_falls_back_to_valid_projects(
    tmp_path: Path,
    project_count: object,
) -> None:
    payload = _status("2026-06-23T12:00:00+00:00", projects=[_project("F:/Projects/valid")])
    payload["project_count"] = project_count
    _write_status(tmp_path, payload)

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    assert report["summary"]["project_count"] == 1
    assert any(issue.get("field") == "project_count" for issue in report["errors"])


def test_daemon_health_mixed_schema_classifies_only_valid_projects_and_formats_safely(tmp_path: Path) -> None:
    secret = "F:/Projects/DO-NOT-LEAK"
    projects = [
        _project("F:/Projects/good", last_error="failed"),
        _project(secret, build_count="bad"),
        "not-a-project",
    ]
    payload = _status("2026-06-23T12:00:00+00:00")
    payload["projects"] = projects
    payload["project_count"] = len(projects)
    _write_status(tmp_path, payload)

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )
    text = format_health_text(report)

    assert report["ok"] is False
    assert [project["root"] for project in report["projects"]["failing"]] == ["F:/Projects/good"]
    assert report["summary"]["project_count"] == 3
    assert len([issue for issue in report["errors"] if issue["code"] == "status_schema_invalid"]) == 2
    assert secret not in json.dumps(report)
    assert secret not in text


def test_daemon_health_schema_issue_cap_does_not_skip_later_valid_projects(tmp_path: Path) -> None:
    invalid_projects = [None] * 25
    valid = _project("F:/Projects/later-valid", last_error="failed")
    payload = _status("2026-06-23T12:00:00+00:00")
    payload["projects"] = [*invalid_projects, valid]
    payload["project_count"] = len(invalid_projects) + 1
    _write_status(tmp_path, payload)

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    schema_issues = [issue for issue in report["errors"] if issue["code"] == "status_schema_invalid"]
    assert len(schema_issues) == 20
    assert [project["root"] for project in report["projects"]["failing"]] == ["F:/Projects/later-valid"]


def test_daemon_health_bounds_project_processing_and_category_details(tmp_path: Path) -> None:
    projects = [
        _project(f"F:/Projects/failing-{index}", last_error="failed")
        for index in range(1_100)
    ]
    projects.append(_project("F:/Projects/after-cap", last_error="must-not-be-processed"))
    payload = _status("2026-06-23T12:00:00+00:00", projects=projects)
    _write_status(tmp_path, payload)

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    assert report["ok"] is False
    assert any(issue["code"] == "status_truncated" for issue in report["errors"])
    assert report["summary"]["projects_processed_count"] == 1_000
    assert report["summary"]["projects_truncated_count"] == 101
    assert report["summary"]["failing_count"] == 1_000
    assert len(report["projects"]["failing"]) == 50
    assert len(report["errors"]) + len(report["warnings"]) <= 100
    assert "after-cap" not in json.dumps(report)
    assert len(json.dumps(report)) < 100_000


def test_daemon_health_rejects_oversized_or_controlled_project_strings_without_leak(tmp_path: Path) -> None:
    secret = "SECRET-OVERSIZED-" + ("X" * 10_000)
    projects = [
        _project(secret, last_error="failed"),
        _project("F:/Projects/control\nINJECT", last_error="failed"),
        _project("F:/Projects/error", last_error=secret),
        _project("F:/Projects/time", last_success_at=secret),
    ]
    _write_status(tmp_path, _status("2026-06-23T12:00:00+00:00", projects=projects))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )
    text = format_health_text(report)
    encoded = json.dumps(report)

    assert len([issue for issue in report["errors"] if issue["code"] == "status_schema_invalid"]) == 4
    assert "SECRET-OVERSIZED" not in encoded
    assert "INJECT" not in encoded
    assert "SECRET-OVERSIZED" not in text
    assert "INJECT" not in text
    assert len(encoded) < 20_000


def test_daemon_health_text_escapes_control_characters_from_daemon_status(tmp_path: Path) -> None:
    payload = _status("2026-06-23T12:00:00+00:00", status="bad\nFORGED\tLINE")
    _write_status(tmp_path, payload)

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )
    text = format_health_text(report)

    assert "bad\\nFORGED\\tLINE" in text
    assert "bad\nFORGED" not in text


@pytest.mark.parametrize("sparse", [False, True])
def test_daemon_health_rejects_oversized_status_before_json_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sparse: bool,
) -> None:
    state = tmp_path / ".graphite-daemon"
    state.mkdir()
    status_path = state / "status.json"
    with status_path.open("wb") as stream:
        if sparse:
            stream.truncate(MAX_DAEMON_STATUS_BYTES + 1)
        else:
            stream.write(b"X" * (MAX_DAEMON_STATUS_BYTES + 1))
    monkeypatch.setattr(
        "graphite.daemon.json.loads",
        lambda value: (_ for _ in ()).throw(AssertionError("oversized status must not parse")),
    )

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    assert [issue["code"] for issue in report["errors"]] == ["status_too_large"]
    assert str(status_path) not in json.dumps(report)


def test_daemon_status_stream_requests_at_most_limit_plus_one() -> None:
    class RecordingStream:
        def __init__(self) -> None:
            self.requests: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.requests.append(size)
            return b"X" * (MAX_DAEMON_STATUS_BYTES + 1 if size < 0 else size)

        def __enter__(self) -> RecordingStream:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    stream = RecordingStream()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("builtins.open", lambda *args, **kwargs: stream)
        with pytest.raises((OSError, json.JSONDecodeError)):
            read_daemon_status(Path("."))
    assert stream.requests == [MAX_DAEMON_STATUS_BYTES + 1]


def test_daemon_health_accepts_exact_limit_utf8_and_rejects_exact_limit_malformed(tmp_path: Path) -> None:
    base_payload = _status("2026-06-23T12:00:00+00:00")
    prefix = json.dumps(base_payload, ensure_ascii=False)[:-1] + ',"padding":"'
    suffix = 'é"}'
    padding_bytes = MAX_DAEMON_STATUS_BYTES - len(prefix.encode()) - len(suffix.encode())
    assert padding_bytes >= 0
    valid = (prefix + ("a" * padding_bytes) + suffix).encode("utf-8")
    assert len(valid) == MAX_DAEMON_STATUS_BYTES
    state = tmp_path / ".graphite-daemon"
    state.mkdir()
    status_path = state / "status.json"
    status_path.write_bytes(valid)

    valid_report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )
    assert not any(issue["code"] in {"status_too_large", "status_unreadable"} for issue in valid_report["errors"])

    status_path.write_bytes(b"{" + (b" " * (MAX_DAEMON_STATUS_BYTES - 1)))
    malformed_report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )
    assert [issue["code"] for issue in malformed_report["errors"]] == ["status_unreadable"]


def test_daemon_health_rejects_invalid_utf8_at_multibyte_boundary(tmp_path: Path) -> None:
    state = tmp_path / ".graphite-daemon"
    state.mkdir()
    (state / "status.json").write_bytes(b'{}' + (b" " * 10) + b"\xe2\x82")

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    assert [issue["code"] for issue in report["errors"]] == ["status_unreadable"]


def test_daemon_health_handles_json_integer_digit_limit_as_unreadable(tmp_path: Path) -> None:
    oversized_integer = "9" * 5_000
    state = tmp_path / ".graphite-daemon"
    state.mkdir()
    (state / "status.json").write_text(
        '{"untrusted_integer":' + oversized_integer + "}",
        encoding="utf-8",
    )

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    assert [issue["code"] for issue in report["errors"]] == ["status_unreadable"]
    assert report["errors"][0]["message"] == "daemon status is unreadable"
    assert oversized_integer not in json.dumps(report)


def test_daemon_health_does_not_hide_downstream_value_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_status(tmp_path, _status("2026-06-23T12:00:00+00:00"))
    monkeypatch.setattr(
        "graphite.daemon_health._normalize_status",
        lambda value: (_ for _ in ()).throw(ValueError("programmer error")),
    )

    with pytest.raises(ValueError, match="programmer error"):
        evaluate_daemon_health(
            tmp_path,
            options=HealthOptions(require_process=False, require_startup=False),
        )


@pytest.mark.parametrize("spoof", ["\u009b", "\u202e", "\u2066"])
@pytest.mark.parametrize("field", ["root", "last_error", "last_success_at"])
def test_daemon_health_rejects_unicode_control_and_format_project_strings(
    tmp_path: Path,
    spoof: str,
    field: str,
) -> None:
    project = _project("F:/Projects/safe")
    project[field] = f"safe{spoof}forged"
    _write_status(tmp_path, _status("2026-06-23T12:00:00+00:00", projects=[project]))

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    assert any(issue.get("field") == field for issue in report["errors"])
    assert "forged" not in json.dumps(report)


def test_daemon_health_text_escapes_unicode_terminal_spoofing(tmp_path: Path) -> None:
    spoofed = "bad\u009bCSI\u202eRTL\u2066ISOLATE"
    _write_status(tmp_path, _status("2026-06-23T12:00:00+00:00", status=spoofed))

    text = format_health_text(evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
    ))

    assert "\\u009b" in text and "\\u202e" in text and "\\u2066" in text
    assert "\u009b" not in text and "\u202e" not in text and "\u2066" not in text


def test_daemon_status_normalization_whitelists_known_fields_only() -> None:
    secret = "UNKNOWN-SECRET-" + ("X" * 10_000)
    raw = _status("2026-06-23T12:00:00+00:00")
    raw["unknown_blob"] = secret
    raw["projects"][0]["unknown_blob"] = secret

    normalized, _, _ = _normalize_status(raw)

    assert "unknown_blob" not in normalized
    assert "unknown_blob" not in normalized["projects"][0]
    assert secret not in json.dumps(normalized)


def test_daemon_health_exposes_only_aggregate_provider_lifecycle_codes(tmp_path: Path) -> None:
    payload = _status("2026-06-23T12:00:30+00:00")
    payload["provider_lifecycle"] = {
        "status": "degraded",
        "attempted": 4,
        "deferred": 0,
        "succeeded": 0,
        "failed": 4,
        "state_counts": {"unavailable": 4},
        "reason_counts": {"runtime_missing": 4},
    }
    _write_status(tmp_path, payload)

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(
            max_status_age_seconds=120,
            require_process=False,
            require_startup=False,
        ),
        now=datetime.fromisoformat("2026-06-23T12:01:00+00:00"),
    )

    assert report["status"] == "warning"
    assert report["provider_lifecycle"] == payload["provider_lifecycle"]
    assert [warning["code"] for warning in report["warnings"]] == [
        "provider_observation_degraded"
    ]


def test_daemon_health_rejects_provider_lifecycle_payload_that_could_leak(tmp_path: Path) -> None:
    secret = "Bearer SECRET from C:/private/provider.exe?token=value"
    payload = _status("2026-06-23T12:00:30+00:00")
    payload["provider_lifecycle"] = {
        "status": "degraded",
        "attempted": 1,
        "deferred": 0,
        "succeeded": 0,
        "failed": 1,
        "state_counts": {"unavailable": 1},
        "reason_counts": {secret: 1},
        "raw_diagnostics": secret,
    }
    _write_status(tmp_path, payload)

    report = evaluate_daemon_health(
        tmp_path,
        options=HealthOptions(require_process=False, require_startup=False),
        now=datetime.fromisoformat("2026-06-23T12:01:00+00:00"),
    )
    serialized = json.dumps(report)

    assert any(
        issue["code"] == "status_schema_invalid" and issue["field"] == "provider_lifecycle"
        for issue in report["errors"]
    )
    assert secret not in serialized


def test_daemon_status_marks_truncation_and_points_at_json(capsys, monkeypatch, tmp_path):
    import argparse

    from graphite import cli

    status = {
        "status": "ok",
        "project_count": 32,
        "failing_projects": 0,
        "pending_projects": 0,
        "updated_at": "2026-07-26T00:00:00Z",
        "projects": [
            {"root": f"/repo{i}", "build_count": 1, "failure_count": 0, "file_count": 10}
            for i in range(32)
        ],
    }
    monkeypatch.setattr(cli, "read_daemon_status", lambda *a, **k: status)
    args = argparse.Namespace(base_path=str(tmp_path), state_dir=None, json=False)
    cli.cmd_daemon_status(args)
    out = capsys.readouterr().out

    assert "  ... 12 more — use --json for the full list" in out
    assert out.count("builds=") == 20


def test_daemon_status_truncation_count_reconciles_with_the_header(capsys, monkeypatch, tmp_path):
    """20 shown + N dropped must equal the count the summary line claims."""
    import argparse
    import re

    from graphite import cli

    status = {
        "status": "ok",
        "project_count": 27,
        "failing_projects": 0,
        "pending_projects": 0,
        "updated_at": "2026-07-26T00:00:00Z",
        "projects": [
            {"root": f"/repo{i}", "build_count": 1, "failure_count": 0, "file_count": 10}
            for i in range(27)
        ],
    }
    monkeypatch.setattr(cli, "read_daemon_status", lambda *a, **k: status)
    cli.cmd_daemon_status(argparse.Namespace(base_path=str(tmp_path), state_dir=None, json=False))
    out = capsys.readouterr().out

    dropped = int(re.search(r"\.\.\. (\d+) more", out).group(1))
    assert out.count("builds=") + dropped == 27


def test_daemon_status_empty_project_list_prints_no_marker(capsys, monkeypatch, tmp_path):
    """Count-in-summary: the header already says 0; a dangling marker would be noise."""
    import argparse

    from graphite import cli

    status = {
        "status": "ok",
        "project_count": 0,
        "failing_projects": 0,
        "pending_projects": 0,
        "updated_at": "2026-07-26T00:00:00Z",
        "projects": [],
    }
    monkeypatch.setattr(cli, "read_daemon_status", lambda *a, **k: status)
    cli.cmd_daemon_status(argparse.Namespace(base_path=str(tmp_path), state_dir=None, json=False))
    out = capsys.readouterr().out

    assert "none found" not in out
    assert "more" not in out


def test_daemon_status_json_stays_uncapped(capsys, monkeypatch, tmp_path):
    import argparse
    import json as jsonlib

    from graphite import cli

    status = {
        "status": "ok",
        "project_count": 32,
        "failing_projects": 0,
        "pending_projects": 0,
        "updated_at": "2026-07-26T00:00:00Z",
        "projects": [
            {"root": f"/repo{i}", "build_count": 1, "failure_count": 0, "file_count": 10}
            for i in range(32)
        ],
    }
    monkeypatch.setattr(cli, "read_daemon_status", lambda *a, **k: status)
    cli.cmd_daemon_status(argparse.Namespace(base_path=str(tmp_path), state_dir=None, json=True))
    payload = jsonlib.loads(capsys.readouterr().out)

    assert len(payload["projects"]) == 32
