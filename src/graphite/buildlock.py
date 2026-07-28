"""Cross-process build lock so only one builder touches a repo at a time.

The daemon was historically the sole builder per repo, serialized by its own
cycle, so nothing needed a lock. Git-hook-triggered builds introduce the first
genuine concurrency, and a manual `graphite build` during a daemon build
already raced before them.

Staleness is TTL-only, deliberately. `os.kill(pid, 0)` is the obvious liveness
idiom and is WRONG here: on Windows, Python's `os.kill` ignores signal 0 and
calls TerminateProcess, so the portable-looking probe would kill the very
process it was checking.
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

# Set by a parent that already holds the lock, so its child does not deadlock
# against it. Mirrors the existing GRAPHITE_DAEMON_CHILD convention.
ENV_LOCK_HELD = "GRAPHITE_BUILD_LOCK_HELD"


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
