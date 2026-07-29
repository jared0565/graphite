"""Relative output_dir/cache_dir must anchor to the target repo, not the CWD.

Regression tests for #26. `Config.output_dir` defaults to `graph-out` and
`cache_dir` to `.cache/graphite`, both relative. `_project_scoped_config`
anchors them against the repo root and `init`/`doctor`/`bootstrap` use it, but
`build`/`check`/`scan`/`watch` called `_config_from_args` directly, so the
process CWD decided where a build's artifacts landed.

Every existing build test chdir'd INTO the repo before building, which is why
this went unnoticed. These deliberately do not.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite import buildlock


def _seed_repo(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    return root


def _away(tmp_path: Path) -> Path:
    d = tmp_path / "away"
    d.mkdir()
    return d


def test_build_writes_the_graph_into_the_target_repo_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graphite.cli import main

    repo = _seed_repo(tmp_path / "repo")
    away = _away(tmp_path)
    monkeypatch.chdir(away)
    monkeypatch.setenv("GRAPHITE_DAEMON_CHILD", "1")

    assert main(["build", str(repo)]) == 0

    assert (repo / "graph-out" / "graph.json").exists(), "target repo got no graph"
    assert not (away / "graph-out").exists(), "graph leaked into the CWD"
    assert not (away / ".cache").exists(), "cache leaked into the CWD"


def test_build_lock_is_repo_scoped_not_cwd_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guarantee Plan A exists to provide.

    The daemon locks `<root>/.cache/graphite` (absolute, via
    `daemon_config_for_project`). A manual build launched from any other
    directory must contend on that SAME file, or the two never mutex and the
    race the lock was built to close is still open.
    """
    from graphite.cli import main

    repo = _seed_repo(tmp_path / "repo")
    away = _away(tmp_path)
    monkeypatch.chdir(away)
    monkeypatch.setenv("GRAPHITE_DAEMON_CHILD", "1")

    with buildlock.build_lock(repo / ".cache" / "graphite") as held:
        assert held is True
        assert main(["build", str(repo)]) == 0
        out = capsys.readouterr().out

    assert "skipped" in out.lower(), f"build from a foreign cwd ignored the repo lock: {out!r}"
    assert not (repo / "graph-out" / "graph.json").exists()


def test_check_reads_the_target_repos_graph_from_a_foreign_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graphite.cli import main

    repo = _seed_repo(tmp_path / "repo")
    monkeypatch.setenv("GRAPHITE_DAEMON_CHILD", "1")
    monkeypatch.chdir(repo)
    assert main(["build", "."]) == 0

    away = _away(tmp_path)
    monkeypatch.chdir(away)
    # Freshly built and untouched -- must read as fresh, not compare the CWD's
    # (absent) graph against the repo's files.
    assert main(["check", str(repo)]) == 0


def test_explicit_absolute_output_dir_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graphite.cli import main

    repo = _seed_repo(tmp_path / "repo")
    away = _away(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.chdir(away)
    monkeypatch.setenv("GRAPHITE_DAEMON_CHILD", "1")

    assert main(["--output-dir", str(elsewhere), "build", str(repo)]) == 0
    assert (elsewhere / "graph.json").exists(), "an explicit absolute path must be honoured"
    assert not (repo / "graph-out" / "graph.json").exists()
