"""The daemon's build child holds the repo lock; the daemon does not (#60).

Force-stopping the daemon mid-build used to leave `.build.lock` on disk with the
dead daemon's pid while the orphaned child kept writing graph-out/. With the
child as holder, a killed daemon leaves no lock at all -- the orphan releases
it when it finishes -- and `build_skipped_locked` is true for exactly as long
as a build is really running.

Two consequences are pinned here. The child must report a refusal so the
daemon can tell it from a failure. And the daemon must release the lock of a
child it killed on timeout, because that child died without its `finally`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from graphite import buildlock, daemon
from graphite.config import Config
from graphite.daemon import run_graphite_build


def test_the_child_is_asked_to_report_a_refusal_and_never_told_the_lock_is_held(tmp_path: Path) -> None:
    _, env = daemon._build_command(Config(), tmp_path)

    assert env.get(buildlock.ENV_REPORT_REFUSAL) == "1"
    assert "GRAPHITE_BUILD_LOCK_HELD" not in env, "the escape hatch is what let the lock outlive the writer"
    assert buildlock.REFUSED_EXIT_STATUS not in (0, 1, 2), "a refusal must not read as success or a generic failure"


def _spawn_as_the_child(monkeypatch: pytest.MonkeyPatch, source: str, *args: str) -> None:
    """Make the daemon run `source` as its build child, with graphite importable.

    Spawns the BASE interpreter, not `sys.executable`. On Windows a venv's
    python.exe is a launcher that runs the real interpreter as a grandchild
    (measured: Popen.pid 6364, the child's os.getpid() 50080), so the lock
    record would name a pid the daemon never sees and the pid-matched release
    would -- correctly, conservatively -- refuse. Production daemons here and
    in CI run the real interpreter, which is the case these tests pin.
    """
    interpreter = getattr(sys, "_base_executable", None) or sys.executable
    src_root = str(Path(daemon.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = src_root
    monkeypatch.setattr(
        daemon, "_build_command", lambda cfg, root: ([interpreter, "-B", "-s", "-c", source, *args], env)
    )


def test_a_refusal_status_is_reported_as_locked_not_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _spawn_as_the_child(monkeypatch, f"import sys; sys.exit({buildlock.REFUSED_EXIT_STATUS})")

    result = run_graphite_build(tmp_path, Config(cache_dir=tmp_path / ".cache" / "graphite"), 30.0)

    assert result.locked is True
    assert result.success is False


def test_an_ordinary_failure_is_not_locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _spawn_as_the_child(monkeypatch, "import sys; sys.exit(1)")

    result = run_graphite_build(tmp_path, Config(cache_dir=tmp_path / ".cache" / "graphite"), 30.0)

    assert result.locked is False
    assert result.success is False


_HOLD_THE_LOCK_UNTIL_KILLED = """
import sys, time
from pathlib import Path
from graphite.buildlock import build_lock
cache, marker = Path(sys.argv[1]), Path(sys.argv[2])
with build_lock(cache) as acquired:
    marker.write_text("held" if acquired else "refused", encoding="utf-8")
    time.sleep(120)
"""


def test_a_timed_out_child_does_not_leave_its_lock_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon kills a child that overruns its budget. The kill runs none of
    the child's `finally`, so the lock it took would otherwise sit there for
    the TTL: 600 s of `build_skipped_locked` after every timeout, where the
    parent-held lock retried next cycle.

    Release is by pid match only. If the process the daemon killed is not the
    one the record names (a venv launcher on Windows), the lock stays for the
    TTL: `release_if_held_by` is not allowed to guess who is still writing.
    """
    cache = tmp_path / ".cache" / "graphite"
    marker = tmp_path / "marker"
    _spawn_as_the_child(monkeypatch, _HOLD_THE_LOCK_UNTIL_KILLED, str(cache), str(marker))

    result = run_graphite_build(tmp_path, Config(cache_dir=cache), 4.0)

    assert result.success is False and result.error is not None and "timed out" in result.error, result
    # Without this the next assertion could pass because the child never got
    # as far as taking the lock -- a clean read that measured nothing.
    assert marker.exists() and marker.read_text(encoding="utf-8") == "held", (
        "the child had not taken the lock when the timeout fired; widen the timeout"
    )
    assert not buildlock.lock_path(cache).exists(), "the daemon left its killed child's lock behind"
