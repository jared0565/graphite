"""The daemon supervises open repositories and leaves the rest alone.

The negative assertions are the point of this file: the mandate is not "build
the right repos", it is "do not touch the others". A repo nobody opened must
not even be scanned, because scanning every repo every cycle is the cost being
removed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite import activation
from graphite import daemon as daemon_mod
from graphite.config import Config
from graphite.daemon import BuildResult, DaemonOptions, run_daemon


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(activation.ENV_STATE_DIR, str(tmp_path / "state"))
    monkeypatch.delenv(activation.ENV_DAEMON_CHILD, raising=False)


def _repo(base: Path, name: str) -> Path:
    """A project root. Deliberately NOT given a bare `.git` directory: an empty
    `.git` is not a valid repo, and `snapshot()` fails it with "unable to
    enumerate Git repository safely" -- which silently empties the supervised
    set and makes the negative assertions in this file pass for the wrong
    reason. Activation supplies roots directly, so no discovery marker is needed.
    """
    root = base / name
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (root / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    return root


def _ok() -> BuildResult:
    return BuildResult(success=True, returncode=0, duration_seconds=0.01)


def _recording_builder(sink: list[Path]):
    def _build(root: Path, cfg: Config, timeout: float) -> BuildResult:
        sink.append(Path(root).resolve())
        return _ok()

    return _build


def test_only_the_active_repo_is_built(tmp_path: Path) -> None:
    base = tmp_path / "projects"
    base.mkdir()
    opened = _repo(base, "opened")
    closed = _repo(base, "closed")
    activation.mark_active(opened, "claude")

    built: list[Path] = []
    run_daemon(
        base,
        Config(),
        DaemonOptions(once=True, max_builds_per_cycle=5),
        build_project=_recording_builder(built),
    )

    assert built == [opened.resolve()]
    assert closed.resolve() not in built


def test_inactive_repo_is_never_snapshotted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not building is not enough -- an unopened repo must not be scanned either."""
    base = tmp_path / "projects"
    base.mkdir()
    opened = _repo(base, "opened")
    _repo(base, "closed")
    activation.mark_active(opened, "claude")

    seen: list[Path] = []
    real_snapshot = daemon_mod.snapshot

    def _spy(root, cfg):
        seen.append(Path(root).resolve())
        return real_snapshot(root, cfg)

    monkeypatch.setattr(daemon_mod, "snapshot", _spy)

    run_daemon(
        base,
        Config(),
        DaemonOptions(once=True, max_builds_per_cycle=5),
        build_project=lambda root, cfg, timeout: _ok(),
    )

    assert set(seen) <= {opened.resolve()}, f"scanned an unopened repo: {seen}"


def test_repo_outside_the_base_path_is_supervised(tmp_path: Path) -> None:
    """Supervision follows markers, not a base folder -- which is also why the
    nested-repo blindness (#6) becomes structurally impossible."""
    base = tmp_path / "projects"
    base.mkdir()
    outside = _repo(tmp_path / "elsewhere", "faraway")
    activation.mark_active(outside, "codex")

    built: list[Path] = []
    run_daemon(
        base,
        Config(),
        DaemonOptions(once=True, max_builds_per_cycle=5),
        build_project=_recording_builder(built),
    )

    assert built == [outside.resolve()]


def test_expired_marker_drops_the_repo_from_supervision(tmp_path: Path) -> None:
    base = tmp_path / "projects"
    base.mkdir()
    stale = _repo(base, "stale")
    activation.mark_active(stale, "claude", now=1.0)

    built: list[Path] = []
    run_daemon(
        base,
        Config(),
        DaemonOptions(once=True, activation_ttl_seconds=1.0),
        build_project=_recording_builder(built),
    )

    assert built == []


def test_status_reports_active_projects_and_not_nested_warnings(tmp_path: Path) -> None:
    base = tmp_path / "projects"
    base.mkdir()
    opened = _repo(base, "opened")
    activation.mark_active(opened, "claude")

    status = run_daemon(
        base,
        Config(),
        DaemonOptions(once=True),
        build_project=lambda root, cfg, timeout: _ok(),
    )

    assert str(opened.resolve()) in status["active_projects"]
    assert "unsupervised_nested_repos" not in status
