"""The MCP channel tools must say WHO set a status and where EACH recipient stands.

2026-09-25: `graphite_channel_read` said round 256 was `acknowledged` while one of
its two recipients (graphite-agent) had recorded nothing; the one word was the
other recipient's, with no actor attached. These are the agent-facing half of
the fix; `test_channel_attribution.py` holds the report and the core.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from graphite import channel

pytest.importorskip("mcp")

from graphite import mcp_server  # noqa: E402
from graphite.mcp_server import GraphiteMCPServer  # noqa: E402


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
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
    return root, graphite, aramid


def _shared_round(root: Path, graphite: Path, aramid: Path) -> int:
    """The round-256 shape: two recipients, one acts, the other records nothing."""
    n = channel.post_round(
        root, graphite, title="Shared", body="b", to=["aramid-agent", "demo-agent"]
    )["round"]
    channel.inbox(root, aramid)
    channel.record_status(root, aramid, n, "acknowledged")
    return n


def test_read_names_who_set_the_status_and_where_each_recipient_stands(wired) -> None:
    root, graphite, aramid = wired
    n = _shared_round(root, graphite, aramid)

    result = GraphiteMCPServer(project_root=graphite).channel_read_tool(number=n)

    # `status` keeps its meaning (the newest event, whoever wrote it) -- it is a
    # stable surface -- but it no longer travels without its actor.
    assert result["status"] == "acknowledged"
    assert result["status_actor"] == "aramid-agent"
    assert result["status_at"] == channel.current_status(root, n)["at"]
    assert result["recipients"]["aramid-agent"]["status"] == "acknowledged"
    assert result["recipients"]["demo-agent"] is None
    assert result["unreceipted"] == ["demo-agent"]


def test_read_carries_the_history_with_each_event_checked_against_its_commit(wired) -> None:
    root, graphite, aramid = wired
    n = _shared_round(root, graphite, aramid)

    history = GraphiteMCPServer(project_root=graphite).channel_read_tool(number=n)["history"]

    assert [(e["seq"], e["status"], e["actor"], e["broker"]) for e in history] == [
        (1, "delivered", "aramid-agent", True),
        (2, "acknowledged", "aramid-agent", False),
    ]
    assert all(e["verification"] == "verified" and e["commit"] for e in history)


def test_read_history_shows_a_forged_event_as_a_discrepancy(wired) -> None:
    """A status file claiming aramid-agent, committed under graphite-agent's
    trailer: someone else speaking for aramid."""
    root, graphite, _aramid = wired
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]
    directory = root / "status" / f"{n:03d}"
    directory.mkdir(parents=True)
    event = {"round": n, "seq": 1, "status": "acknowledged", "actor": "aramid-agent",
             "broker": False, "at": "2026-09-25T00:00:00Z", "reason": None}
    (directory / "0001-acknowledged.json").write_text(json.dumps(event), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"forged\n\n{channel.trailer('graphite-agent')}\n")

    history = GraphiteMCPServer(project_root=graphite).channel_read_tool(number=n)["history"]

    assert [e["verification"] for e in history] == ["discrepancy"]


def test_list_carries_the_actor_and_per_recipient_status_for_every_round(wired) -> None:
    root, graphite, aramid = wired
    n = _shared_round(root, graphite, aramid)

    rounds = GraphiteMCPServer(project_root=graphite).channel_list_tool()["rounds"]
    row = next(r for r in rounds if r["round"] == n)

    assert row["status"] == "acknowledged"
    assert row["status_actor"] == "aramid-agent"
    assert row["recipients"]["demo-agent"] is None
    assert row["unreceipted"] == ["demo-agent"]


def test_the_read_tool_tells_agents_that_reading_records_no_receipt() -> None:
    """graphite's own sessions read rounds for weeks without ever calling inbox,
    leaving 16 rounds with no graphite event. The tool an agent reaches for must
    say so."""
    read = next(
        t for t in mcp_server.channel_tool_definitions() if t.name == "graphite_channel_read"
    )
    assert "inbox" in read.description
    assert "records nothing" in read.description
