"""Registry of repositories currently open in a coding agent.

The daemon supervises exactly the repositories with a live marker here, and
leaves every other repository untouched -- no snapshot, no build. Markers are
written by agent hooks (see ``agent_hooks``) and by interactive graphite CLI
invocations, and expire on a TTL because no agent reliably signals session end.

Every function is fail-open: an IO, permission, or parse problem yields a no-op
rather than an exception. Activation is bookkeeping and must never be able to
break an agent session or a daemon cycle.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .io import replace_file

ACTIVATION_TTL_SECONDS: float = 3600.0
ENV_STATE_DIR = "GRAPHITE_STATE_DIR"
ENV_DAEMON_CHILD = "GRAPHITE_DAEMON_CHILD"


@dataclass(frozen=True)
class ActivationRecord:
    """One repository that is open right now."""

    root: Path
    agent: str
    first_seen: float
    last_seen: float


def state_dir() -> Path:
    """User-scoped state directory, deliberately not tied to a daemon base path."""
    override = os.environ.get(ENV_STATE_DIR)
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "graphite"
    return Path.home() / ".local" / "state" / "graphite"


def active_dir() -> Path:
    return state_dir() / "active"


def marker_path(root: Path) -> Path:
    digest = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]
    return active_dir() / f"{digest}.json"


def is_daemon_child() -> bool:
    """True inside a build the daemon spawned.

    The daemon runs ``-m graphite build`` inside the repo it is supervising. If
    that invocation refreshed the marker, an activated repo would renew its own
    activation forever and never expire.
    """
    return bool(os.environ.get(ENV_DAEMON_CHILD))


def mark_active(root: Path, agent: str = "unknown", *, now: float | None = None) -> Path | None:
    """Create or refresh this repository's marker. Returns None on any no-op."""
    if is_daemon_child():
        return None
    stamp = time.time() if now is None else now
    try:
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            return None
        path = marker_path(resolved)
        path.parent.mkdir(parents=True, exist_ok=True)
        first_seen = stamp
        existing = _read_marker(path)
        if existing is not None:
            candidate = existing.get("first_seen")
            if isinstance(candidate, (int, float)):
                first_seen = min(float(candidate), stamp)
        payload = {
            "root": str(resolved),
            "agent": agent,
            "first_seen": first_seen,
            "last_seen": stamp,
        }
        _atomic_write(path, payload)
        return path
    except Exception:
        return None


def read_active(
    *, ttl_seconds: float = ACTIVATION_TTL_SECONDS, now: float | None = None
) -> list[ActivationRecord]:
    """Live activation records. Expired/corrupt/orphaned markers are deleted."""
    stamp = time.time() if now is None else now
    records: list[ActivationRecord] = []
    try:
        entries = sorted(active_dir().glob("*.json"))
    except Exception:
        return []
    for path in entries:
        payload = _read_marker(path)
        if payload is None:
            _discard(path)
            continue
        try:
            root = Path(str(payload["root"]))
            last_seen = float(payload["last_seen"])
            first_seen = float(payload.get("first_seen", last_seen))
            agent = str(payload.get("agent", "unknown"))
        except Exception:
            _discard(path)
            continue
        # Clamp a future timestamp so clock skew cannot pin a repo active.
        if last_seen > stamp:
            last_seen = stamp
        if stamp - last_seen > ttl_seconds:
            _discard(path)
            continue
        if not root.is_dir():
            _discard(path)
            continue
        records.append(
            ActivationRecord(root=root, agent=agent, first_seen=first_seen, last_seen=last_seen)
        )
    return records


def _read_marker(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Temp-file + replace, so a concurrent reader never sees a partial marker."""
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        replace_file(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
