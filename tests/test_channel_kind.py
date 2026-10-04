"""The channel carries only bug reports and recommended improvements.

Operator rule, 2026-10-04: a consumer has the TOOL, never the agent behind it, so
a round may only report a defect in a tool another agent owns or recommend an
improvement to it -- not ask a question, announce a release, or request a
verification. Anything that needs an agent to answer cannot exist in a real
installation.

The broker records that classification as the round's `kind`. It is optional in
this release, because the channel protocol is a stable surface
(`docs/compatibility.md`) and a new required field would break every existing
caller: a post without it still lands, with a deprecation warning that names the
replacement, and it becomes an error in the next major release.
"""
from __future__ import annotations

import json
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


def test_the_two_kinds_are_exactly_bug_report_and_improvement() -> None:
    """Exact-set on purpose: widening what the channel carries is a decision,
    not a side effect of adding an enum value."""
    assert channel.ROUND_KINDS == ("bug_report", "improvement")


@pytest.mark.parametrize("kind", ["bug_report", "improvement"])
def test_a_post_records_its_kind_and_carries_no_warning(wired, kind: str) -> None:
    root, aramid, _ = wired

    result = channel.post_round(root, aramid, title="t", body="b", to=["graphite-agent"], kind=kind)

    assert result["ok"] is True
    assert "warning" not in result
    entry = channel.read_round(root, result["round"])
    assert entry.kind == kind
    assert f"kind: {kind}\n" in entry.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", ["question", "announcement", "Bug_Report", ""])
def test_any_other_kind_is_refused_and_writes_nothing(wired, kind: str) -> None:
    """A question or an announcement is exactly what the rule excludes, so it is
    refused before a round number is spent or a file is written."""
    root, aramid, _ = wired

    with pytest.raises(channel.ChannelError) as exc:
        channel.post_round(root, aramid, title="t", body="b", kind=kind)

    assert exc.value.code == "invalid_kind"
    assert list((root / "rounds").glob("*.md")) == []
    assert channel.next_round_number(root) == 1


def test_a_post_without_kind_still_lands_but_is_warned_as_deprecated(wired) -> None:
    root, aramid, _ = wired

    result = channel.post_round(root, aramid, title="t", body="b")

    assert result["ok"] is True
    warning = result["warning"]
    assert warning["code"] == "kind_missing"
    for needle in ("bug_report", "improvement", "next major"):
        assert needle in warning["message"]
    assert channel.read_round(root, result["round"]).kind is None


def test_a_round_without_kind_parses_as_none(wired) -> None:
    """Every round posted before this release has no `kind`; reading one must
    not invent a classification for it."""
    root, aramid, _ = wired
    channel.post_round(root, aramid, title="old", body="b")

    assert [entry.kind for entry in channel.list_rounds(root)] == [None]


def test_the_report_shows_each_rounds_kind(wired) -> None:
    root, aramid, _ = wired
    channel.post_round(root, aramid, title="a bug", body="b", kind="bug_report")
    channel.post_round(root, aramid, title="unclassified", body="b")

    rows = {row["title"]: row for row in channel.build_report(root)["rounds"]}

    assert rows["a bug"]["kind"] == "bug_report"
    assert rows["unclassified"]["kind"] is None


