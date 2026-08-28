"""Cross-process build lock: only one builder per repo at a time.

Graphite had no cross-process locking at all -- the daemon held only an
in-process threading.Lock, because it had always been the sole builder per
repo. A manual `graphite build` during a daemon build already raced; git-hook
builds (Plan B) make that the normal case.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite.buildlock import DEFAULT_TTL_SECONDS, build_lock, is_held, lock_path, release_if_held_by


def test_acquires_when_free_and_releases_after(tmp_path: Path) -> None:
    cache = tmp_path / ".cache" / "graphite"

    with build_lock(cache) as acquired:
        assert acquired is True
        assert lock_path(cache).exists(), "lock file must exist while held"

    assert not lock_path(cache).exists(), "lock must be released on exit"


def test_second_acquisition_is_refused_while_held(tmp_path: Path) -> None:
    cache = tmp_path / ".cache" / "graphite"

    with build_lock(cache) as first:
        assert first is True
        with build_lock(cache) as second:
            assert second is False, "a live lock must refuse a second builder"

    assert not lock_path(cache).exists()


def test_inner_refusal_does_not_release_the_outer_lock(tmp_path: Path) -> None:
    """The refused builder must not unlink a lock it never owned."""
    cache = tmp_path / ".cache" / "graphite"

    with build_lock(cache) as first:
        assert first is True
        with build_lock(cache) as second:
            assert second is False
        assert lock_path(cache).exists(), "refused builder deleted the live lock"


def test_stale_lock_is_stolen(tmp_path: Path) -> None:
    cache = tmp_path / ".cache" / "graphite"
    now = [1000.0]

    with build_lock(cache, clock=lambda: now[0]) as acquired:
        assert acquired is True
        now[0] += DEFAULT_TTL_SECONDS + 1.0
        with build_lock(cache, clock=lambda: now[0]) as second:
            assert second is True, "a lock older than the TTL must be stealable"


def test_garbage_lock_file_is_treated_as_stale(tmp_path: Path) -> None:
    cache = tmp_path / ".cache" / "graphite"
    cache.mkdir(parents=True)
    lock_path(cache).write_text("{not json", encoding="utf-8")

    with build_lock(cache) as acquired:
        assert acquired is True, "an unreadable lock must not block builds forever"


def test_lock_is_released_when_the_body_raises(tmp_path: Path) -> None:
    cache = tmp_path / ".cache" / "graphite"

    with pytest.raises(RuntimeError):
        with build_lock(cache) as acquired:
            assert acquired is True
            raise RuntimeError("build blew up")

    assert not lock_path(cache).exists(), "a crashed build must not leak the lock"


def test_lock_record_identifies_the_holder(tmp_path: Path) -> None:
    cache = tmp_path / ".cache" / "graphite"

    with build_lock(cache, clock=lambda: 1234.0):
        record = json.loads(lock_path(cache).read_text(encoding="utf-8"))

    assert record["started_at"] == 1234.0
    assert isinstance(record["pid"], int)
    assert isinstance(record["host"], str) and record["host"]


def test_is_held_is_false_when_no_lock_exists(tmp_path: Path) -> None:
    assert is_held(tmp_path / ".cache" / "graphite") is False


def test_is_held_is_true_while_a_live_builder_holds_it(tmp_path: Path) -> None:
    cache = tmp_path / ".cache" / "graphite"

    with build_lock(cache) as acquired:
        assert acquired is True
        assert is_held(cache) is True
        assert lock_path(cache).exists(), "an advisory read released the lock"

    assert is_held(cache) is False


def test_is_held_is_false_for_a_lock_older_than_the_ttl(tmp_path: Path) -> None:
    """Same aging rule as an acquirer -- but a read never steals."""
    cache = tmp_path / ".cache" / "graphite"
    now = [1000.0]

    with build_lock(cache, clock=lambda: now[0]) as acquired:
        assert acquired is True
        now[0] += DEFAULT_TTL_SECONDS + 1.0
        assert is_held(cache, clock=lambda: now[0]) is False
        assert lock_path(cache).exists(), "an advisory read unlinked a stale lock; only an acquirer may"


def test_release_if_held_by_unlinks_the_named_holders_lock(tmp_path: Path) -> None:
    cache = tmp_path / ".cache" / "graphite"
    cache.mkdir(parents=True)
    lock_path(cache).write_text(json.dumps({"pid": 4242, "started_at": 1.0, "host": "h"}), encoding="utf-8")

    assert release_if_held_by(cache, pid=4242) is True
    assert not lock_path(cache).exists()


def test_release_if_held_by_leaves_another_holders_lock_alone(tmp_path: Path) -> None:
    """Release on a dead child's behalf is not a steal: a record naming anyone
    else stays, however old it is."""
    cache = tmp_path / ".cache" / "graphite"
    cache.mkdir(parents=True)
    lock_path(cache).write_text(json.dumps({"pid": 4242, "started_at": 1.0, "host": "h"}), encoding="utf-8")

    assert release_if_held_by(cache, pid=4243) is False
    assert lock_path(cache).exists()


def test_release_if_held_by_is_a_no_op_without_a_lock(tmp_path: Path) -> None:
    assert release_if_held_by(tmp_path / ".cache" / "graphite", pid=4242) is False
