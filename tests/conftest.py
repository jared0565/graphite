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

from graphite import activation, usage_ledger


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


# --- tests that read the repository's documentation tree ------------------------
#
# The source distribution ships `tests/` but NOT `docs/`. The documentation is
# the repository's internal engineering record, and two dozen of its documents
# quote a developer machine's absolute paths -- which RELEASING.md makes a stop
# condition for an archive, and which a record of what was decided should not
# be rewritten to suit a packaging boundary.
#
# The tests below read that tree, so in an unpacked sdist they cannot be
# evaluated. They SKIP rather than FAIL: a failure there would say the
# artifact is broken, and it is not -- it is a source distribution that
# deliberately does not carry the repository's documentation. In a checkout
# the tree is present, nothing is skipped, and these run exactly as before.
#
# MEASURED 2026-10-29 against the unpacked 1.8.0 sdist: the suite did not
# COLLECT at all -- 6 collection errors, 0 tests run -- and those errors were
# the missing `scripts/` and `benchmarks/` modules, which the packaging config
# now ships. These 46 tests are what remained.
_REPOSITORY_TREE_MODULES = frozenset(
    {
        "test_cli_reference",
        "test_configuration_reference",
        "test_documentation",
        "test_exit_codes_reference",
        "test_published_schemas",
    }
)


# --- tests that run a REAL manifest build (#146) -------------------------------
#
# `probe_mcp` launches a manifest builder child before the server. A test that
# leaves that child real pays for a real build, and a COLD one -- no cached
# manifest for that cache key -- walks every package's metadata. The walk is
# syscall-bound on Windows (`_getfinalpathname`, `_path_exists`), so under
# contention each call yields its slice and queues behind the busy processes:
# it slows far more than its CPU share would predict. The probe's default 20s
# budget (19s of execution) is an idle-machine figure, and the suite spends it
# on a loaded one.
#
# MEASURED 2026-11-05 on a 12-core Windows dev box, cold build, dev venv:
#
#   idle                          n=5   3.60-3.96s
#   2x oversubscribed             n=20  p50 21.1s  p90 30.0s  max 52.3s
#   (24 CPU burners on 12 cores)
#
# CI that day, Windows cells: five runs failed with every build killed at
# 19.1-19.3s, and the pre-change tag failed the same way on the same day -- the
# runners, not a change. That gives only a LOWER bound on CI's true duration,
# which is why the budget is derived from the harsher local shape instead.
#
# Derived the way `_COLD_START_BUDGET_SECONDS` in test_doctor.py derives its
# own: the transport's EXECUTION deadline -- `budget - min(1.0, 0.4 * budget)`
# -- about 4x the loaded p90. 4 x 30.0s = 120s; a 120s budget leaves 119s of
# execution, 2.3x the slowest build measured. It costs nothing when the code is
# correct, because the run returns the moment the build does. Re-derive it if
# the build or the load model changes -- do not tune it by re-running until
# green.
#
# THE CEILING. CI runs pytest with `--timeout=120 --timeout-method=thread`, and
# that method calls `os._exit` -- it kills the whole xdist worker, not the
# test. A test legitimately waiting out this budget would die mid-wait and take
# the rest of its file with it. So every test that requests the fixture also
# gets a per-test timeout above its own worst case: two builds (one test calls
# the probe twice), one real server phase (20s), and a minute for the rest.
# The local gate passes no `--timeout`, so only CI would ever show that
# ceiling -- which is why it is stated here.
#
# THE HANG COST. "Costs nothing" holds only while the build FINISHES. If the
# builder genuinely hangs, the cache never fills and every test requesting the
# fixture waits out the whole budget: test_doctor.py runs on one worker under
# `--dist loadfile` and holds about 24 such items, roughly 48 minutes in
# series -- past the CI test job's 30-minute `timeout-minutes`, which kills the
# job with no pytest summary (the #45 shape). Under the old 20s default the same
# hang cost about 8 minutes of clean `timeout` failures. Either way a hang is a
# real defect; this records what it would now cost to see one.
#
# NOT for a test whose subject IS the manifest budget (the hard-bound test in
# test_doctor.py): it keeps its own number.
COLD_MANIFEST_BUILD_BUDGET_SECONDS = 120.0
_COLD_MANIFEST_BUILD_TEST_TIMEOUT_SECONDS = 2 * COLD_MANIFEST_BUILD_BUDGET_SECONDS + 20 + 60
_COLD_MANIFEST_BUILD_FIXTURE = "cold_manifest_build_budget"


@pytest.fixture
def cold_manifest_build_budget() -> float:
    """The manifest budget for a test that runs a REAL build (see above)."""
    return COLD_MANIFEST_BUILD_BUDGET_SECONDS


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    ceiling = pytest.mark.timeout(_COLD_MANIFEST_BUILD_TEST_TIMEOUT_SECONDS)
    for item in items:
        if _COLD_MANIFEST_BUILD_FIXTURE in getattr(item, "fixturenames", ()):
            item.add_marker(ceiling)
    if (_REPO_ROOT / "docs").is_dir():
        return
    skip = pytest.mark.skip(
        reason="the documentation tree is not part of the source distribution"
    )
    for item in items:
        module = getattr(item, "module", None)
        name = getattr(module, "__name__", "")
        if name.rsplit(".", 1)[-1] in _REPOSITORY_TREE_MODULES:
            item.add_marker(skip)


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


@pytest.fixture(autouse=True)
def _isolate_session_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may inherit the machine's own session identity.

    A usage-ledger entry records the session that made it, read from the
    environment (#110), and the Stop hook counts only the entries naming its
    own session. A test that inherited this machine's session id would tag
    its entries with it, and the hook -- which a test gives a made-up session
    id -- would count none of them. The suite would then pass on a machine
    whose environment carries no session and fail on one that does: a green
    run that proved nothing on the machine that made it.
    """
    for name in usage_ledger.SESSION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


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


# --- drive graphite.io's replace-retry loop deterministically -----------------
#
# `io.replace_file` waits out a reader that holds the target open (Windows
# WinError 5, #59). These stand-ins are patched on graphite.io's OWN aliases,
# never on the global `time` module: freezing `time.monotonic` process-wide is
# how #45 hung a test run.
class ReplaceRetryClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def replace_retry_clock(monkeypatch: pytest.MonkeyPatch) -> ReplaceRetryClock:
    """Force the retry path on every platform and make its waits observable."""
    from graphite import io as graphite_io

    clock = ReplaceRetryClock()
    monkeypatch.setattr(graphite_io, "_RETRY_REPLACE_ON_ACCESS_DENIED", True)
    monkeypatch.setattr(graphite_io, "_monotonic", clock.monotonic)
    monkeypatch.setattr(graphite_io, "_sleep", clock.sleep)
    return clock


@pytest.fixture
def replace_denied(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], list[int]]:
    """`os.replace` raises EACCES for the first `times` calls, then succeeds.

    Returns the attempts list so a test can assert how many tries it took.
    """
    import errno
    import os

    def install(times: int) -> list[int]:
        real_replace = os.replace
        attempts: list[int] = []

        def denied(src: object, dst: object) -> None:
            attempts.append(len(attempts) + 1)
            if len(attempts) <= times:
                raise PermissionError(errno.EACCES, "held open by a reader")
            real_replace(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "replace", denied)
        return attempts

    return install
