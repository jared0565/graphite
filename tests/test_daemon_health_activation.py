"""Health reports what is supervised now.

The old nested-repo warning answered a question activation deletes: an unopened
repo SHOULD be unsupervised, so warning about it would fire constantly and mean
nothing.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from graphite.daemon_health import HealthOptions, evaluate_daemon_health

NOW = datetime(2026, 7, 28, 9, 0, 0, tzinfo=timezone.utc)


def _status(tmp_path: Path, payload: dict) -> Path:
    state = tmp_path / ".graphite-daemon"
    state.mkdir(parents=True, exist_ok=True)
    (state / "status.json").write_text(json.dumps(payload), encoding="utf-8")
    return state


def _evaluate(tmp_path: Path, payload: dict) -> dict:
    # project_count is required by the status schema; omitting it raises a
    # status_schema_invalid error that has nothing to do with what these tests
    # are about.
    state = _status(tmp_path, {"project_count": 0, **payload})
    return evaluate_daemon_health(
        tmp_path,
        state_dir=state,
        options=HealthOptions(require_process=False, require_startup=False),
        now=NOW,
    )


def test_active_projects_are_reported(tmp_path: Path) -> None:
    """_normalize_status is a strict whitelist: a new status field is silently
    dropped unless normalized explicitly."""
    report = _evaluate(tmp_path, {
        "updated_at": "2026-07-28T09:00:00+00:00",
        "status": "ok",
        "projects": [],
        "active_projects": ["F:\\Projects\\aramid", "F:\\Projects\\graphite"],
    })

    assert report["active_projects"] == ["F:\\Projects\\aramid", "F:\\Projects\\graphite"]


def test_missing_active_projects_defaults_to_empty(tmp_path: Path) -> None:
    report = _evaluate(tmp_path, {
        "updated_at": "2026-07-28T09:00:00+00:00",
        "status": "ok",
        "projects": [],
    })

    assert report["active_projects"] == []


def test_nested_repo_warning_is_gone(tmp_path: Path) -> None:
    """Supervision follows markers, so nesting is irrelevant -- the warning is
    removed rather than ported."""
    report = _evaluate(tmp_path, {
        "updated_at": "2026-07-28T09:00:00+00:00",
        "status": "ok",
        "projects": [],
        "active_projects": [],
        "unsupervised_nested_repos": ["F:\\Projects\\demo\\worker"],
    })

    codes = {w["code"] for w in report.get("warnings", [])}
    assert "project_nested_repo_unsupervised" not in codes
    assert report["status"] == "ok"