def test_the_cli_list_json_shows_each_rounds_kind(wired, capsys) -> None:
    from graphite.cli import main

    root, aramid, _ = wired
    channel.post_round(root, aramid, title="an idea", body="b", kind="improvement")

    assert main(["channel", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["kind"] == "improvement"


def _line_for(output: str, title: str) -> str:
    return next(line for line in output.splitlines() if title in line)


def test_the_human_report_and_list_show_kind_beside_the_title(wired, capsys) -> None:
    """The human views, not just --json: an operator reading `channel report`
    must see which rounds are classified without switching formats. Only a
    classified round is tagged, so ~300 legacy rows do not each grow a marker."""
    from graphite.cli import main

    root, aramid, _ = wired
    channel.post_round(root, aramid, title="a bug", body="b", kind="bug_report")
    channel.post_round(root, aramid, title="unclassified", body="b")

    main(["channel", "report"])
    report = capsys.readouterr().out
    assert main(["channel", "list"]) == 0
    listing = capsys.readouterr().out

    for output in (report, listing):
        assert "[bug_report] a bug" in _line_for(output, "a bug")
        assert "[" not in _line_for(output, "unclassified")


def test_show_names_the_kind_before_the_body(wired, capsys) -> None:
    from graphite.cli import main

    root, aramid, _ = wired
    channel.post_round(root, aramid, title="an idea", body="the body", kind="improvement")

    assert main(["channel", "show", "1"]) == 0
    out = capsys.readouterr().out
    assert out.index("kind: improvement") < out.index("the body")


def test_channel_help_states_what_the_channel_is_for(capsys) -> None:
    """`docs/compatibility.md` names the protocol "as described by `graphite
    channel`", so its help is a place the rule must be stated, for an agent
    that has the CLI and not the MCP tools."""
    from graphite.cli import main

    with pytest.raises(SystemExit):
        main(["channel", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())

    assert "only bug reports and recommended improvements" in help_text
    assert "never" in help_text and "agent" in help_text


# --- the MCP surface --------------------------------------------------------


def _post_tool():
    pytest.importorskip("mcp")
    from graphite import mcp_server

    return next(t for t in mcp_server.channel_tool_definitions() if t.name == "graphite_channel_post")


def test_the_post_tool_advertises_kind_as_an_optional_enum() -> None:
    # By alias: mcp 1.x names the field `inputSchema`, 2.x `input_schema` with
    # `inputSchema` as its alias, so the alias is the one spelling both share.
    schema = _post_tool().model_dump(by_alias=True)["inputSchema"]

    assert schema["properties"]["kind"]["enum"] == ["bug_report", "improvement"]
    # Optional in this release: required would break every existing caller of a
    # stable surface. It becomes required in the next major.
    assert "kind" not in schema["required"]


def test_the_post_tool_states_the_rule_where_an_agent_will_read_it() -> None:
    """The description is the one text an agent is guaranteed to see at the
    moment it posts, whatever its repo's instruction files say."""
    description = _post_tool().description

    assert "only bug reports and recommended improvements" in description
    for excluded in ("questions", "announcements"):
        assert excluded in description
    assert "never its agent" in description


def test_dispatch_carries_kind_through_to_the_round(wired) -> None:
    """Pins the wiring, not just the schema: a `kind` the dispatcher dropped
    would leave every MCP post unclassified while the schema looked right."""
    pytest.importorskip("mcp")
    from graphite.mcp_server import GraphiteMCPServer, _dispatch

    root, aramid, _ = wired
    server = GraphiteMCPServer(project_root=aramid)

    contents = _dispatch(
        server,
        "graphite_channel_post",
        {"title": "t", "body": "b", "to": ["graphite-agent"], "kind": "improvement"},
    )

    result = json.loads(contents[0].text)
    assert channel.read_round(root, result["round"]).kind == "improvement"


def test_list_and_read_tools_show_kind(wired) -> None:
    pytest.importorskip("mcp")
    from graphite.mcp_server import GraphiteMCPServer

    _root, aramid, _ = wired
    server = GraphiteMCPServer(project_root=aramid)
    server.channel_post_tool(title="a bug", body="b", kind="bug_report")

    assert server.channel_list_tool()["rounds"][0]["kind"] == "bug_report"
    assert server.channel_read_tool(number=1)["kind"] == "bug_report"


def test_the_post_tool_returns_the_refusal_for_a_bad_kind(wired) -> None:
    pytest.importorskip("mcp")
    from graphite.mcp_server import GraphiteMCPServer

    _root, aramid, _ = wired

    result = GraphiteMCPServer(project_root=aramid).channel_post_tool(title="t", body="b", kind="question")

    assert result["error"] == "invalid_kind"
