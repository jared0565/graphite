"""Every status a reader sees must say WHO recorded it and where EACH recipient stands.

2026-09-25: round 256 was addressed to graphite-agent and demo-store2-agent, and
`channel_read` said `acknowledged` while graphite-agent had recorded nothing at
all -- the one word was demo-store2-agent's. The event log itself was intact:
every event names its actor and is committed under that agent's trailer. The
surfaces folded it into one word, across every recipient, with no actor. In 39
of the 212 live rounds carrying events, that word misstated at least one
recipient.

Pinned here: the audit report grading each status event against its own commit
the way it already grades rounds, per-recipient rows and stalling, and reads
that never contend for the channel's index lock. The MCP and CLI surfaces are
pinned in `test_mcp_server_channel_attribution.py` and
`test_cli_channel_attribution.py`, where the mutation launcher's targeted
stage finds them.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
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


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Path]:
    """A channel with an author (graphite) and two recipients (aramid, demo)."""
    monkeypatch.setenv("GRAPHITE_PROJECTS_ROOT", str(tmp_path))
    root = tmp_path / ".agent-channel"
    (root / "rounds").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(root))
    _git(root, "config", "user.email", "operator@example.com")
    _git(root, "config", "user.name", "Operator")
    (root / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")
    graphite, aramid, demo = tmp_path / "graphite", tmp_path / "aramid", tmp_path / "demo"
    for repo in (graphite, aramid, demo):
        repo.mkdir()
    channel.write_registry(
        root,
        {str(graphite): "graphite-agent", str(aramid): "aramid-agent", str(demo): "demo-agent"},
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root, graphite, aramid, demo


def _shared_round(wired) -> int:
    """The round-256 shape: two recipients, one acts, the other records nothing."""
    root, graphite, aramid, _demo = wired
    n = channel.post_round(
        root, graphite, title="Shared", body="b", to=["aramid-agent", "demo-agent"]
    )["round"]
    channel.inbox(root, aramid)
    channel.record_status(root, aramid, n, "acknowledged")
    return n


def _server(project_root: Path):
    pytest.importorskip("mcp")
    from graphite.mcp_server import GraphiteMCPServer

    return GraphiteMCPServer(project_root=project_root)


def _commit_as(root: Path, agent: str, subject: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"{subject}\n\n{channel.trailer(agent)}\n")


def _row(report: dict, number: int) -> dict:
    return next(r for r in report["rounds"] if r["round"] == number)


def _forge_ack(root: Path, number: int, *, claimed: str, committed_as: str) -> None:
    """A status file claiming one agent, committed under another's trailer."""
    directory = root / "status" / f"{number:03d}"
    directory.mkdir(parents=True, exist_ok=True)
    seq = len(list(directory.glob("*.json"))) + 1
    event = {
        "round": number,
        "seq": seq,
        "status": "acknowledged",
        "actor": claimed,
        "broker": False,
        "at": "2026-09-25T00:00:00Z",
        "reason": None,
    }
    (directory / f"{seq:04d}-acknowledged.json").write_text(json.dumps(event), encoding="utf-8")
    _commit_as(root, committed_as, f"round {number}: acknowledged")


# --- the audit report grades status events like rounds ------------------------


def test_report_flags_a_status_event_whose_actor_disagrees_with_its_commit(wired) -> None:
    """The forgery signature for a status: the file names one agent, the commit
    trailer another. Recipient membership alone cannot see it -- the forged actor
    IS a recipient."""
    root, graphite, _aramid, _demo = wired
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]
    _forge_ack(root, n, claimed="aramid-agent", committed_as="graphite-agent")

    report = channel.build_report(root)

    assert any(a["round"] == n and a["kind"] == "status_discrepancy" for a in report["anomalies"])
    assert report["ok"] is False


