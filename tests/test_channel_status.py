"""Status is an append-only event log, and delivery is the broker's to assert.

Two properties under test. First, nothing is ever rewritten: a status that
mutated a file in place would make "who said what, when" only as reliable as the
last edit. Second, an agent cannot claim a message was delivered to it -- the
broker records that as it hands the message over, so "the message reached the
agent" and "the agent says it acted" stay separately verifiable.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from graphite import channel


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Channel plus two registered agent repos."""
    root = tmp_path / ".agent-channel"
    (root / "rounds").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(root))
    _git(root, "config", "user.email", "operator@example.com")
    _git(root, "config", "user.name", "Operator")
    (root / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")
    aramid = tmp_path / "aramid"
    graphite = tmp_path / "graphite"
    aramid.mkdir()
    graphite.mkdir()
    channel.write_registry(root, {str(aramid): "aramid-agent", str(graphite): "graphite-agent"})
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root, aramid, graphite


# --- append-only ------------------------------------------------------------


def test_each_status_is_a_new_file_and_none_are_ever_rewritten(tmp_path: Path) -> None:
    root, aramid, graphite = _setup(tmp_path)
    posted = channel.post_round(root, graphite, title="Run init", body="please", to=["aramid-agent"])
    n = posted["round"]

    channel.record_status(root, aramid, n, "acknowledged")
    channel.record_status(root, aramid, n, "blocked", reason="need the wording ratified")
    channel.record_status(root, aramid, n, "done")

    files = sorted((root / "status" / f"{n:03d}").glob("*.json"))
    assert len(files) == 3
    for path in files:
        rel = path.relative_to(root).as_posix()
        commits = _git(root, "log", "--oneline", "--", rel).strip().splitlines()
        assert len(commits) == 1, f"{rel} was rewritten: {commits}"


def test_current_status_is_the_last_event_and_history_survives(tmp_path: Path) -> None:
    root, aramid, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]

    channel.record_status(root, aramid, n, "acknowledged")
    channel.record_status(root, aramid, n, "blocked", reason="waiting on data")

    current = channel.current_status(root, n)
    assert current["status"] == "blocked"
    assert current["reason"] == "waiting on data"
    assert [e["status"] for e in channel.status_events(root, n)] == ["acknowledged", "blocked"]


# --- authorization ----------------------------------------------------------


def test_only_the_recipient_may_acknowledge(tmp_path: Path) -> None:
    root, aramid, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]

    with pytest.raises(channel.ChannelError) as exc:
        channel.record_status(root, graphite, n, "done")
    assert exc.value.code == "not_recipient"


def test_only_the_author_may_withdraw(tmp_path: Path) -> None:
    root, aramid, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]

    assert channel.record_status(root, graphite, n, "withdrawn")["status"] == "withdrawn"
    with pytest.raises(channel.ChannelError) as exc:
        channel.record_status(root, aramid, n, "withdrawn")
    assert exc.value.code == "not_author"


def test_an_agent_cannot_claim_delivery(tmp_path: Path) -> None:
    """The whole point of recording delivery separately: an agent that ignores
    its inbox must not be able to paper over it."""
    root, aramid, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]

    with pytest.raises(channel.ChannelError) as exc:
        channel.record_status(root, aramid, n, "delivered")
    assert exc.value.code == "broker_only_status"


def test_an_unknown_status_is_refused(tmp_path: Path) -> None:
    root, aramid, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]

    with pytest.raises(channel.ChannelError) as exc:
        channel.record_status(root, aramid, n, "mostly-done")
    assert exc.value.code == "unknown_status"


def test_transitions_are_not_policed_only_recorded(tmp_path: Path) -> None:
    """`done` with no prior `delivered` is an anomaly for the report to flag, not
    a refusal -- refusing it would destroy the evidence that it happened."""
    root, aramid, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]

    assert channel.record_status(root, aramid, n, "done")["status"] == "done"


# --- inbox and delivery -----------------------------------------------------


def test_inbox_returns_only_rounds_addressed_to_the_caller(tmp_path: Path) -> None:
    root, aramid, graphite = _setup(tmp_path)
    channel.post_round(root, graphite, title="For aramid", body="b", to=["aramid-agent"])
    channel.post_round(root, graphite, title="For codex", body="b", to=["codex-agent"])
    channel.post_round(root, graphite, title="For nobody", body="b")

    assert [r.title for r in channel.inbox(root, aramid)] == ["For aramid"]


def test_inbox_records_delivery_as_it_hands_the_message_over(tmp_path: Path) -> None:
    root, aramid, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]

    assert channel.current_status(root, n) is None
    channel.inbox(root, aramid)

    current = channel.current_status(root, n)
    assert current["status"] == "delivered"
    assert current["actor"] == "aramid-agent"
    assert current["broker"] is True


def test_a_delivered_round_leaves_the_inbox(tmp_path: Path) -> None:
    root, aramid, graphite = _setup(tmp_path)
    channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])

    assert len(channel.inbox(root, aramid)) == 1
    assert channel.inbox(root, aramid) == []


def test_status_events_are_committed_with_the_actors_trailer(tmp_path: Path) -> None:
    """The channel's commit-msg hook rejects unattributed commits, and the audit
    report compares the event's actor against the trailer to catch forgery."""
    root, aramid, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]

    channel.record_status(root, aramid, n, "acknowledged")

    assert _git(root, "status", "--porcelain").strip() == ""
    assert "Co-Authored-By: aramid-agent <aramid@agents.local>" in _git(root, "log", "-1", "--format=%B")


def test_status_on_a_missing_round_is_refused(tmp_path: Path) -> None:
    root, aramid, _ = _setup(tmp_path)

    with pytest.raises(channel.ChannelError) as exc:
        channel.record_status(root, aramid, 99, "done")
    assert exc.value.code == "round_not_found"
