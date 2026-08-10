"""Suite-wide safety rails.

The activation registry lives in a *user-scoped* directory, not under the repo,
so a test that marks a repository active would otherwise write into the real
`%LOCALAPPDATA%\\graphite\\active\\` and the live daemon would start supervising
pytest temp directories. The suite has polluted live state before (the incident
ledger, fixed in 36e2528); this fixture makes that class of bug impossible for
activation rather than relying on every test file to remember.
"""
from __future__ import annotations

import json
import sqlite3
import traceback
from pathlib import Path
from typing import Callable

import pytest

from graphite import activation


# --- every sqlite connection this repo opens must be explicitly closed -------
#
# Written because the suite could not see the defect it was supposed to. The
# `_connect` orphan -- a handle opened, abandoned when a PRAGMA failed, and
# reachable by nobody -- passed 2854 tests, ruff, semgrep, a fail-closed push
# gate and six CI legs. Nothing was broken; no test asserted the invariant, and
# a gate only enforces the assertions that exist.
#
# Two properties this has to have, both learned the hard way:
#
# * it must NOT depend on coverage. The `ResourceWarning` those leaks produced
#   only appears under `--cov`, because the tracer holds frames alive and defeats
#   refcounting. An invariant that fires only under an optional flag is not
#   default-on. So this records close() calls directly rather than watching for
#   warnings.
# * it must hold NO strong reference to a connection. Pinning every handle for
#   the length of a session would itself keep databases open, and on Windows an
#   open handle blocks unlink -- the check would cause the class of failure it
#   exists to detect. Only the allocation site is kept, as a string.
#
# `with connection:` does not call `close()`, and neither does the C-level
# deallocator, so both the transaction-mistaken-for-a-close and the
# reclaimed-by-the-collector cases are caught. That second one is the point:
# "it gets closed eventually, by GC" is precisely the property that made the
# recovery path timing-dependent.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFTEST = str(Path(__file__).resolve())
_open_connections: dict[int, str] = {}
_real_sqlite_connect = sqlite3.connect


class _TrackedConnection(sqlite3.Connection):
    def close(self) -> None:
        _open_connections.pop(id(self), None)
        super().close()


def _owning_site() -> str | None:
    """Deepest frame belonging to this repository, or None for a foreign caller.

    Foreign callers are ignored on purpose. `coverage` keeps its data in SQLite,
    so under `--cov` it opens connections this suite neither owns nor can close;
    holding it to graphite's invariant would fail the run for someone else's
    handle.
    """
    for frame in reversed(traceback.extract_stack(limit=30)):
        if frame.filename == _CONFTEST or "site-packages" in frame.filename:
            continue
        try:
            relative = Path(frame.filename).resolve().relative_to(_REPO_ROOT)
        except (ValueError, OSError):
            continue
        return f"{relative.as_posix()}:{frame.lineno} in {frame.name}"
    return None


def _tracking_connect(*arguments: object, **keywords: object) -> sqlite3.Connection:
    if "factory" not in keywords:
        keywords["factory"] = _TrackedConnection
    connection = _real_sqlite_connect(*arguments, **keywords)
    if isinstance(connection, _TrackedConnection):
        site = _owning_site()
        if site is not None:
            _open_connections[id(connection)] = site
    return connection


sqlite3.connect = _tracking_connect


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _open_connections:
        return
    sites: dict[str, int] = {}
    for site in _open_connections.values():
        sites[site] = sites.get(site, 0) + 1
    report = "\n".join(f"    {count:>4} x {site}" for site, count in sorted(sites.items()))
    print(
        f"\n\nLEAKED SQLITE CONNECTIONS: {len(_open_connections)} never had close() called\n"
        f"{report}\n"
        "Each site opened a handle that only the garbage collector can release.\n"
        "Use `with closing(...)` or an explicit `try/finally: connection.close()`.\n",
        flush=True,
    )
    if exitstatus == 0:
        # Do not overwrite a real test failure -- that diagnosis comes first.
        session.exitstatus = 1


@pytest.fixture(autouse=True)
def _isolate_activation_state(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every test's activation registry at a throwaway directory."""
    state = tmp_path_factory.mktemp("graphite-state")
    monkeypatch.setenv(activation.ENV_STATE_DIR, str(state))
    monkeypatch.delenv(activation.ENV_DAEMON_CHILD, raising=False)
    return state


@pytest.fixture
def assert_json_omits() -> Callable[[object, object], None]:
    """Assert a needle appears nowhere in a JSON payload, escaped spelling included.

    `needle not in json.dumps(payload)` is **vacuous whenever the needle is a
    Windows path**: `json.dumps` doubles every backslash, so a raw path can
    never match its own serialised form. The assertion passes while the leak sits
    in plain sight.

    That is not hypothetical. `daemon_health` embedded an absolute status path in
    its report and `test_daemon_health.py` waved it through for as long as
    windows-latest was the only platform CI ran; the portability matrix caught it
    on macOS the first time it executed, because POSIX paths have no backslashes
    to escape.

    Comparing the escaped spelling too makes the check bite on every host.
    """

    def _check(needle: object, payload: object) -> None:
        blob = payload if isinstance(payload, str) else json.dumps(payload)
        text = str(needle)
        escaped = json.dumps(text)[1:-1]
        assert text not in blob, f"{text!r} leaked into the payload"
        assert escaped not in blob, f"{text!r} leaked, escaped as {escaped!r}"

    return _check
