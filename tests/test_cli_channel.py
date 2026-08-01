"""`graphite channel` -- resolve the shared agent channel's path.

Why this command exists (operator, 2026-08-01): the channel is the single
exception to repository isolation, so every agent needs to find it. But its
absolute path must NOT be written into `GRAPHITE.md` or any other managed
instruction file, because those are committed and pushed in consumer repos and
a local directory layout does not belong on a public remote. I wrote one in on
2026-08-01 and a guard caught it.

So the path lives in machine-local config and agents resolve it at runtime.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite.cli import main


@pytest.fixture()
def projects_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GRAPHITE_PROJECTS_ROOT", str(tmp_path))
    return tmp_path


def test_prints_the_channel_path(projects_root: Path, capsys) -> None:
    # A real channel is a git repo -- see the bare-directory test below for why
    # that distinction is load-bearing rather than cosmetic.
    ((projects_root / ".agent-channel") / ".git").mkdir(parents=True)

    code = main(["channel"])

    out = capsys.readouterr().out.strip()
    assert code == 0
    assert Path(out) == (projects_root / ".agent-channel").resolve()


def test_path_goes_to_stdout_even_when_the_channel_is_absent(projects_root: Path, capsys) -> None:
    """`$(graphite channel)` must still yield a usable path, so the diagnostic
    goes to stderr and the exit code carries the status. Printing an error onto
    stdout would have callers `cd` into a sentence."""
    code = main(["channel"])

    captured = capsys.readouterr()
    assert Path(captured.out.strip()) == (projects_root / ".agent-channel").resolve()
    assert code == 1, "absent channel must be detectable by exit code"
    assert captured.out.count("\n") == 1, "stdout is the path and nothing else"


def test_respects_the_projects_root_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """The path must never be welded to this machine's layout -- the same reason
    `default_projects_root` honours the env var ahead of the legacy F:/Projects
    fallback."""
    elsewhere = tmp_path / "somewhere-else"
    (elsewhere / ".agent-channel").mkdir(parents=True)
    monkeypatch.setenv("GRAPHITE_PROJECTS_ROOT", str(elsewhere))

    main(["channel"])

    assert Path(capsys.readouterr().out.strip()) == (elsewhere / ".agent-channel").resolve()


def test_json_reports_existence_and_git_status(projects_root: Path, capsys) -> None:
    channel = projects_root / ".agent-channel"
    (channel / ".git").mkdir(parents=True)
    (channel / "PROTOCOL.md").write_text("x", encoding="utf-8")

    code = main(["channel", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["exists"] is True
    assert payload["is_git_repo"] is True
    assert payload["has_protocol"] is True
    assert Path(payload["path"]) == channel.resolve()


def test_json_distinguishes_a_bare_directory_from_a_channel(projects_root: Path, capsys) -> None:
    """A directory that is not a git repo has no audit trail, which is the whole
    point of the channel. Report it rather than implying the setup is complete."""
    (projects_root / ".agent-channel").mkdir()

    code = main(["channel", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["exists"] is True
    assert payload["is_git_repo"] is False
    assert payload["has_protocol"] is False
    assert code == 1, "a channel without git cannot satisfy the audit requirement"


def test_channel_is_discoverable_through_capabilities(capsys) -> None:
    """Agents find commands via `capabilities`, so a channel command absent from
    it is a channel agents cannot locate -- which is the problem it exists to
    solve."""
    main(["capabilities", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert "channel" in payload["commands"]


def test_channel_does_not_register_the_cwd_as_an_open_repo(projects_root: Path) -> None:
    """It answers a question about machine layout, not about the repo you happen
    to be standing in. Registering activation here would make an unrelated repo
    look 'open' to the daemon."""
    from graphite.cli import _ACTIVATION_EXEMPT_COMMANDS

    assert "channel" in _ACTIVATION_EXEMPT_COMMANDS
