"""Any agent that uses graphite at all activates its repo.

That is what covers agents graphite cannot hook (Codex, Gemini). The daemon's
own build children are excluded, or activation would renew itself forever.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite import activation
from graphite.config import Config
from graphite.daemon import _build_command


def test_daemon_build_children_run_with_the_suppression_flag(tmp_path: Path) -> None:
    """_build_command(cfg, root) -> (argv, env). Signature verified, not guessed."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _argv, env = _build_command(Config(), repo)

    assert env.get(activation.ENV_DAEMON_CHILD) == "1"


def test_suppression_flag_does_not_disturb_provider_credential_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_build_command` builds the child env by FILTERING provider variables out
    (daemon.py:337-341), guarded by
    tests/test_graph_provider_isolation.py::test_daemon_child_build_has_no_provider_argv_or_environment.

    Adding the activation flag must be one extra key on that filtered env. It
    must NOT become `dict(os.environ)` -- that would copy ANTHROPIC_API_KEY,
    OPENAI_API_KEY and friends into every daemon build child and silently undo
    the isolation contract.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-propagate")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-propagate")
    repo = tmp_path / "repo"
    repo.mkdir()

    _argv, env = _build_command(Config(), repo)

    assert env.get(activation.ENV_DAEMON_CHILD) == "1"
    assert "must-not-propagate" not in " ".join(env.values())


def test_a_daemon_child_cannot_renew_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ratchet test: activate, then let a child 'build', then cross the TTL
    with no agent activity. The repo must fall out of supervision.

    Without this, activation is permanent and the mandate is silently unmet --
    and because the ratchet only shows up over an hour of wall-clock, it would
    not surface in ordinary use.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    activation.mark_active(repo, "claude", now=1000.0)

    monkeypatch.setenv(activation.ENV_DAEMON_CHILD, "1")
    activation.mark_active(repo, "cli", now=1500.0)  # the daemon's build child
    monkeypatch.delenv(activation.ENV_DAEMON_CHILD)

    assert activation.read_active(ttl_seconds=100.0, now=2000.0) == []


def test_interactive_cli_invocation_marks_the_repo_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The universal backstop: an agent graphite cannot hook still registers the
    repo the moment it uses graphite at all."""
    from graphite.cli import main

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    main(["build", "."])

    assert [r.root for r in activation.read_active()] == [repo.resolve()]


def test_daemon_subcommand_does_not_mark_the_repo_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor is not an editing session."""
    from graphite.cli import main

    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.chdir(base)

    main(["daemon", str(base), "--once", "--state-dir", str(tmp_path / "state")])

    assert activation.read_active() == []
