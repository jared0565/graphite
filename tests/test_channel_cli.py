"""Human-facing channel surface. The report is the thing an operator runs.

`graphite channel` with no action must keep printing just the path -- round 42
documented `$(python -m graphite channel)` to consumers, so a subcommand that
changed the bare form would break every caller that followed the instructions.
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
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setenv("GRAPHITE_PROJECTS_ROOT", str(tmp_path))
    root = tmp_path / ".agent-channel"
    (root / "rounds").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(root))
    _git(root, "config", "user.email", "operator@example.com")
    _git(root, "config", "user.name", "Operator")
    (root / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")
    graphite = tmp_path / "graphite"
    graphite.mkdir()
    channel.write_registry(root, {str(graphite): "graphite-agent"})
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root, graphite


def test_bare_channel_still_prints_only_the_path(wired, capsys) -> None:
    root, _ = wired

    assert main(["channel"]) == 0
    assert capsys.readouterr().out.strip() == str(root.resolve())


def test_report_renders_for_a_human_and_exits_zero_when_clean(wired, capsys) -> None:
    root, graphite = wired
    channel.post_round(root, graphite, title="A round", body="body")

    assert main(["channel", "report"]) == 0
    out = capsys.readouterr().out
    assert "Agent channel audit" in out
    assert "A round" in out
    assert "VERIFIED" in out


def test_report_exits_nonzero_when_a_row_is_degraded(wired, capsys) -> None:
    """So it can be run as a check, not merely read."""
    root, _ = wired
    (root / "rounds" / "2026-08-01-graphite-round-5-loose.md").write_text("x\n", encoding="utf-8")

    assert main(["channel", "report"]) == 1
    assert "NOT OK" in capsys.readouterr().out


def test_report_json_is_machine_readable(wired, capsys) -> None:
    root, graphite = wired
    channel.post_round(root, graphite, title="A round", body="body")

    main(["channel", "report", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["rounds"][0]["verification"] == "verified"


def test_list_and_show(wired, capsys) -> None:
    root, graphite = wired
    channel.post_round(root, graphite, title="First", body="the body text")

    assert main(["channel", "list"]) == 0
    assert "First" in capsys.readouterr().out

    assert main(["channel", "show", "1"]) == 0
    assert "the body text" in capsys.readouterr().out


def test_show_without_a_number_is_a_clean_refusal(wired, capsys) -> None:
    assert main(["channel", "show"]) == 2
    assert "round number" in capsys.readouterr().err
