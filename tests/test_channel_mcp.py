"""The agent-facing surface: channel tools on graphite's MCP server.

This is the first place graphite writes outside the selected project, so the
tests here are mostly about what the tools REFUSE. Identity comes from the
server's project_root; nothing a caller passes can change it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from graphite import channel

pytest.importorskip("mcp")

from graphite.mcp_server import GraphiteMCPServer  # noqa: E402


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GRAPHITE_PROJECTS_ROOT", str(tmp_path))
    root = tmp_path / ".agent-channel"
    (root / "rounds").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(root))
    _git(root, "config", "user.email", "operator@example.com")
    _git(root, "config", "user.name", "Operator")
    (root / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")
    aramid, graphite = tmp_path / "aramid", tmp_path / "graphite"
    aramid.mkdir()
    graphite.mkdir()
    channel.write_registry(root, {str(aramid): "aramid-agent", str(graphite): "graphite-agent"})
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root, aramid, graphite


def test_post_attributes_to_the_repo_the_server_runs_in(wired) -> None:
    root, aramid, _ = wired
    server = GraphiteMCPServer(project_root=aramid)

    result = server.channel_post_tool(title="From aramid", body="b", to=["graphite-agent"])

    assert result["ok"] is True
    assert result["author"] == "aramid-agent"


def test_an_unregistered_repo_cannot_post(wired, tmp_path: Path) -> None:
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    server = GraphiteMCPServer(project_root=stranger)

    result = server.channel_post_tool(title="Sneaky", body="b")

    assert result["error"] == "unregistered_project"


def test_the_post_tool_exposes_no_author_field(wired) -> None:
    """Schema-level guard: an MCP client must not be able to name an author even
    if the core function is later loosened."""
    import inspect

    _root, aramid, _ = wired
    params = set(inspect.signature(GraphiteMCPServer.channel_post_tool).parameters)
    assert "author" not in params and "agent" not in params
    assert GraphiteMCPServer(project_root=aramid) is not None


def test_inbox_hands_over_and_records_delivery(wired) -> None:
    root, aramid, graphite = wired
    GraphiteMCPServer(project_root=graphite).channel_post_tool(
        title="For aramid", body="b", to=["aramid-agent"]
    )

    inbox = GraphiteMCPServer(project_root=aramid).channel_inbox_tool()

    assert [item["title"] for item in inbox["rounds"]] == ["For aramid"]
    assert channel.current_status(root, inbox["rounds"][0]["round"])["status"] == "delivered"


def test_status_tool_refuses_a_non_recipient(wired) -> None:
    _root, aramid, graphite = wired
    posted = GraphiteMCPServer(project_root=graphite).channel_post_tool(
        title="T", body="b", to=["aramid-agent"]
    )

    server = GraphiteMCPServer(project_root=graphite)
    assert server.channel_status_tool(number=posted["round"], status="done")["error"] == "not_recipient"

    recipient = GraphiteMCPServer(project_root=aramid)
    assert recipient.channel_status_tool(number=posted["round"], status="done")["status"] == "done"


def test_status_tool_refuses_a_broker_only_status(wired) -> None:
    _root, aramid, graphite = wired
    posted = GraphiteMCPServer(project_root=graphite).channel_post_tool(
        title="T", body="b", to=["aramid-agent"]
    )

    server = GraphiteMCPServer(project_root=aramid)
    result = server.channel_status_tool(number=posted["round"], status="delivered")

    assert result["error"] == "broker_only_status"


def test_list_and_read(wired) -> None:
    _root, _aramid, graphite = wired
    server = GraphiteMCPServer(project_root=graphite)
    server.channel_post_tool(title="One", body="the body")

    assert server.channel_list_tool()["rounds"][0]["title"] == "One"
    assert "the body" in server.channel_read_tool(number=1)["body"]


def test_a_missing_channel_is_an_error_not_a_crash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GRAPHITE_PROJECTS_ROOT", str(tmp_path / "nowhere"))
    repo = tmp_path / "repo"
    repo.mkdir()

    assert GraphiteMCPServer(project_root=repo).channel_inbox_tool()["error"] == "channel_missing"


def test_the_channel_tools_are_advertised(wired) -> None:
    """A tool an agent cannot discover is the problem this was built to solve."""
    from graphite import mcp_server

    names = {tool.name for tool in mcp_server.channel_tool_definitions()}
    assert names == {
        "graphite_channel_inbox",
        "graphite_channel_read",
        "graphite_channel_post",
        "graphite_channel_status",
        "graphite_channel_list",
    }
