"""Opening a repo in Claude Code is what puts it under supervision.

`Stop` fires once per assistant turn, so it doubles as the heartbeat that keeps
a working session inside the activation TTL.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite import activation
from graphite.agent_hooks import handle_session_start, handle_stop


def _checkout(tmp_path: Path) -> Path:
    """A checkout's root: `.git` is what makes a folder registrable."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_session_start_marks_the_repo_active(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)

    handle_session_start({"cwd": str(repo)})

    records = activation.read_active()
    assert [r.root for r in records] == [repo.resolve()]
    assert records[0].agent == "claude"


def test_stop_refreshes_activation_as_a_heartbeat(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    activation.mark_active(repo, "claude", now=1000.0)

    handle_stop({"cwd": str(repo)})

    records = activation.read_active()
    assert [r.root for r in records] == [repo.resolve()]
    assert records[0].last_seen > 1000.0, "Stop did not refresh the marker"


def test_session_start_still_returns_context_when_activation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open contract: a broken registry must not cost the agent its
    session context. agent_hooks is fail-open by module contract.

    The folder is a checkout's root, so registration is attempted and the
    failure is real: in a folder that registers nothing, `mark_active` would
    never be reached and the test could not fail."""
    repo = _checkout(tmp_path)
    monkeypatch.setattr(
        "graphite.agent_hooks.mark_active",
        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )

    out = handle_session_start({"cwd": str(repo)})

    assert out is not None
    assert "graphite-first" in out["hookSpecificOutput"]["additionalContext"]
