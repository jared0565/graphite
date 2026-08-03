"""Recovery from an abandoned channel lock.

`_Lock` guarded the critical section but recorded nothing about who held it, so
a holder that was *killed* -- rather than merely stalled, which the 20s `_git`
timeout already covers -- left `.channel.lock` behind forever. Every agent was
locked out until a human deleted the file.

The property these tests defend is asymmetric, and deliberately so: breaking a
live lock puts two writers in the critical section, which is the exact failure
the lock exists to prevent and is worse than the wedge it would be recovering
from. So "never break a live lock" outranks "always recover promptly", and the
guards below are written to be provable in that direction.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from graphite import channel


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _make_channel(tmp_path: Path, registry: dict[str, str] | None = None) -> Path:
    root = tmp_path / ".agent-channel"
    (root / "rounds").mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "operator@example.com")
    _git(root, "config", "user.name", "Operator")
    (root / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")
    if registry is not None:
        channel.write_registry(root, registry)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _plant_lock(root: Path, *, started_at: float, pid: int = 999_999) -> Path:
    """A lock file exactly as a holder that never ran `__exit__` would leave it."""
    path = root / ".channel.lock"
    path.write_text(
        json.dumps({"pid": pid, "started_at": started_at, "host": "dead-host"}),
        encoding="utf-8",
    )
    return path


# --- recovery ---------------------------------------------------------------


def test_an_abandoned_lock_is_broken_once_it_passes_the_ttl(tmp_path: Path) -> None:
    now = 10_000.0
    _plant_lock(tmp_path, started_at=now - channel._LOCK_STALE_SECONDS - 1.0)

    with channel._Lock(tmp_path, clock=lambda: now) as lock:
        assert lock.recovered is not None, "an abandoned lock must be recoverable"
        assert lock.recovered["pid"] == 999_999
        assert lock.recovered["host"] == "dead-host"


def test_a_live_lock_is_never_broken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard has to discriminate, not merely permit: same setup as the test
    above with only the age changed, and the outcome must invert."""
    now = 10_000.0
    _plant_lock(tmp_path, started_at=now - 1.0)
    monkeypatch.setattr(channel, "_LOCK_TIMEOUT_SECONDS", 0.2)

    with pytest.raises(channel.ChannelError) as excinfo:
        with channel._Lock(tmp_path, clock=lambda: now):
            pass

    assert excinfo.value.code == "lock_timeout"
    assert (tmp_path / ".channel.lock").exists(), "a live holder's lock was deleted"


def test_an_unreadable_lock_file_is_broken_once_it_passes_the_ttl(tmp_path: Path) -> None:
    """Garbage carries no `started_at`, so it can never age out on its own. It
    still has to age out by SOMETHING -- blocking forever on a file nobody can
    parse is the wedge with extra steps."""
    path = tmp_path / ".channel.lock"
    path.write_text("{not json", encoding="utf-8")
    aged = 10_000.0 - channel._LOCK_STALE_SECONDS - 1.0
    os.utime(path, (aged, aged))

    with channel._Lock(tmp_path, clock=lambda: 10_000.0) as lock:
        assert lock.recovered is not None


