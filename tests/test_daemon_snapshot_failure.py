"""A project the daemon cannot snapshot must be visible, not invisible (#113).

The daemon logs `project_snapshot_failed` and moves on, so the project never
enters the supervised set -- and a health report built from that set reads
"ok" while no graph is being maintained at all. On 2026-10-27 the daemon
failed every snapshot of an activated repository -- ten failures across nine
discovery cycles, 01:50:22Z to 02:03:59Z -- while `daemon-health` said
"ok, 0 projects, 0 failing".

These tests pin both halves of the fix: the daemon records the failure, and
the report counts it, names it, and says why.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from graphite import activation
from graphite import daemon as daemon_mod
from graphite import ingest as ingest_module
from graphite.cli import main
from graphite.config import Config
from graphite.daemon import BuildResult, DaemonOptions, describe_exception_chain, run_daemon
from graphite.daemon_health import HealthOptions, evaluate_daemon_health, format_health_text
from graphite.git import GitLaunchError
from graphite.ingest import IngestError

SNAPSHOT_ERROR = "unable to enumerate Git repository safely"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(activation.ENV_STATE_DIR, str(tmp_path / "state"))
    monkeypatch.delenv(activation.ENV_DAEMON_CHILD, raising=False)


def _broken_repo(base: Path, name: str) -> Path:
    """A project root whose snapshot cannot succeed.

    An empty `.git` directory is not a valid repository, so enumerating it
    fails -- the same failure the daemon hit on 2026-10-27.
    """
    root = base / name
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    return root


def _ok() -> BuildResult:
    return BuildResult(success=True, returncode=0, duration_seconds=0.01)


def _recording_builder(sink: list[Path]):
    def _build(root: Path, cfg: Config, timeout: float) -> BuildResult:
        sink.append(Path(root).resolve())
        return _ok()

    return _build


def test_a_project_whose_snapshot_fails_is_reported_as_failing(tmp_path: Path) -> None:
    base = tmp_path / "projects"
    base.mkdir()
    broken = _broken_repo(base, "broken")
    activation.mark_active(broken, "claude")

    status = run_daemon(
        base,
        Config(),
        DaemonOptions(once=True),
        build_project=lambda root, cfg, timeout: _ok(),
    )

    assert status["status"] == "degraded"
    assert status["project_count"] == 1
    assert status["failing_projects"] == 1
    assert str(broken.resolve()) in status["active_projects"]
    failures = status["snapshot_failures"]
    assert len(failures) == 1
    assert failures[0]["root"] == str(broken.resolve())
    assert SNAPSHOT_ERROR in failures[0]["error"]
    assert failures[0]["failed_at"]


def test_a_project_whose_snapshot_fails_is_never_built(tmp_path: Path) -> None:
    """No snapshot means no change set -- building on an empty one would
    publish an empty graph over a working one."""
    base = tmp_path / "projects"
    base.mkdir()
    broken = _broken_repo(base, "broken")
    activation.mark_active(broken, "claude")

    built: list[Path] = []
    run_daemon(
        base,
        Config(),
        DaemonOptions(once=True),
        build_project=_recording_builder(built),
    )

    assert built == []


def test_a_later_successful_snapshot_clears_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "projects"
    base.mkdir()
    broken = _broken_repo(base, "broken")
    activation.mark_active(broken, "claude")

    real_snapshot = daemon_mod.snapshot
    calls: list[Path] = []

    def _snapshot(root, cfg):
        calls.append(Path(root))
        if len(calls) == 1:
            # The empty .git is what breaks enumeration. Repair it so the
            # next discovery cycle can snapshot the project for real.
            shutil.rmtree(broken / ".git")
            raise IngestError(SNAPSHOT_ERROR)
        return real_snapshot(root, cfg)

    monkeypatch.setattr(daemon_mod, "snapshot", _snapshot)

    status = run_daemon(
        base,
        Config(),
        DaemonOptions(
            max_cycles=2,
            scan_interval_seconds=0.05,
            discover_interval_seconds=0.01,
        ),
        build_project=lambda root, cfg, timeout: _ok(),
    )

    assert status["snapshot_failures"] == []
    assert status["failing_projects"] == 0
    assert status["status"] == "ok"
    assert status["project_count"] == 1


def test_daemon_health_counts_a_snapshot_failure_as_failing(tmp_path: Path) -> None:
    base = tmp_path / "projects"
    base.mkdir()
    broken = _broken_repo(base, "broken")
    activation.mark_active(broken, "claude")

    run_daemon(
        base,
        Config(),
        DaemonOptions(once=True),
        build_project=lambda root, cfg, timeout: _ok(),
    )

    report = evaluate_daemon_health(
        base,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    assert report["ok"] is False
    assert report["summary"]["project_count"] == 1
    assert report["summary"]["failing_count"] == 1
    assert "project_snapshot_failed" in {issue["code"] for issue in report["errors"]}
    failing = report["projects"]["failing"]
    assert [project["root"] for project in failing] == [str(broken.resolve())]
    assert SNAPSHOT_ERROR in failing[0]["last_error"]
    # The same fields a build failure carries, so a consumer reading
    # `build_count` off a failing project does not find the key missing.
    assert set(failing[0]) >= {
        "root",
        "file_count",
        "build_count",
        "failure_count",
        "last_error",
        "last_success_at",
        "last_success_age_seconds",
        "needs_initial_build",
    }
    assert failing[0]["failure_source"] == "snapshot"


def test_daemon_health_text_names_the_project_and_the_reason(tmp_path: Path) -> None:
    base = tmp_path / "projects"
    base.mkdir()
    broken = _broken_repo(base, "broken")
    activation.mark_active(broken, "claude")

    run_daemon(
        base,
        Config(),
        DaemonOptions(once=True),
        build_project=lambda root, cfg, timeout: _ok(),
    )

    report = evaluate_daemon_health(
        base,
        options=HealthOptions(require_process=False, require_startup=False),
    )
    text = format_health_text(report)

    assert "1 failing" in text
    assert "project_snapshot_failed" in text
    assert str(broken.resolve()) in text
    assert SNAPSHOT_ERROR in text


def test_daemon_status_cli_names_the_failed_project(tmp_path: Path, capsys) -> None:
    base = tmp_path / "projects"
    base.mkdir()
    broken = _broken_repo(base, "broken")
    activation.mark_active(broken, "claude")

    run_daemon(
        base,
        Config(),
        DaemonOptions(once=True),
        build_project=lambda root, cfg, timeout: _ok(),
    )

    main(["daemon-status", str(base)])
    out = capsys.readouterr().out

    assert "1 failing" in out
    assert str(broken.resolve()) in out
    assert SNAPSHOT_ERROR in out


def test_one_broken_repo_does_not_blind_the_daemon_to_a_healthy_one(tmp_path: Path) -> None:
    """The failure is reported per project, not by swallowing the rest."""
    base = tmp_path / "projects"
    base.mkdir()
    broken = _broken_repo(base, "broken")
    healthy = base / "healthy"
    healthy.mkdir()
    (healthy / "pyproject.toml").write_text('[project]\nname = "y"\n', encoding="utf-8")
    activation.mark_active(broken, "claude")
    activation.mark_active(healthy, "claude")

    built: list[Path] = []
    status = run_daemon(
        base,
        Config(),
        DaemonOptions(once=True),
        build_project=_recording_builder(built),
    )

    assert built == [healthy.resolve()]
    assert status["project_count"] == 2
    assert status["failing_projects"] == 1
    assert [failure["root"] for failure in status["snapshot_failures"]] == [str(broken.resolve())]


def test_a_status_file_without_the_key_is_still_readable(tmp_path: Path) -> None:
    """A status.json written before this key existed must not break the report."""
    base = tmp_path / "projects"
    base.mkdir()
    now = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
    payload = {
        "status": "ok",
        "updated_at": "2026-10-27T07:59:30+00:00",
        "project_count": 1,
        "projects": [{
            "root": "F:/Projects/app",
            "file_count": 2,
            "build_count": 1,
            "failure_count": 0,
            "last_success_at": "2026-10-27T07:59:00+00:00",
            "last_error": None,
            "needs_initial_build": False,
        }],
    }
    (base / ".graphite-daemon").mkdir()
    (base / ".graphite-daemon" / "status.json").write_text(json.dumps(payload), encoding="utf-8")

    report = evaluate_daemon_health(
        base,
        options=HealthOptions(require_process=False, require_startup=False),
        now=now,
    )

    assert report["summary"]["failing_count"] == 0
    assert report["summary"]["project_count"] == 1
    assert report["ok"] is True


def test_a_malformed_snapshot_failures_key_is_reported_not_ignored(tmp_path: Path) -> None:
    """A corrupt status file must say so -- never read as health."""
    base = tmp_path / "projects"
    base.mkdir()
    payload = {
        "status": "ok",
        "updated_at": "2026-10-27T07:59:30+00:00",
        "project_count": 1,
        "projects": [],
        "snapshot_failures": {"root": "F:/Projects/app"},
    }
    (base / ".graphite-daemon").mkdir()
    (base / ".graphite-daemon" / "status.json").write_text(json.dumps(payload), encoding="utf-8")

    report = evaluate_daemon_health(
        base,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    assert report["ok"] is False
    assert "status_schema_invalid" in {issue["code"] for issue in report["errors"]}


def test_a_snapshot_failure_without_a_reason_is_still_counted_and_named(
    tmp_path: Path,
) -> None:
    """A missing reason must not un-count the failure -- that is the defect."""
    base = tmp_path / "projects"
    base.mkdir()
    payload = {
        "status": "ok",
        "updated_at": "2026-10-27T07:59:30+00:00",
        "project_count": 1,
        "projects": [],
        "snapshot_failures": [{"root": "F:/Projects/app", "error": "", "failed_at": "2026-10-27T07:59:00+00:00"}],
    }
    (base / ".graphite-daemon").mkdir()
    (base / ".graphite-daemon" / "status.json").write_text(json.dumps(payload), encoding="utf-8")

    report = evaluate_daemon_health(
        base,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    assert report["ok"] is False
    assert report["summary"]["failing_count"] == 1
    assert [project["root"] for project in report["projects"]["failing"]] == ["F:/Projects/app"]
    assert "project_snapshot_failed" in {issue["code"] for issue in report["errors"]}


def test_unknown_fields_in_a_snapshot_failure_are_not_leaked(tmp_path: Path) -> None:
    secret = "UNKNOWN-SECRET-" + ("X" * 10_000)
    base = tmp_path / "projects"
    base.mkdir()
    payload = {
        "status": "degraded",
        "updated_at": "2026-10-27T07:59:30+00:00",
        "project_count": 1,
        "projects": [],
        "snapshot_failures": [{
            "root": "F:/Projects/app",
            "error": SNAPSHOT_ERROR,
            "failed_at": "2026-10-27T07:59:00+00:00",
            "unknown_blob": secret,
        }],
    }
    (base / ".graphite-daemon").mkdir()
    (base / ".graphite-daemon" / "status.json").write_text(json.dumps(payload), encoding="utf-8")

    report = evaluate_daemon_health(
        base,
        options=HealthOptions(require_process=False, require_startup=False),
    )

    assert secret not in json.dumps(report)
    assert report["summary"]["failing_count"] == 1


def _daemon_events(base: Path) -> list[dict]:
    log = base / ".graphite-daemon" / "graphite-daemon.log"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_snapshot_failure_log_carries_the_chained_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`str(exc)` alone says what failed, not why.

    Seven distinct sites raise `GitLaunchError` inside `_run_bounded` alone;
    its step token and errno are what separate them. On 2026-10-27 the
    daemon failed every snapshot for thirteen minutes and the log could not
    say why -- the cause was in the exception's chain and the chain was
    discarded.
    """
    base = tmp_path / "projects"
    base.mkdir()
    broken = _broken_repo(base, "broken")
    activation.mark_active(broken, "claude")

    def _snapshot(root, cfg):
        try:
            raise GitLaunchError("unable to run Git command", reason="spawn", os_error=2)
        except GitLaunchError as exc:
            raise IngestError(SNAPSHOT_ERROR) from exc

    monkeypatch.setattr(daemon_mod, "snapshot", _snapshot)

    run_daemon(
        base,
        Config(),
        DaemonOptions(once=True),
        build_project=lambda root, cfg, timeout: _ok(),
    )

    events = [event for event in _daemon_events(base) if event.get("event") == "project_snapshot_failed"]
    assert len(events) == 1
    cause = events[0]["cause"]
    assert "IngestError" in cause
    assert "GitLaunchError" in cause
    assert "spawn" in cause
    assert "os=2" in cause


