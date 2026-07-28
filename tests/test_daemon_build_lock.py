"""The daemon must not race another builder on the same repo.

A lock skip must NOT look like a build failure. Returning a non-success
BuildResult would route straight into the failure path (daemon.py
_record_build_result), which increments failure_count, sets last_error AND
writes a ledger incident; daemon_health then counts any project with last_error
as failing. Once git hooks contend with the daemon routinely, health would sit
permanently degraded and the ledger would fill with non-incidents.
"""
from __future__ import annotations

from pathlib import Path

from graphite import activation, buildlock
from graphite.config import Config
from graphite.daemon import BuildResult, DaemonOptions, run_daemon


def _seed(tmp_path: Path) -> Path:
    project = tmp_path / "alpha"
    (project / "src").mkdir(parents=True)
    (project / "src" / "index.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (project / "package.json").write_text("{}\n", encoding="utf-8")
    activation.mark_active(project)
    return project


def _run(tmp_path: Path) -> tuple[list[Path], dict]:
    builds: list[Path] = []

    def fake_build(root: Path, cfg: Config, timeout: float) -> BuildResult:
        builds.append(root)
        return BuildResult(success=True, returncode=0, duration_seconds=0.01, stdout="ok")

    status = run_daemon(
        tmp_path,
        Config(),
        DaemonOptions(
            once=True,
            debounce_seconds=0,
            max_builds_per_cycle=10,
            state_dir=tmp_path / "state",
        ),
        build_project=fake_build,
    )
    return builds, status


def test_daemon_skips_a_locked_project_without_building(tmp_path: Path) -> None:
    project = _seed(tmp_path)
    cache = project / ".cache" / "graphite"

    with buildlock.build_lock(cache) as held:
        assert held is True
        builds, _ = _run(tmp_path)

    assert builds == [], f"daemon built despite the lock: {builds}"


def test_a_lock_skip_is_not_recorded_as_a_failure(tmp_path: Path) -> None:
    """The point of this task. A skip must not look like a broken build."""
    project = _seed(tmp_path)
    cache = project / ".cache" / "graphite"

    with buildlock.build_lock(cache) as held:
        assert held is True
        _, status = _run(tmp_path)

    entry = next(p for p in status["projects"] if Path(p["root"]) == project)
    assert entry["failure_count"] == 0, "lock skip counted as a build failure"
    assert entry["last_error"] is None, "lock skip set last_error, which health reads as failing"
    assert entry["build_count"] == 0, "lock skip counted as a build"
    assert status["status"] == "ok", f"lock skip degraded daemon health: {status['status']}"


def test_daemon_builds_normally_when_the_lock_is_free(tmp_path: Path) -> None:
    project = _seed(tmp_path)

    builds, status = _run(tmp_path)

    assert builds == [project]
    assert not buildlock.lock_path(project / ".cache" / "graphite").exists(), "daemon leaked the lock"
    assert status["status"] == "ok"
