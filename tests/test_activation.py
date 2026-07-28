"""Activation registry: which repositories are open in a coding agent right now.

The daemon's supervised set is derived from these markers, so a bug here means
either supervising repos nobody opened (the thing this replaces) or supervising
nothing at all. Every test injects `now` rather than sleeping.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite import activation


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch the real user state dir from tests."""
    state = tmp_path / "state"
    monkeypatch.setenv(activation.ENV_STATE_DIR, str(state))
    monkeypatch.delenv(activation.ENV_DAEMON_CHILD, raising=False)
    return state


def test_mark_active_then_read_returns_the_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    activation.mark_active(repo, "claude", now=1000.0)
    records = activation.read_active(now=1000.0)

    assert [r.root for r in records] == [repo.resolve()]
    assert records[0].agent == "claude"
    assert records[0].first_seen == 1000.0
    assert records[0].last_seen == 1000.0


def test_refresh_preserves_first_seen_and_advances_last_seen(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    activation.mark_active(repo, "claude", now=1000.0)
    activation.mark_active(repo, "codex", now=1500.0)
    records = activation.read_active(now=1500.0)

    assert len(records) == 1
    assert records[0].first_seen == 1000.0
    assert records[0].last_seen == 1500.0
    assert records[0].agent == "codex"


def test_marker_past_ttl_is_excluded_and_deleted(tmp_path: Path) -> None:
    """Expired markers are removed from disk, not merely ignored, so the
    registry cannot grow without bound."""
    repo = tmp_path / "repo"
    repo.mkdir()
    activation.mark_active(repo, "claude", now=1000.0)
    path = activation.marker_path(repo)
    assert path.is_file()

    records = activation.read_active(ttl_seconds=100.0, now=1200.0)

    assert records == []
    assert not path.exists(), "expired marker was not garbage-collected"


def test_marker_exactly_at_ttl_boundary_is_still_live(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    activation.mark_active(repo, "claude", now=1000.0)

    records = activation.read_active(ttl_seconds=100.0, now=1100.0)

    assert [r.root for r in records] == [repo.resolve()]


def test_future_last_seen_is_clamped_so_skew_cannot_pin_a_repo(tmp_path: Path) -> None:
    """A clock-skewed writer must not be able to hold a repo active forever."""
    repo = tmp_path / "repo"
    repo.mkdir()
    activation.mark_active(repo, "claude", now=9_000_000.0)

    records = activation.read_active(ttl_seconds=100.0, now=1000.0)

    assert [r.root for r in records] == [repo.resolve()]
    assert records[0].last_seen == 1000.0, "future timestamp was not clamped to now"


def test_corrupt_marker_is_dropped_and_does_not_raise(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    activation.mark_active(repo, "claude", now=1000.0)
    activation.marker_path(repo).write_text("{not json", encoding="utf-8")

    records = activation.read_active(now=1000.0)

    assert records == []
    assert not activation.marker_path(repo).exists()


def test_marker_for_deleted_repo_is_dropped(tmp_path: Path) -> None:
    repo = tmp_path / "gone"
    repo.mkdir()
    activation.mark_active(repo, "claude", now=1000.0)
    repo.rmdir()

    records = activation.read_active(now=1000.0)

    assert records == []


def test_distinct_repos_get_distinct_markers(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()

    activation.mark_active(a, "claude", now=1000.0)
    activation.mark_active(b, "codex", now=1000.0)

    assert {r.root for r in activation.read_active(now=1000.0)} == {a.resolve(), b.resolve()}


def test_mark_active_is_a_noop_for_daemon_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon runs `-m graphite build` INSIDE the repo it supervises. If that
    refreshed the marker, every activated repo would renew its own activation
    forever and never expire -- supervision would ratchet up and never release."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv(activation.ENV_DAEMON_CHILD, "1")

    result = activation.mark_active(repo, "cli", now=1000.0)

    assert result is None
    assert activation.read_active(now=1000.0) == []


def test_mark_active_never_raises_when_state_dir_is_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open: activation must never break an agent session."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    monkeypatch.setenv(activation.ENV_STATE_DIR, str(blocker))
    repo = tmp_path / "repo"
    repo.mkdir()

    assert activation.mark_active(repo, "claude", now=1000.0) is None
    assert activation.read_active(now=1000.0) == []


def test_marker_file_is_valid_json_with_expected_keys(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    activation.mark_active(repo, "claude", now=1000.0)

    payload = json.loads(activation.marker_path(repo).read_text(encoding="utf-8"))

    assert payload["root"] == str(repo.resolve())
    assert payload["agent"] == "claude"
    assert payload["first_seen"] == 1000.0
    assert payload["last_seen"] == 1000.0
