"""The daemon must not race another builder on the same repo.

A lock skip must NOT look like a build failure. Returning a non-success
BuildResult would route straight into the failure path (daemon.py
_record_build_result), which increments failure_count, sets last_error AND
writes a ledger incident; daemon_health then counts any project with last_error
as failing. Once git hooks contend with the daemon routinely, health would sit
permanently degraded and the ledger would fill with non-incidents.
"""
from __future__ import annotations

import json
import time
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


def _options(tmp_path: Path) -> DaemonOptions:
    return DaemonOptions(once=True, debounce_seconds=0, max_builds_per_cycle=10, state_dir=tmp_path / "state")


def _events(tmp_path: Path) -> list[str]:
    log = tmp_path / "state" / "graphite-daemon.log"
    return [json.loads(line)["event"] for line in log.read_text(encoding="utf-8").splitlines()]


def test_the_daemon_never_holds_the_lock_itself(tmp_path: Path) -> None:
    """#60: the holder must be the writer.

    While the daemon held the lock on its child's behalf, force-stopping the
    daemon left the record on disk (TerminateProcess runs no `finally`) while
    the orphaned child kept writing graph-out/. The successor could neither
    trust "holder dead" -- the writer was still alive -- nor see "writer done",
    so it skipped the repo for the whole TTL. If the child holds the lock, its
    death and its writes end together, and a killed daemon leaves no lock.
    """
    project = _seed(tmp_path)
    lock = buildlock.lock_path(project / ".cache" / "graphite")
    lock_present_when_the_builder_ran: list[bool] = []

    def fake_build(root: Path, cfg: Config, timeout: float) -> BuildResult:
        lock_present_when_the_builder_ran.append(lock.exists())
        return BuildResult(success=True, returncode=0, duration_seconds=0.01, stdout="ok")

    run_daemon(tmp_path, Config(), _options(tmp_path), build_project=fake_build)

    assert lock_present_when_the_builder_ran == [False], "the daemon took the lock before spawning its builder"


def test_a_refused_child_is_a_lock_skip_not_a_failure(tmp_path: Path) -> None:
    """The child reports a refusal by exit status. The daemon must book it as
    "another builder owns the repo": no failure count, no last_error, no
    incident, and the initial build still owed."""
    project = _seed(tmp_path)

    def refused(root: Path, cfg: Config, timeout: float) -> BuildResult:
        return BuildResult(
            success=False,
            returncode=buildlock.REFUSED_EXIT_STATUS,
            duration_seconds=0.5,
            stdout="[graphite] build skipped: another build is already running for this repo",
        )

    status = run_daemon(tmp_path, Config(), _options(tmp_path), build_project=refused)

    entry = next(p for p in status["projects"] if Path(p["root"]) == project)
    assert entry["failure_count"] == 0, "a refusal counted as a build failure"
    assert entry["last_error"] is None, "a refusal set last_error, which health reads as failing"
    assert entry["build_count"] == 0, "a refusal counted as a build"
    assert entry["needs_initial_build"] is True, "a refused initial build must be retried next cycle"
    assert status["status"] == "ok", f"a refusal degraded daemon health: {status['status']}"
    events = _events(tmp_path)
    assert "build_skipped_locked" in events, events
    assert "build_failed" not in events, events


def test_a_stale_lock_does_not_stop_the_daemon(tmp_path: Path) -> None:
    """The daemon's own pre-read must age a lock out exactly as an acquirer
    would, or a dead builder's record would silence the daemon forever."""
    project = _seed(tmp_path)
    cache = project / ".cache" / "graphite"
    cache.mkdir(parents=True)
    dead = {"pid": 1, "started_at": time.time() - buildlock.DEFAULT_TTL_SECONDS - 1.0, "host": "gone"}
    buildlock.lock_path(cache).write_text(json.dumps(dead), encoding="utf-8")

    builds, _ = _run(tmp_path)

    assert builds == [project], "a lock older than the TTL kept the daemon from building"
