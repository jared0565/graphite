"""`graphite channel list --json` and `show` must say who set each status.

The human and scripted half of the 2026-09-25 attribution fix (round 256 read
`acknowledged` while one of its two recipients had recorded nothing).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from graphite import channel
from graphite.cli import main


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def shared_round(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """The round-256 shape: two recipients, one acts, the other records nothing."""
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
    n = channel.post_round(
        root, graphite, title="Shared", body="b", to=["aramid-agent", "demo-agent"]
    )["round"]
    channel.inbox(root, aramid)
    channel.record_status(root, aramid, n, "acknowledged")
    return n


def test_cli_list_json_carries_status_with_its_attribution(shared_round, capsys) -> None:
    assert main(["channel", "list", "--json"]) == 0
    row = next(r for r in json.loads(capsys.readouterr().out) if r["round"] == shared_round)

    assert row["status"] == "acknowledged"
    assert row["status_actor"] == "aramid-agent"
    assert row["unreceipted"] == ["demo-agent"]


def test_cli_show_prints_the_status_history_after_the_body(shared_round, capsys) -> None:
    """`graphite channel show N` is where a human asks "who set this"."""
    assert main(["channel", "show", str(shared_round)]) == 0
    out = capsys.readouterr().out

    assert "acknowledged" in out and "aramid-agent" in out
    assert any("demo-agent" in line and "no receipt" in line.lower() for line in out.splitlines())