def test_a_lock_created_but_not_yet_written_is_not_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_acquire` creates the file with O_EXCL and writes the record a moment
    later, so an EMPTY lock is a normal state on the acquisition path, not
    evidence of a crash. Treating unparseable content as instantly stale would
    break a lock that had just been legitimately taken -- and unlike the
    single-syscall residual in `_break_if_abandoned`, that window is on the
    ordinary contended path, not on a rare recovery."""
    (tmp_path / ".channel.lock").touch()
    monkeypatch.setattr(channel, "_LOCK_TIMEOUT_SECONDS", 0.2)

    with pytest.raises(channel.ChannelError) as excinfo:
        with channel._Lock(tmp_path):
            pass

    assert excinfo.value.code == "lock_timeout"
    assert (tmp_path / ".channel.lock").exists(), "broke a lock mid-acquisition"


def test_the_lock_records_who_holds_it(tmp_path: Path) -> None:
    with channel._Lock(tmp_path) as lock:
        record = json.loads(lock.path.read_text(encoding="utf-8"))

    assert isinstance(record["pid"], int)
    assert isinstance(record["started_at"], float)
    assert record["host"]


# --- the stampede ------------------------------------------------------------


def test_a_break_is_abandoned_when_the_record_changed_under_us(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several agents wedged behind one dead holder all judge it stale together.
    The first breaker unlinks and immediately re-creates the lock for itself;
    without the confirming read every other breaker would then unlink that FRESH
    lock and they would all enter the critical section at once."""
    now = 10_000.0
    stale = {"pid": 999_999, "started_at": now - channel._LOCK_STALE_SECONDS - 1.0, "host": "dead"}
    winner = {"pid": 4242, "started_at": now, "host": "live"}
    lock = channel._Lock(tmp_path, clock=lambda: now)
    lock.path.write_text(json.dumps(winner), encoding="utf-8")
    reads = iter([stale, winner])
    monkeypatch.setattr(lock, "_read_record", lambda: next(reads))

    assert lock._break_if_abandoned() is False
    assert lock.path.exists(), "the winning breaker's fresh lock was trampled"
    assert json.loads(lock.path.read_text(encoding="utf-8"))["pid"] == 4242


def test_exit_does_not_release_a_lock_that_is_no_longer_ours(tmp_path: Path) -> None:
    """If a spurious break ever does let a second holder in, releasing on the
    way out would trample them too -- turning one overlap into a cascade."""
    lock = channel._Lock(tmp_path)
    lock.__enter__()
    lock.path.write_text(
        json.dumps({"pid": 4242, "started_at": 1.0, "host": "other"}), encoding="utf-8"
    )

    lock.__exit__(None, None, None)

    assert lock.path.exists(), "released a lock belonging to another holder"


def test_the_lock_is_released_normally(tmp_path: Path) -> None:
    with channel._Lock(tmp_path) as lock:
        assert lock.path.exists()

    assert not lock.path.exists()


# --- what recovery must not hide ---------------------------------------------


def test_recovery_reports_the_uncommitted_files_the_dead_holder_left(tmp_path: Path) -> None:
    """`status_events` reads from DISK, so an orphaned `status/NNN/*.json` from a
    holder that died before committing silently suppresses redelivery of that
    round. Clearing the lock by hand came with an instruction to look for it;
    clearing it automatically has to carry that instruction itself."""
    root = _make_channel(tmp_path)
    orphan = root / "status" / "001"
    orphan.mkdir(parents=True)
    (orphan / "0001-delivered.json").write_text("{}", encoding="utf-8")
    now = 10_000.0
    _plant_lock(root, started_at=now - channel._LOCK_STALE_SECONDS - 1.0)

    with channel._Lock(root, clock=lambda: now) as lock:
        assert lock.recovered is not None
        assert lock.recovered["residue"] == ["status/001/0001-delivered.json"]


def test_post_round_surfaces_a_recovered_lock_to_its_caller(tmp_path: Path) -> None:
    """The break has to reach a human. There is no logger here, and the return
    value is what an agent actually sees."""
    repo = tmp_path / "aramid"
    repo.mkdir()
    root = _make_channel(tmp_path, {str(repo): "aramid-agent"})
    _plant_lock(root, started_at=0.0)

    result = channel.post_round(root, repo, title="Hello", body="Body")

    assert result["ok"] is True
    assert result["lock_recovered"]["pid"] == 999_999


def test_an_ordinary_post_reports_no_recovery(tmp_path: Path) -> None:
    repo = tmp_path / "aramid"
    repo.mkdir()
    root = _make_channel(tmp_path, {str(repo): "aramid-agent"})

    result = channel.post_round(root, repo, title="Hello", body="Body")

    assert "lock_recovered" not in result, "a quiet key on every response is noise"