def test_the_cause_chain_is_path_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The chain names classes and steps, never a path or Git's own text."""
    base = tmp_path / "projects"
    base.mkdir()
    broken = _broken_repo(base, "broken")
    activation.mark_active(broken, "claude")
    secret = "SECRET-PATH-" + ("X" * 40)

    def _snapshot(root, cfg):
        try:
            raise GitLaunchError(
                f"unable to run Git command in {secret}", reason="spawn", os_error=2
            )
        except GitLaunchError as exc:
            raise IngestError(SNAPSHOT_ERROR) from exc

    monkeypatch.setattr(daemon_mod, "snapshot", _snapshot)

    run_daemon(
        base,
        Config(),
        DaemonOptions(once=True),
        build_project=lambda root, cfg, timeout: _ok(),
    )

    events = [event for event in _daemon_events(base) if event.get("event") == "project_snapshot_failed"]
    assert len(events) == 1
    assert events[0]["cause"]
    assert secret not in json.dumps(events)


def test_the_exit_status_reaches_the_daemon_log(tmp_path: Path) -> None:
    """The whole point of the cause: a git command that exits non-zero now
    says so, instead of the daemon logging one flat sentence for fourteen
    minutes."""
    base = tmp_path / "projects"
    base.mkdir()
    broken = _broken_repo(base, "broken")
    activation.mark_active(broken, "claude")

    class _FakeRunner:
        def __init__(self, project_root):
            pass

        def run(self, arguments, *, timeout_seconds, **kwargs):
            return subprocess.CompletedProcess(arguments, 128, b"", b"")

    monkeypatch_git = pytest.MonkeyPatch()
    monkeypatch_git.setattr(ingest_module, "GitRunner", _FakeRunner)
    try:
        run_daemon(
            base,
            Config(),
            DaemonOptions(once=True),
            build_project=lambda root, cfg, timeout: _ok(),
        )
    finally:
        monkeypatch_git.undo()

    events = [event for event in _daemon_events(base) if event.get("event") == "project_snapshot_failed"]
    assert len(events) == 1
    assert "exit_128" in events[0]["cause"]


def test_the_cause_chain_reads_the_whole_chain_and_stops_at_a_cycle() -> None:
    inner = GitLaunchError("unable to run Git command", reason="spawn", os_error=2)
    middle = IngestError(SNAPSHOT_ERROR)
    middle.__cause__ = inner
    outer = RuntimeError("snapshot failed")
    outer.__cause__ = middle

    assert describe_exception_chain(outer) == (
        "RuntimeError <- IngestError <- GitLaunchError:spawn:os=2"
    )

    # A cause that points back at itself must not loop forever.
    looped = RuntimeError("loop")
    looped.__cause__ = looped
    assert describe_exception_chain(looped) == "RuntimeError"


def test_the_cause_chain_stops_at_a_bounded_depth() -> None:
    current: BaseException = ValueError("root")
    for index in range(20):
        wrapper = RuntimeError(f"level {index}")
        wrapper.__cause__ = current
        current = wrapper

    chain = describe_exception_chain(current)

    assert chain.count(" <- ") < 10
    assert chain.startswith("RuntimeError <- RuntimeError")
