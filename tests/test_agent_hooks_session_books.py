"""A session's savings books start where the usage ledger ended when the
session began (#108).

The ledger is per repository and every session appends to it. A session the
stop cursor had not seen used to start reading at byte 0, so its first Stop
reported every earlier session's entries as saved "this turn", and its session
total carried them from then on.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.agent_hooks import MAX_CURSOR_SESSIONS, handle_session_start, handle_stop
from graphite.savings import estimate_entry
from graphite.usage_ledger import iter_entries, read_cursor, record_usage


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "alpha.py").write_text("def alpha():\n    return 1\n" * 40, encoding="utf-8")
    return root


def _event(root: Path, event: str, session: str) -> dict:
    return {"session_id": session, "cwd": str(root), "hook_event_name": event}


def _start(root: Path, session: str) -> None:
    handle_session_start(_event(root, "SessionStart", session))


def _stop(root: Path, session: str) -> dict | None:
    return handle_stop(_event(root, "Stop", session))


def _use(root: Path) -> None:
    record_usage(root, cmd="context", wall_ms=50, result={"files": [{"path": "alpha.py"}]})


def _one_entry_tokens(root: Path) -> int:
    """Every `_use` writes the same entry, so one entry's estimate is the unit."""
    estimates = {estimate_entry(entry)["tokens_saved"] for entry in iter_entries(root)}
    assert len(estimates) == 1 and estimates != {0}
    return estimates.pop()


def _session_tokens(root: Path, session: str) -> int:
    return read_cursor(root)["sessions"][session]["tokens"]


def test_a_sessions_first_stop_counts_only_what_followed_its_start(repo: Path) -> None:
    for _ in range(3):
        _use(repo)  # earlier sessions' history
    _start(repo, "A")
    _use(repo)

    message = _stop(repo, "A")

    assert message is not None
    assert _session_tokens(repo, "A") == _one_entry_tokens(repo)


def test_a_session_that_ran_nothing_reports_nothing(repo: Path) -> None:
    for _ in range(3):
        _use(repo)
    _start(repo, "B")

    assert _stop(repo, "B") is None


def test_a_stop_for_a_session_nothing_recorded_counts_no_history(repo: Path) -> None:
    """No SessionStart reached the cursor (the session was pruned from it, or
    its start was not seen). The ledger already holds other sessions' entries,
    and none of them is this session's."""
    for _ in range(3):
        _use(repo)

    assert _stop(repo, "C") is None

    _use(repo)
    assert _stop(repo, "C") is not None
    assert _session_tokens(repo, "C") == _one_entry_tokens(repo)


def test_a_session_started_again_keeps_its_books(repo: Path) -> None:
    """SessionStart fires again for the same session on a resume or a compact.
    The session's books must not start over then."""
    _start(repo, "A")
    _use(repo)
    assert _stop(repo, "A") is not None

    _start(repo, "A")
    _use(repo)
    assert _stop(repo, "A") is not None

    assert _session_tokens(repo, "A") == 2 * _one_entry_tokens(repo)


def test_session_start_without_a_session_id_writes_no_books(repo: Path) -> None:
    handle_session_start({"cwd": str(repo), "hook_event_name": "SessionStart"})

    assert read_cursor(repo).get("sessions", {}) == {}


def test_session_start_keeps_the_cursor_to_its_cap(repo: Path) -> None:
    for i in range(MAX_CURSOR_SESSIONS + 3):
        _start(repo, f"s{i}")

    sessions = read_cursor(repo)["sessions"]
    assert len(sessions) == MAX_CURSOR_SESSIONS
    assert "s0" not in sessions and f"s{MAX_CURSOR_SESSIONS + 2}" in sessions