def test_report_flags_a_status_event_rewritten_after_it_was_committed(wired) -> None:
    root, graphite, aramid, _demo = wired
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]
    channel.inbox(root, aramid)
    path = next((root / "status" / f"{n:03d}").glob("*.json"))
    path.write_text(path.read_text(encoding="utf-8").replace("delivered", "done"), encoding="utf-8")
    _commit_as(root, "aramid-agent", "rewrite")

    report = channel.build_report(root)

    assert any(a["round"] == n and a["kind"] == "status_modified" for a in report["anomalies"])
    assert report["ok"] is False


def test_report_flags_an_uncommitted_edit_to_a_status_event(wired) -> None:
    root, graphite, aramid, _demo = wired
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]
    channel.inbox(root, aramid)
    path = next((root / "status" / f"{n:03d}").glob("*.json"))
    path.write_text(path.read_text(encoding="utf-8").replace("delivered", "done"), encoding="utf-8")

    report = channel.build_report(root)

    assert any(a["round"] == n and a["kind"] == "status_modified" for a in report["anomalies"])


def test_report_names_silent_recipients_without_failing_the_check(wired) -> None:
    """Recipients who never record anything are listed per agent -- visible -- but
    are not an integrity failure: several registered agents have never called
    inbox at all, and a check that is always red is one nobody reads."""
    root, *_ = wired
    n = _shared_round(wired)

    report = channel.build_report(root)
    row = _row(report, n)

    assert row["status_actor"] == "aramid-agent"
    assert row["recipients"]["aramid-agent"]["status"] == "acknowledged"
    assert row["unreceipted"] == ["demo-agent"]
    assert report["unreceipted"] == {"demo-agent": [n]}
    assert report["ok"] is True


def test_a_recipient_left_on_delivered_is_stalled_even_when_another_finished(wired) -> None:
    """Stalling used to key off the one folded word, so a co-recipient's `done`
    hid a recipient that never followed up."""
    root, graphite, aramid, demo = wired
    n = channel.post_round(
        root, graphite, title="T", body="b", to=["aramid-agent", "demo-agent"]
    )["round"]
    channel.inbox(root, aramid)
    channel.inbox(root, demo)
    channel.record_status(root, demo, n, "done")

    later = datetime.now(timezone.utc) + timedelta(days=4)
    row = _row(channel.build_report(root, stale_days=3, now=later), n)

    assert row["status"] == "done"
    assert row["stalled"] is True
    assert row["stalled_recipients"] == ["aramid-agent"]


def test_a_withdrawn_round_is_not_stalled_by_a_recipient_left_on_delivered(wired) -> None:
    root, graphite, aramid, _demo = wired
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]
    channel.inbox(root, aramid)
    channel.record_status(root, graphite, n, "withdrawn")

    later = datetime.now(timezone.utc) + timedelta(days=4)
    row = _row(channel.build_report(root, stale_days=3, now=later), n)

    assert row["stalled"] is False
    assert row["stalled_recipients"] == []


def test_the_human_report_shows_each_recipient_where_the_folded_status_misleads(wired) -> None:
    root, *_ = wired
    n = _shared_round(wired)

    text = channel.render_report(channel.build_report(root))

    assert f"round {n}" in text
    assert any("demo-agent" in line and "NO RECEIPT" in line for line in text.splitlines())


def test_reading_the_channel_never_rewrites_its_git_index(wired) -> None:
    """Reads now run git in the channel, alongside other agents' brokers writing
    to it. A plain `git status` refreshes and REWRITES the index when stat data is
    stale, taking `index.lock` -- and a broker's `git add` that meets that lock
    fails. A read must not contend for the one lock writers need."""
    root, *_ = wired
    n = _shared_round(wired)
    index = root / ".git" / "index"
    tracked = next((root / "status" / f"{n:03d}").glob("*.json"))
    future = tracked.stat().st_mtime + 120
    os.utime(tracked, (future, future))  # stale stat data, identical content
    before = index.read_bytes()

    channel.build_report(root)
    _server(root.parent / "graphite").channel_read_tool(number=n)

    assert index.read_bytes() == before
