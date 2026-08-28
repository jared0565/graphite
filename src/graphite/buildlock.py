"""Cross-process build lock so only one builder touches a repo at a time.

The daemon was historically the sole builder per repo, serialized by its own
cycle, so nothing needed a lock. Git-hook-triggered builds introduce the first
genuine concurrency, and a manual `graphite build` during a daemon build
already raced before them.

Staleness is TTL-only, deliberately. `os.kill(pid, 0)` is the obvious liveness
idiom and is WRONG here: on Windows, Python's `os.kill` ignores signal 0 and
calls TerminateProcess, so the portable-looking probe would kill the very
process it was checking.

The holder must be the process that writes (#60). The daemon used to take the
lock and hand its child an escape hatch; force-stopping the daemon then left
the record on disk with a dead pid while the orphaned child kept writing
graph-out/, and the successor could neither trust "holder dead" nor see
"writer done" -- so it skipped the repo for the whole TTL. Liveness probing
would not have helped: the recorded pid was dead while the writer was alive.
With the writer as holder, its death and its writes end together.
"""
from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

# 2x the daemon's own build_timeout_seconds default (240.0). A build still
# holding the lock past double the daemon's give-up point is dead by definition.
DEFAULT_TTL_SECONDS = 600.0

# A builder that cannot take the lock exits 0: a skipped build is a normal
# outcome for a human or a git hook, not a failure. The daemon needs more --
# "another builder owns the repo" must not be booked as a failed build -- so
# it sets this in its child's environment, and the child answers a refusal
# with REFUSED_EXIT_STATUS instead. Mirrors the GRAPHITE_DAEMON_CHILD convention.
ENV_REPORT_REFUSAL = "GRAPHITE_BUILD_LOCK_REPORT_REFUSAL"
REFUSED_EXIT_STATUS = 75  # sysexits.h EX_TEMPFAIL: try again later


def lock_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / ".build.lock"


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_stale(record: dict[str, Any] | None, now: float, ttl_seconds: float) -> bool:
    if record is None:
        return True  # unreadable or garbage: never block builds forever
    started = record.get("started_at")
    if not isinstance(started, (int, float)) or isinstance(started, bool):
        return True
    return (now - float(started)) > ttl_seconds


def is_held(
    cache_dir: Path,
    *,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    clock: Callable[[], float] = time.time,
) -> bool:
    """Advisory: would a builder be refused right now?

    Reads only -- never acquires, never unlinks, never steals. The answer can
    be stale by the time the caller acts on it, so correctness still rests on
    the acquirer's own `build_lock`. The daemon uses it to avoid spawning a
    child every cycle only to have it refused.
    """
    path = lock_path(cache_dir)
    if not path.exists():
        return False
    return not _is_stale(_read_record(path), clock(), ttl_seconds)


def release_if_held_by(cache_dir: Path, *, pid: int) -> bool:
    """Unlink the lock if its record names `pid`; True if it did.

    For a parent that has just killed `pid`: the child died without its
    `finally`, and no other process can hold a record carrying its pid, so this
    releases on the dead child's behalf rather than stealing. It is NOT a
    liveness probe -- the caller must already know the process is dead.
    """
    path = lock_path(cache_dir)
    record = _read_record(path)
    if record is None or record.get("pid") != pid:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _create_exclusive(path: Path, now: float) -> bool:
    """Atomically create the lock file. False if someone else already has it."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(
            {"pid": os.getpid(), "started_at": now, "host": socket.gethostname()},
            handle,
        )
    return True


@contextmanager
def build_lock(
    cache_dir: Path,
    *,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    clock: Callable[[], float] = time.time,
) -> Iterator[bool]:
    """Yield True if this process acquired the build lock, else False.

    The lock is released on exit ONLY when it was acquired here -- a refused
    builder must never unlink a lock it does not own.
    """
    path = lock_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    acquired = _create_exclusive(path, clock())

    if not acquired and _is_stale(_read_record(path), clock(), ttl_seconds):
        try:
            path.unlink()
        except OSError:
            pass
        acquired = _create_exclusive(path, clock())

    try:
        yield acquired
    finally:
        if acquired:
            try:
                path.unlink()
            except OSError:
                pass
