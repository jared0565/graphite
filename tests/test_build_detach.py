"""CLI build: lock acquisition and detached spawning."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite import buildlock


def _seed_repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    return tmp_path


def test_build_skips_when_another_builder_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from graphite.cli import main

    repo = _seed_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GRAPHITE_DAEMON_CHILD", "1")

    with buildlock.build_lock(repo / ".cache" / "graphite") as held:
        assert held is True
        assert main(["build", "."]) == 0
        out = capsys.readouterr().out

    assert "skipped" in out.lower(), f"a locked build must say so: {out!r}"
    assert not (repo / "graph-out" / "graph.json").exists(), "locked build produced a graph"


def test_build_proceeds_when_the_lock_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graphite.cli import main

    repo = _seed_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GRAPHITE_DAEMON_CHILD", "1")

    assert main(["build", "."]) == 0

    assert (repo / "graph-out" / "graph.json").exists()
    assert not buildlock.lock_path(repo / ".cache" / "graphite").exists()


def test_a_refused_build_reports_the_refusal_when_the_parent_asks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The daemon's child holds the lock itself (#60); the daemon never does.

    A human or a git hook still gets exit 0 for a skipped build -- it is a
    normal outcome, not a failure. The daemon needs to tell "another builder
    owns the repo" from "the build failed" without parsing output, so it asks
    its child for a distinct status.
    """
    from graphite.cli import main

    repo = _seed_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GRAPHITE_DAEMON_CHILD", "1")
    monkeypatch.setenv(buildlock.ENV_REPORT_REFUSAL, "1")

    with buildlock.build_lock(repo / ".cache" / "graphite") as held:
        assert held is True
        assert main(["build", "."]) == buildlock.REFUSED_EXIT_STATUS
        out = capsys.readouterr().out
        assert buildlock.lock_path(repo / ".cache" / "graphite").exists(), "a refused child unlinked a lock it did not own"

    assert "skipped" in out.lower(), f"a refused build must still say so: {out!r}"
    assert not (repo / "graph-out" / "graph.json").exists(), "refused build produced a graph"


def test_spawn_detached_uses_platform_isolation_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detaching is done in Python, not the shell, so it is testable.

    Git hooks on Windows run under Git Bash, where backgrounding is unreliable.
    """
    import subprocess
    import sys

    from graphite import detach

    seen: dict[str, object] = {}

    class FakePopen:
        pid = 4321

        def __init__(self, cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs

    monkeypatch.setattr(detach.subprocess, "Popen", FakePopen)

    pid = detach.spawn_detached(["python", "-m", "graphite", "build", "."], Path("."))

    assert pid == 4321
    kwargs = seen["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    if sys.platform == "win32":
        assert kwargs["creationflags"] & subprocess.DETACHED_PROCESS
    else:
        assert kwargs["start_new_session"] is True


def test_detach_flag_spawns_a_child_without_detach_and_returns_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child must NOT carry --detach, or it would re-spawn forever."""
    from graphite import cli
    from graphite.cli import main

    repo = _seed_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GRAPHITE_DAEMON_CHILD", "1")

    seen: dict[str, object] = {}

    def fake_spawn(cmd, cwd):
        seen["cmd"] = cmd
        return 999

    monkeypatch.setattr(cli, "spawn_detached", fake_spawn)

    assert main(["build", ".", "--detach"]) == 0

    cmd = seen["cmd"]
    assert "--detach" not in cmd, f"detached child would re-spawn forever: {cmd}"
    assert "build" in cmd
    assert not (repo / "graph-out" / "graph.json").exists(), "--detach built inline"
