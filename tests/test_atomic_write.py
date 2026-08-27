"""`atomic_write_text` under a concurrent reader -- the Windows rename race.

Seen in the daemon log three times (2026-08-11 x2, 2026-08-27) as
`[WinError 5] Access is denied: '..graphite_graph.json.<tmp>' -> '.graphite_graph.json'`.
`MoveFileEx` cannot replace a file another handle holds open without
FILE_SHARE_DELETE, and neither `open()` nor `os.open()` grants that share
mode -- so every graphite reader (`graph_io`, the MCP server, a hook running
`query`) makes the writer fail for the length of its read. The daemon masks
the failure by retrying next cycle; a hook- or agent-driven `build` does not.
"""
from __future__ import annotations

import errno
import os
import threading
from pathlib import Path

import pytest

from graphite import io as graphite_io
from graphite.io import atomic_write_text


class _FakeClock:
    """Deterministic stand-ins for the writer's module-local clock and sleep.

    Patched on `graphite.io`'s own aliases, never on the global `time` module:
    freezing `time.monotonic` process-wide is how #45 hung a test run.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _install_clock(monkeypatch: pytest.MonkeyPatch, *, retry: bool) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(graphite_io, "_RETRY_REPLACE_ON_ACCESS_DENIED", retry)
    monkeypatch.setattr(graphite_io, "_monotonic", clock.monotonic)
    monkeypatch.setattr(graphite_io, "_sleep", clock.sleep)
    return clock


@pytest.mark.skipif(os.name != "nt", reason="MoveFileEx over an open handle is a Windows behaviour")
def test_atomic_write_text_outlasts_a_reader_holding_the_target_open(tmp_path: Path) -> None:
    target = tmp_path / "graph.json"
    atomic_write_text(target, "old")
    opened = threading.Event()
    release = threading.Event()

    def hold_open() -> None:
        # Same share mode as graph_io's os.open(): read and write shared, delete NOT.
        with open(target, "rb"):
            opened.set()
            release.wait(5.0)

    reader = threading.Thread(target=hold_open)
    reader.start()
    assert opened.wait(5.0)
    releaser = threading.Timer(0.15, release.set)
    releaser.start()
    try:
        atomic_write_text(target, "new")
    finally:
        release.set()
        releaser.cancel()
        reader.join(5.0)

    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.glob(".graph.json.*.tmp")) == []


def test_replace_is_retried_while_access_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _install_clock(monkeypatch, retry=True)
    target = tmp_path / "artifact.txt"
    atomic_write_text(target, "old")
    real_replace = os.replace
    attempts: list[int] = []

    def denied_twice(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise PermissionError(errno.EACCES, "held open by a reader")
        real_replace(src, dst)

    monkeypatch.setattr(graphite_io.os, "replace", denied_twice)
    atomic_write_text(target, "new")

    assert attempts == [1, 2, 3]
    assert target.read_text(encoding="utf-8") == "new"
    assert len(clock.sleeps) == 2
    assert all(seconds > 0 for seconds in clock.sleeps)
    assert list(tmp_path.glob(".artifact.txt.*.tmp")) == []


def test_replace_gives_up_after_the_retry_budget_and_removes_the_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _install_clock(monkeypatch, retry=True)
    target = tmp_path / "artifact.txt"
    atomic_write_text(target, "old")

    def never_released(src: object, dst: object) -> None:
        raise PermissionError(errno.EACCES, "never released")

    monkeypatch.setattr(graphite_io.os, "replace", never_released)
    with pytest.raises(PermissionError):
        atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".artifact.txt.*.tmp")) == []
    assert clock.sleeps, "it must wait at least once before giving up"
    budget = graphite_io._REPLACE_RETRY_SECONDS + graphite_io._REPLACE_RETRY_MAX_DELAY
    assert sum(clock.sleeps) <= budget, "it must not wait past the budget"


def test_replace_is_not_retried_where_access_denied_is_permanent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # POSIX: EACCES on rename is a directory permission, not a reader. Waiting
    # 2s to report it would be a behaviour change for no gain.
    clock = _install_clock(monkeypatch, retry=False)
    target = tmp_path / "artifact.txt"
    atomic_write_text(target, "old")
    attempts: list[int] = []

    def denied(src: object, dst: object) -> None:
        attempts.append(1)
        raise PermissionError(errno.EACCES, "directory not writable")

    monkeypatch.setattr(graphite_io.os, "replace", denied)
    with pytest.raises(PermissionError):
        atomic_write_text(target, "new")

    assert len(attempts) == 1
    assert clock.sleeps == []


def test_the_replace_error_survives_a_failing_temp_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The 2026-08-11 WinError 32 log line named ONLY the temp file, which
    # os.replace never does: the cleanup's own failure had replaced the
    # error that mattered. The original must propagate.
    target = tmp_path / "artifact.txt"
    atomic_write_text(target, "old")

    def replace_fails(src: object, dst: object) -> None:
        raise OSError(errno.EIO, "the error that matters")

    def unlink_fails(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(errno.EACCES, "temp file held by an indexer")

    monkeypatch.setattr(graphite_io.os, "replace", replace_fails)
    monkeypatch.setattr(Path, "unlink", unlink_fails)
    with pytest.raises(OSError) as caught:
        atomic_write_text(target, "new")

    assert caught.value.errno == errno.EIO
