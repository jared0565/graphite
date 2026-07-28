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


def test_env_escape_hatch_bypasses_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child whose parent already holds the lock must not deadlock on it."""
    from graphite.cli import main

    repo = _seed_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GRAPHITE_DAEMON_CHILD", "1")
    monkeypatch.setenv(buildlock.ENV_LOCK_HELD, "1")

    with buildlock.build_lock(repo / ".cache" / "graphite") as held:
        assert held is True
        assert main(["build", "."]) == 0

    assert (repo / "graph-out" / "graph.json").exists(), "escape hatch did not build"
