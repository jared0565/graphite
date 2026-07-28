# Activation-Scoped Supervision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the graphite daemon supervise only repositories currently open in a coding agent, and rebuild a repository's graph when it is opened.

**Architecture:** A new `activation` module owns a user-scoped registry of "this repo is open right now" markers. Agent hooks and the graphite CLI write markers; the daemon reads them and uses the live set as its supervised set, replacing filesystem discovery entirely. Repos with no live marker are never snapshotted and never built.

**Tech Stack:** Python 3.11+, pytest, tree-sitter (untouched here), existing `graphite.daemon` / `graphite.agent_hooks` / `graphite.init` modules.

**Spec:** `docs/superpowers/specs/2026-07-28-activation-scoped-supervision-design.md`

## Global Constraints

- Activation TTL: **3600 seconds** (60 minutes) without a heartbeat.
- State directory: `%LOCALAPPDATA%\graphite` on Windows, `~/.local/state/graphite` on POSIX, overridden by env var `GRAPHITE_STATE_DIR`.
- Daemon-child suppression env var: `GRAPHITE_DAEMON_CHILD=1`.
- Every activation code path is **fail-open**: any IO, parse, or permission problem yields a no-op, never an exception. `agent_hooks.py` is fail-open by contract (module docstring); activation must not break that.
- Timestamps in marker files are **epoch seconds (float)**. These files are machine-only state, never hand-edited, so a numeric format avoids parse ambiguity and makes TTL arithmetic and test injection trivial.
- Tests inject `now` explicitly. Never call `time.sleep` to cross a TTL boundary.
- Doctrine rule (spec §5): the words "skip" and "daemon"/"supervising"/"fresh" must never appear in the same shipped instruction.
- `init.py` has a test, `test_template_change_requires_doc_version_bump`, that pins template edits to a `DOC_VERSION` bump. Task 5 changes templates and MUST bump `DOC_VERSION` 9 → 10 in the same commit.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/graphite/activation.py` | **Create.** Sole owner of the activation registry: state-dir resolution, marker read/write, TTL expiry, daemon-child detection. No daemon or hook imports — it is a leaf module. |
| `tests/test_activation.py` | **Create.** Registry round-trip, TTL, atomicity, corruption, clock skew, daemon-child suppression. |
| `src/graphite/daemon.py` | **Modify.** Supervised set comes from `activation.read_active()` instead of `discover_projects()`. Nested-repo warning removed. |
| `src/graphite/agent_hooks.py` | **Modify.** `handle_session_start` and `handle_stop` mark the repo active. |
| `src/graphite/cli.py` | **Modify.** Universal backstop: interactive invocations mark the repo active; daemon children set `GRAPHITE_DAEMON_CHILD`. |
| `src/graphite/init.py` | **Modify.** Doctrine rewrite, `DOC_VERSION` 9 → 10, `.vscode/tasks.json` merge. |
| `src/graphite/daemon_health.py` | **Modify.** Drop `project_nested_repo_unsupervised`; report the active set. |

---

### Task 1: Activation registry module

**Files:**
- Create: `src/graphite/activation.py`
- Test: `tests/test_activation.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces:
  - `ACTIVATION_TTL_SECONDS: float = 3600.0`
  - `ENV_STATE_DIR: str = "GRAPHITE_STATE_DIR"`
  - `ENV_DAEMON_CHILD: str = "GRAPHITE_DAEMON_CHILD"`
  - `@dataclass(frozen=True) class ActivationRecord: root: Path; agent: str; first_seen: float; last_seen: float`
  - `def state_dir() -> Path`
  - `def active_dir() -> Path`
  - `def marker_path(root: Path) -> Path`
  - `def mark_active(root: Path, agent: str = "unknown", *, now: float | None = None) -> Path | None`
  - `def read_active(*, ttl_seconds: float = ACTIVATION_TTL_SECONDS, now: float | None = None) -> list[ActivationRecord]`
  - `def is_daemon_child() -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_activation.py`:

```python
"""Activation registry: which repositories are open in a coding agent right now.

The daemon's supervised set is derived from these markers, so a bug here means
either supervising repos nobody opened (the thing this replaces) or supervising
nothing at all. Every test injects `now` rather than sleeping.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite import activation


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch the real user state dir from tests."""
    state = tmp_path / "state"
    monkeypatch.setenv(activation.ENV_STATE_DIR, str(state))
    monkeypatch.delenv(activation.ENV_DAEMON_CHILD, raising=False)
    return state


def test_mark_active_then_read_returns_the_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    activation.mark_active(repo, "claude", now=1000.0)
    records = activation.read_active(now=1000.0)

    assert [r.root for r in records] == [repo.resolve()]
    assert records[0].agent == "claude"
    assert records[0].first_seen == 1000.0
    assert records[0].last_seen == 1000.0


def test_refresh_preserves_first_seen_and_advances_last_seen(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    activation.mark_active(repo, "claude", now=1000.0)
    activation.mark_active(repo, "codex", now=1500.0)
    records = activation.read_active(now=1500.0)

    assert len(records) == 1
    assert records[0].first_seen == 1000.0
    assert records[0].last_seen == 1500.0
    assert records[0].agent == "codex"


def test_marker_past_ttl_is_excluded_and_deleted(tmp_path: Path) -> None:
    """Expired markers are removed from disk, not merely ignored, so the
    registry cannot grow without bound."""
    repo = tmp_path / "repo"
    repo.mkdir()
    activation.mark_active(repo, "claude", now=1000.0)
    path = activation.marker_path(repo)
    assert path.is_file()

    records = activation.read_active(ttl_seconds=100.0, now=1200.0)

    assert records == []
    assert not path.exists(), "expired marker was not garbage-collected"


def test_marker_exactly_at_ttl_boundary_is_still_live(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    activation.mark_active(repo, "claude", now=1000.0)

    records = activation.read_active(ttl_seconds=100.0, now=1100.0)

    assert [r.root for r in records] == [repo.resolve()]


def test_future_last_seen_is_clamped_so_skew_cannot_pin_a_repo(tmp_path: Path) -> None:
    """A clock-skewed writer must not be able to hold a repo active forever."""
    repo = tmp_path / "repo"
    repo.mkdir()
    activation.mark_active(repo, "claude", now=9_000_000.0)

    records = activation.read_active(ttl_seconds=100.0, now=1000.0)

    assert [r.root for r in records] == [repo.resolve()]
    assert records[0].last_seen == 1000.0, "future timestamp was not clamped to now"


def test_corrupt_marker_is_dropped_and_does_not_raise(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    activation.mark_active(repo, "claude", now=1000.0)
    activation.marker_path(repo).write_text("{not json", encoding="utf-8")

    records = activation.read_active(now=1000.0)

    assert records == []
    assert not activation.marker_path(repo).exists()


def test_marker_for_deleted_repo_is_dropped(tmp_path: Path) -> None:
    repo = tmp_path / "gone"
    repo.mkdir()
    activation.mark_active(repo, "claude", now=1000.0)
    repo.rmdir()

    records = activation.read_active(now=1000.0)

    assert records == []


def test_distinct_repos_get_distinct_markers(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()

    activation.mark_active(a, "claude", now=1000.0)
    activation.mark_active(b, "codex", now=1000.0)

    assert {r.root for r in activation.read_active(now=1000.0)} == {a.resolve(), b.resolve()}


def test_mark_active_is_a_noop_for_daemon_children(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon runs `-m graphite build` INSIDE the repo it supervises. If that
    refreshed the marker, every activated repo would renew its own activation
    forever and never expire -- supervision would ratchet up and never release."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv(activation.ENV_DAEMON_CHILD, "1")

    result = activation.mark_active(repo, "cli", now=1000.0)

    assert result is None
    assert activation.read_active(now=1000.0) == []


def test_mark_active_never_raises_when_state_dir_is_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open: activation must never break an agent session."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    monkeypatch.setenv(activation.ENV_STATE_DIR, str(blocker))
    repo = tmp_path / "repo"
    repo.mkdir()

    assert activation.mark_active(repo, "claude", now=1000.0) is None
    assert activation.read_active(now=1000.0) == []


def test_marker_file_is_valid_json_with_expected_keys(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    activation.mark_active(repo, "claude", now=1000.0)

    payload = json.loads(activation.marker_path(repo).read_text(encoding="utf-8"))

    assert payload["root"] == str(repo.resolve())
    assert payload["agent"] == "claude"
    assert payload["first_seen"] == 1000.0
    assert payload["last_seen"] == 1000.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_activation.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphite.activation'`

- [ ] **Step 3: Write the implementation**

Create `src/graphite/activation.py`:

```python
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


def _read_marker(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _atomic_write(path: Path, payload: dict) -> None:
    """Temp-file + replace, so a concurrent reader never sees a partial marker."""
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_activation.py -q`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add src/graphite/activation.py tests/test_activation.py
git commit -m "feat(activation): registry of repos currently open in a coding agent"
```

---

### Task 2: Daemon supervises only active repositories

**Files:**
- Modify: `src/graphite/daemon.py:576-614` (discovery block inside `run_daemon`)
- Modify: `src/graphite/daemon.py:105-121` (`DaemonOptions`)
- Test: `tests/test_daemon_activation.py` (create)

**Interfaces:**
- Consumes: `activation.read_active`, `activation.ActivationRecord` (Task 1).
- Produces: `DaemonOptions.activation_ttl_seconds: float = 3600.0`. Status payload gains `"active_projects": list[str]` and **loses** `"unsupervised_nested_repos"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_daemon_activation.py`:

```python
"""The daemon supervises open repositories and leaves the rest alone.

The negative assertions are the point of this file: the mandate is not "build
the right repos", it is "do not touch the others".
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite import activation
from graphite.config import Config
from graphite.daemon import DaemonOptions, run_daemon


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(activation.ENV_STATE_DIR, str(tmp_path / "state"))
    monkeypatch.delenv(activation.ENV_DAEMON_CHILD, raising=False)


def _repo(base: Path, name: str) -> Path:
    root = base / name
    (root / ".git").mkdir(parents=True)
    (root / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    return root


def test_only_the_active_repo_is_built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / "projects"
    base.mkdir()
    opened = _repo(base, "opened")
    closed = _repo(base, "closed")
    activation.mark_active(opened, "claude")

    built: list[Path] = []
    monkeypatch.setattr(
        "graphite.daemon.build_project",
        lambda root, cfg, timeout: built.append(root) or _ok(),
    )

    run_daemon(base, Config(), DaemonOptions(once=True, max_builds_per_cycle=5))

    assert built == [opened.resolve()]
    assert closed.resolve() not in built


def test_inactive_repo_is_never_snapshotted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not building is not enough -- an unopened repo must not be scanned either,
    because scanning every repo every cycle is the cost being removed."""
    base = tmp_path / "projects"
    base.mkdir()
    opened = _repo(base, "opened")
    _repo(base, "closed")
    activation.mark_active(opened, "claude")

    snapshotted: list[Path] = []
    real_snapshot = __import__("graphite.daemon", fromlist=["snapshot"]).snapshot

    def _spy(root, cfg):
        snapshotted.append(Path(root).resolve())
        return real_snapshot(root, cfg)

    monkeypatch.setattr("graphite.daemon.snapshot", _spy)
    monkeypatch.setattr("graphite.daemon.build_project", lambda root, cfg, timeout: _ok())

    run_daemon(base, Config(), DaemonOptions(once=True, max_builds_per_cycle=5))

    assert set(snapshotted) <= {opened.resolve()}


def test_repo_outside_the_base_path_is_supervised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Supervision follows markers, not a base folder."""
    base = tmp_path / "projects"
    base.mkdir()
    outside = _repo(tmp_path / "elsewhere", "faraway")
    activation.mark_active(outside, "codex")

    built: list[Path] = []
    monkeypatch.setattr(
        "graphite.daemon.build_project",
        lambda root, cfg, timeout: built.append(root) or _ok(),
    )

    run_daemon(base, Config(), DaemonOptions(once=True, max_builds_per_cycle=5))

    assert built == [outside.resolve()]


def test_status_reports_active_projects_and_not_nested_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "projects"
    base.mkdir()
    opened = _repo(base, "opened")
    activation.mark_active(opened, "claude")
    monkeypatch.setattr("graphite.daemon.build_project", lambda root, cfg, timeout: _ok())

    status = run_daemon(base, Config(), DaemonOptions(once=True))

    assert str(opened.resolve()) in status["active_projects"]
    assert "unsupervised_nested_repos" not in status


def _ok():
    from graphite.daemon import BuildResult

    return BuildResult(success=True, seconds=0.01, error=None, stdout="", stderr="")
```

> **Note for the implementer:** `BuildResult`'s exact field names may differ. Open `src/graphite/daemon.py`, find the `BuildResult` definition and the `build_project` return, and adjust `_ok()` to match before running. Do not change production code to fit the helper.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_daemon_activation.py -q`
Expected: FAIL — the daemon still discovers all repos, so `built` contains both, and `status` has no `active_projects` key.

- [ ] **Step 3: Add the TTL option**

In `src/graphite/daemon.py`, inside `class DaemonOptions` (after `state_dir: Path | None = None`):

```python
    activation_ttl_seconds: float = 3600.0
```

And in `DaemonOptions.validate`, after the existing interval checks:

```python
        if self.activation_ttl_seconds <= 0:
            raise ValueError("daemon activation TTL must be greater than zero")
```

- [ ] **Step 4: Replace discovery with activation**

In `run_daemon`, replace the discovery block (currently `projects = discover_projects(...)` through `logger.event("project_discovered", ...)` and the nested-repo loop) with:

```python
        if cycle == 0 or now - last_discovery >= options.discover_interval_seconds:
            # Supervision follows activation markers, not a filesystem walk: a
            # repository nobody has open is never snapshotted and never built.
            records = activation.read_active(ttl_seconds=options.activation_ttl_seconds)
            projects = [record.root for record in records]
            discovered = set(projects)
            for project in projects:
                if project not in states:
                    try:
                        project_cfg = daemon_config_for_project(cfg, project, options)
                        snap = snapshot(project, project_cfg)
                    except Exception as exc:
                        logger.event("project_snapshot_failed", project=str(project), error=str(exc))
                        continue
                    states[project] = ProjectRuntime(
                        root=project,
                        snapshot=snap,
                        needs_initial_build=options.build_now,
                    )
                    logger.event("project_activated", project=str(project))
                else:
                    states[project].last_seen_at = utc_now()
            for project in list(states):
                if project not in discovered:
                    logger.event("project_deactivated", project=str(project))
                    del states[project]
            last_discovery = now
```

Add the import at the top of `daemon.py`:

```python
from . import activation
```

Delete the now-unused `unsupervised_nested` variable (declared near `states: dict[Path, ProjectRuntime] = {}`) and every reference to it, including its entry in `_write_status`.

- [ ] **Step 5: Surface the active set in status**

In `_write_status`, replace the `"unsupervised_nested_repos"` entry with:

```python
        "active_projects": sorted(str(root) for root in states),
```

- [ ] **Step 6: Run the new test and the existing daemon tests**

Run: `python -m pytest tests/test_daemon_activation.py -q`
Expected: PASS

Run: `python -m pytest tests/ -k daemon -q`

The affected tests were identified from the graph (`graphite query "callers discover_projects"` and `"callers nested_git_repos"`, both `decision_grade`), not by guesswork:

| test | action |
|---|---|
| `tests/test_daemon.py::test_nested_git_repo_is_reported_as_unsupervised` | **Delete.** Asserts the #6 warning this task removes. |
| `tests/test_daemon.py::test_plain_workspace_is_not_reported_as_a_nested_repo` | **Delete.** Same contract, negative side. |
| `tests/test_daemon.py::test_discover_projects_honors_graphite_ignore_marker` | **Keep unchanged.** Tests `discover_projects` directly. |
| `tests/test_daemon.py::test_discover_projects_stops_at_project_roots_and_skips_heavy_tool_directories` | **Keep unchanged.** Same. |
| `tests/test_monorepo.py::test_discovery_does_not_descend_into_projects` | **Keep unchanged.** Same. |

**Do not delete `discover_projects` or `nested_git_repos` themselves.** Three tests still exercise `discover_projects` directly, and it remains a legitimate utility (`graphite daemon-health` and future tooling may want it). This task removes the daemon's *use* of them, not the functions.

Any other daemon test that fails because a repo is no longer supervised without a marker is asserting the old contract — update it to mark the repo active first. Do **not** weaken a test that is catching a real regression.

- [ ] **Step 7: Run the full suite**

```bash
python -m pytest -q > suite.txt 2>&1
echo "exit: $?"
tail -5 suite.txt
```

Expected: exit 0. (Redirect and read `$?` directly — piping to `tail` reports the pipe's status, which has made failing runs look green.)

- [ ] **Step 8: Commit**

```bash
git add src/graphite/daemon.py tests/
git commit -m "feat(daemon): supervise only repos open in a coding agent

Supervised set comes from activation markers instead of a filesystem walk, so
an unopened repo is never snapshotted and never built. Supervision now follows
markers rather than a base path, so repos outside the base participate too --
which also makes the nested-repo blindness (#6) structurally impossible."
```

---

### Task 3: CLI backstop and daemon-child suppression

**Files:**
- Modify: `src/graphite/cli.py` (main dispatch; `_build_command` in `daemon.py`)
- Test: `tests/test_activation_backstop.py` (create)

**Interfaces:**
- Consumes: `activation.mark_active`, `activation.ENV_DAEMON_CHILD` (Task 1).
- Produces: no new public names. Behavioural contract: an interactive `graphite` invocation inside a repo marks it active; a daemon-spawned build does not.

- [ ] **Step 1: Write the failing test**

Create `tests/test_activation_backstop.py`:

```python
"""Any agent that uses graphite at all activates its repo -- that is what covers
agents graphite cannot hook (Codex, Gemini). The daemon's own build children are
excluded, or activation would renew itself forever."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite import activation
from graphite.config import Config
from graphite.daemon import _build_command


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(activation.ENV_STATE_DIR, str(tmp_path / "state"))
    monkeypatch.delenv(activation.ENV_DAEMON_CHILD, raising=False)


def test_daemon_build_children_run_with_the_suppression_flag(tmp_path: Path) -> None:
    """_build_command(cfg, root) -> (argv, env). Verified signature, not guessed."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _argv, env = _build_command(Config(), repo)

    assert env.get(activation.ENV_DAEMON_CHILD) == "1"


def test_suppression_flag_does_not_disturb_provider_credential_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_build_command` builds the child env by FILTERING provider variables out
    (daemon.py:337-341), guarded by
    tests/test_graph_provider_isolation.py::test_daemon_child_build_has_no_provider_argv_or_environment.

    Adding the activation flag must be one extra key on that filtered env. It
    must NOT become `dict(os.environ)` -- that would copy ANTHROPIC_API_KEY,
    OPENAI_API_KEY and friends into every daemon build child and silently undo
    the isolation contract.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-propagate")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-propagate")
    repo = tmp_path / "repo"
    repo.mkdir()

    _argv, env = _build_command(Config(), repo)

    assert env.get(activation.ENV_DAEMON_CHILD) == "1"
    assert "must-not-propagate" not in " ".join(env.values())


def test_a_daemon_child_cannot_renew_activation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ratchet test: activate, then let a child 'build', then cross the TTL
    with no agent activity. The repo must fall out of supervision."""
    repo = tmp_path / "repo"
    repo.mkdir()
    activation.mark_active(repo, "claude", now=1000.0)

    monkeypatch.setenv(activation.ENV_DAEMON_CHILD, "1")
    activation.mark_active(repo, "cli", now=1500.0)   # the daemon's build child
    monkeypatch.delenv(activation.ENV_DAEMON_CHILD)

    assert activation.read_active(ttl_seconds=100.0, now=2000.0) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_activation_backstop.py -q`
Expected: FAIL — `_build_command` does not yet return an environment carrying the flag.

- [ ] **Step 3: Set the suppression flag on daemon children**

`_build_command` (`daemon.py:315`) already returns `(cmd, env)`, and `run_graphite_build` (`daemon.py:348`) already passes that env to the child. **Do not restructure either.** The change is one line, added immediately before `return cmd, env` (i.e. after the PYTHONPATH block at `daemon.py:342-344`):

```python
    env[activation.ENV_DAEMON_CHILD] = "1"
```

Add `from . import activation` to the imports if Task 2 has not already.

> **Do not** replace the env-construction loop with `dict(os.environ)`. Lines 337-341 build the child environment by *filtering out* every variable matching `_PROVIDER_ENV_PREFIXES`, so provider credentials never reach a daemon build child. Copying the whole environment would undo that. If `test_daemon_child_build_has_no_provider_argv_or_environment` fails after your change, the change is wrong — do not edit that test.

- [ ] **Step 4: Add the CLI backstop**

In `src/graphite/cli.py`, in the top-level `main()` dispatch, immediately after the parsed args are available and before the subcommand runs:

```python
    # Universal activation backstop: an agent that uses graphite at all marks
    # its repo active, which covers agents graphite cannot hook. Daemon build
    # children are excluded inside mark_active via GRAPHITE_DAEMON_CHILD.
    if getattr(args, "cmd", None) not in {"daemon", "agent-hook"}:
        from . import activation

        activation.mark_active(Path.cwd(), "cli")
```

> `agent-hook` is excluded because Task 4 marks activation there explicitly with the real agent name; `daemon` is excluded because the supervisor is not an editing session.
> Check the attribute name the parser uses for the subcommand (`args.cmd`, `args.command`, or similar) and use the real one.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_activation_backstop.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/graphite/cli.py src/graphite/daemon.py tests/test_activation_backstop.py
git commit -m "feat(activation): CLI backstop, with daemon build children suppressed

Covers agents graphite cannot hook. Daemon children must not refresh the marker
of the repo they are building, or activation would ratchet and never release."
```

---

### Task 4: Agent hooks write activation markers

**Files:**
- Modify: `src/graphite/agent_hooks.py:59-79` (`handle_session_start`), and `handle_stop` (~line 196)
- Test: `tests/test_agent_hook_activation.py` (create)

**Interfaces:**
- Consumes: `activation.mark_active` (Task 1).
- Produces: no new public names. Contract: `handle_session_start` and `handle_stop` mark their payload's repo active with agent `"claude"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_hook_activation.py`:

```python
"""Opening a repo in Claude Code is what puts it under supervision."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite import activation
from graphite.agent_hooks import handle_session_start


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(activation.ENV_STATE_DIR, str(tmp_path / "state"))
    monkeypatch.delenv(activation.ENV_DAEMON_CHILD, raising=False)


def test_session_start_marks_the_repo_active(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    handle_session_start({"cwd": str(repo)})

    assert [r.root for r in activation.read_active()] == [repo.resolve()]
    assert activation.read_active()[0].agent == "claude"


def test_session_start_still_returns_context_when_activation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open contract: a broken registry must not cost the agent its
    session context."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "graphite.agent_hooks.mark_active",
        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )

    out = handle_session_start({"cwd": str(repo)})

    assert out is not None
    assert "graphite-first" in out["hookSpecificOutput"]["additionalContext"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_hook_activation.py -q`
Expected: FAIL — no marker is written; second test fails on the missing `mark_active` attribute.

- [ ] **Step 3: Mark active in the hooks**

In `src/graphite/agent_hooks.py`, add to the imports:

```python
from .activation import mark_active
```

In `handle_session_start`, as the first statement inside the existing `try:`:

```python
        root = _payload_root(payload)
        try:
            mark_active(root, "claude")
        except Exception:
            pass  # activation is bookkeeping; never cost the agent its context
```

(then keep the existing body, which already begins by computing `root` — remove the now-duplicated assignment).

In `handle_stop`, add the same guarded `mark_active(_payload_root(payload), "claude")` call as its first statement inside the existing `try:`. `Stop` fires once per assistant turn, which makes it the heartbeat that keeps a working session inside the TTL.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_agent_hook_activation.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/graphite/agent_hooks.py tests/test_agent_hook_activation.py
git commit -m "feat(hooks): SessionStart and Stop mark the repo active

Stop fires per turn, so it doubles as the heartbeat that keeps a working
session inside the activation TTL."
```

---

### Task 5: Doctrine rewrite, DOC_VERSION bump, VS Code activation task

**Files:**
- Modify: `src/graphite/init.py:17` (`DOC_VERSION`), `:57` (the skip sentence), template body
- Test: `tests/test_init_activation_doctrine.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `DOC_VERSION = 10`; init writes/merges `.vscode/tasks.json`; init defaults to `strict` hooks; init gains `--no-build`.

**Why strict becomes the default and init stops rebuilding.** Both changes exist so this decision never requires visiting every repo again. A per-repo setting that must be applied by sweeping decays the moment a new repo appears — and the sweep itself violates the operator mandate, because `init` rebuilds. Changing the default means a repo inherits strict when it is next opened and re-inited, one repo at a time.

- [ ] **Step 1: Write the failing test**

Create `tests/test_init_activation_doctrine.py`:

```python
"""Doctrine must never make freshness a precondition for querying the graph.

Operator-reported: agents declined to use the graph, citing that graphite was
supervising the repo. The shipped sentence put 'skip' beside 'a daemon keeps
this repo fresh' and contradicted PRE_TOOL_REMINDER. See #22.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from graphite import init as init_mod


def test_no_shipped_instruction_pairs_skip_with_supervision() -> None:
    """The rule is mechanical so it cannot be re-introduced by a well-meaning
    rewrite: 'skip' never shares an instruction with daemon/supervising/fresh."""
    text = Path(init_mod.__file__).read_text(encoding="utf-8")
    for line in text.splitlines():
        lowered = line.lower()
        if "skip" not in lowered:
            continue
        assert not re.search(r"daemon|supervis|fresh", lowered), (
            f"instruction offers a freshness-based excuse to skip: {line.strip()!r}"
        )


def test_doc_version_is_10() -> None:
    assert init_mod.DOC_VERSION == 10


def test_init_defaults_to_strict_hooks(tmp_path: Path) -> None:
    """A setting that has to be applied by sweeping every repo decays the moment
    a new repo appears -- and the sweep itself rebuilds repos nobody opened."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    init_mod.init_repo(repo)

    settings = (repo / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert "strict" in settings


def test_no_build_leaves_an_existing_graph_untouched(tmp_path: Path) -> None:
    """Re-initing for a DOC_VERSION bump must be a doc-only operation, so a repo
    that is not open is never rebuilt (operator mandate)."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    init_mod.init_repo(repo)
    graph = repo / "graph-out" / "graph.json"
    before = graph.stat().st_mtime_ns

    init_mod.init_repo(repo, build=False)

    assert graph.stat().st_mtime_ns == before, "--no-build rebuilt the graph anyway"
    assert (repo / "GRAPHITE.md").is_file()


def test_vscode_task_activates_on_folder_open(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    init_mod.init_repo(repo)

    tasks = json.loads((repo / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    graphite_tasks = [t for t in tasks["tasks"] if "graphite" in json.dumps(t).lower()]
    assert graphite_tasks, "no graphite activation task written"
    assert any(t.get("runOptions", {}).get("runOn") == "folderOpen" for t in graphite_tasks)


def test_existing_vscode_tasks_are_preserved(tmp_path: Path) -> None:
    """#13's lesson: never destroy hand-written content."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".vscode").mkdir()
    (repo / ".vscode" / "tasks.json").write_text(
        json.dumps({"version": "2.0.0", "tasks": [{"label": "my-precious-build"}]}),
        encoding="utf-8",
    )

    init_mod.init_repo(repo)

    tasks = json.loads((repo / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    labels = [t.get("label") for t in tasks["tasks"]]
    assert "my-precious-build" in labels
```

> **Note for the implementer:** `init_repo`'s real name and signature may differ. Open `src/graphite/init.py`, find the public entry point `cmd_init` calls, and use it. Adjust the fixture setup if it requires extra arguments.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_init_activation_doctrine.py -q`
Expected: FAIL — `DOC_VERSION` is 9, the skip sentence still pairs with "daemon", no `.vscode/tasks.json` is written.

- [ ] **Step 3: Rewrite the doctrine text**

In `src/graphite/init.py`, replace line 57:

```
1. Run `python -m graphite build .` (skip if a Graphite daemon/watcher keeps this repo fresh; verify with `python -m graphite check .`)
```

with:

```
1. Run `python -m graphite build .` when `python -m graphite check .` reports the graph stale. Otherwise the graph refreshes when you open this repo in a coding agent.
```

Then add, in the template's graph-first section:

```
Always query the graph for relationship questions. It grades its own answers, so
trust `answer.grade` rather than guessing whether the graph is current. If an
answer comes back `inconclusive` or insufficient, fall back to search and say
that you did.
```

- [ ] **Step 4: Bump DOC_VERSION**

`src/graphite/init.py:17`:

```python
DOC_VERSION = 10
```

- [ ] **Step 5: Write the VS Code activation task**

Add to `init.py` a helper that merges rather than overwrites, and call it from the init entry point:

```python
VSCODE_ACTIVATION_LABEL = "graphite: activate repo"


def _write_vscode_activation_task(root: Path) -> dict[str, Any]:
    """Register this repo as active when the folder is opened.

    VS Code, Cursor and Antigravity all honour `runOn: folderOpen`. Existing
    tasks are preserved -- destroying hand-written config is the #13 mistake.
    """
    path = root / ".vscode" / "tasks.json"
    document: dict[str, Any] = {"version": "2.0.0", "tasks": []}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                document = loaded
                document.setdefault("version", "2.0.0")
                document.setdefault("tasks", [])
        except Exception:
            return {"action": "skipped", "path": str(path), "reason": "unparseable"}

    tasks = [t for t in document["tasks"] if t.get("label") != VSCODE_ACTIVATION_LABEL]
    tasks.append(
        {
            "label": VSCODE_ACTIVATION_LABEL,
            "type": "shell",
            "command": "python -m graphite activate .",
            "presentation": {"reveal": "never", "panel": "dedicated"},
            "runOptions": {"runOn": "folderOpen"},
        }
    )
    document["tasks"] = tasks
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return {"action": "written", "path": str(path)}
```

- [ ] **Step 6: Add the `activate` subcommand**

The VS Code task needs a command whose only job is to mark activation. In `src/graphite/cli.py`:

```python
def cmd_activate(args: argparse.Namespace) -> int:
    from . import activation

    activation.mark_active(Path(args.path).resolve(), args.agent)
    return 0
```

and register it beside the other subparsers:

```python
    p_activate = sub.add_parser("activate", help="Mark a repository as open in a coding agent")
    p_activate.add_argument("path", nargs="?", default=".")
    p_activate.add_argument("--agent", default="editor")
    p_activate.set_defaults(func=cmd_activate)
```

Add `"activate"` to `_INFERENCE_FREE_EXTRA_COMMANDS` (`cli.py:133`) — it reads no model and must stay in the inference-free set.

- [ ] **Step 6b: Make strict the default and add `--no-build`**

In `src/graphite/cli.py:793`, the mode currently defaults to `remind`:

```python
    agent_hooks_mode = "strict" if args.strict else ("remind" if args.remind else None)
```

Invert it so strict is what a repo gets unless asked otherwise:

```python
    # Strict by default: a setting that must be applied by sweeping every repo
    # decays as soon as a new repo appears. Strict denials are health-gated in
    # code -- they fire only on a proven-healthy graph and re-arm automatically
    # -- so this cannot trap an agent behind a bad graph.
    agent_hooks_mode = "remind" if args.remind else "strict"
```

Keep `--strict` accepted (now a no-op that states intent) and keep `--remind` as the opt-out.

Then add the doc-only path. Register the flag beside the other `init` arguments:

```python
    p_init.add_argument("--no-build", action="store_true", help="Write docs and hooks without building the graph")
```

and thread it to the init entry point as `build=not args.no_build`, defaulting to `True`. The entry point must skip both the build and the validation step when `build=False`, and report `build: skipped` so a rollout can be audited.

> This exists so re-initing five consumers for a `DOC_VERSION` bump does not rebuild five repos nobody has open.

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_init_activation_doctrine.py -q`
Expected: PASS

Run: `python -m pytest tests/ -k "init or template or doc_version" -q`
Expected: PASS. `test_template_change_requires_doc_version_bump` should be satisfied by Step 4; if it still fails, the template hash it pins needs regenerating per its own docstring.

- [ ] **Step 8: Commit**

```bash
git add src/graphite/init.py src/graphite/cli.py tests/test_init_activation_doctrine.py
git commit -m "fix(init): stop licensing agents to skip the graph on freshness grounds

'skip if a Graphite daemon/watcher keeps this repo fresh' contradicted
PRE_TOOL_REMINDER and was being cited by agents as a reason not to use the
graph at all. Rebuilding is now conditioned on an observation (check says
stale), never on an assumption about who maintains the graph.

Adds a VS Code folderOpen activation task, merged not overwritten (#13).

Closes #22"
```

---

### Task 6: daemon-health reports the active set

**Files:**
- Modify: `src/graphite/daemon_health.py` (remove `project_nested_repo_unsupervised`; add active-set reporting)
- Test: `tests/test_daemon_health_activation.py` (create)

**Interfaces:**
- Consumes: the `active_projects` status key (Task 2).
- Produces: health payload gains `"active_projects"`; warning code `project_nested_repo_unsupervised` is removed.

- [ ] **Step 1: Write the failing test**

Create `tests/test_daemon_health_activation.py`:

```python
"""Health reports what is supervised now. The old nested-repo warning answered a
question activation deletes: an unopened repo SHOULD be unsupervised."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.daemon_health import evaluate_daemon_health


def _status(tmp_path: Path, payload: dict) -> Path:
    state = tmp_path / ".graphite-daemon"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "status.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_active_projects_are_reported(tmp_path: Path) -> None:
    _status(tmp_path, {"updated_at": "2026-07-28T05:00:00+00:00",
                       "projects": [], "active_projects": ["F:\\Projects\\aramid"]})

    report = evaluate_daemon_health(tmp_path)

    assert report["active_projects"] == ["F:\\Projects\\aramid"]


def test_nested_repo_warning_is_gone(tmp_path: Path) -> None:
    _status(tmp_path, {"updated_at": "2026-07-28T05:00:00+00:00", "projects": [],
                       "active_projects": [], "unsupervised_nested_repos": ["X"]})

    report = evaluate_daemon_health(tmp_path)

    codes = {w["code"] for w in report.get("warnings", [])}
    assert "project_nested_repo_unsupervised" not in codes
```

> **Note for the implementer:** `evaluate_daemon_health`'s real name/signature may differ — check `daemon_health.py` and how `cmd_daemon_health` calls it, then adjust. `_normalize_status` is a **strict whitelist**: a new status field is silently dropped unless it is normalized explicitly. `active_projects` will not appear in the report until you add it there.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_daemon_health_activation.py -q`
Expected: FAIL — `active_projects` missing (dropped by `_normalize_status`); nested warning still emitted.

- [ ] **Step 3: Normalize the new field and drop the warning**

In `daemon_health.py`: add `active_projects` to `_normalize_status`'s whitelist, surface it on the report, and delete the `project_nested_repo_unsupervised` warning branch together with its now-unused status field.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_daemon_health_activation.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

```bash
python -m pytest -q > suite.txt 2>&1
echo "exit: $?"
tail -5 suite.txt
```

Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/graphite/daemon_health.py tests/test_daemon_health_activation.py
git commit -m "feat(daemon-health): report the active set; drop nested-repo warning

An unopened repo being unsupervised is now correct behaviour, so the warning
answered a question this design deletes. Closes #6."
```

---

## Post-implementation (operator steps, not code)

**Operator mandate — binding on this rollout.** Graphite may act only on the repo currently open in a coding agent. No step here may build, scan, or re-init a repo that is not open. An earlier draft of this section said "re-run `graphite init` on the five managed consumers" and "restart the daemon once" — **both were violations**, because `init` rebuilds and a daemon restart force-rebuilds all 32 projects. They are replaced.

- [ ] **Stop the old daemon before anything else.** Until Task 2 ships, the running daemon derives its supervised set from a filesystem walk and rebuilds repos nobody opened — it violates the mandate on every cycle. Operator action (the classifier blocks the agent from killing it):
  ```
  ! taskkill //F //PID <daemon-pid>
  ```
  Do **not** relaunch it from the Startup launcher until Task 2 is merged; a restart force-rebuilds everything.

- [ ] **Do not sweep the consumers.** Each managed consumer picks up the `version=10` docs and its rebuild **when it is next opened**, via `graphite init --no-build` from the activation path or a deliberate re-init while that repo is the one being worked on. Audit opportunistically with a marker survey — `init` exiting 0 does not prove a repo was updated (#13).

- [ ] **Confirm the mandate holds live.** With exactly one repo open, wait one cycle and check `graphite daemon-health --json`: `active_projects` must list that repo and nothing else. Compare against the ground truth — the live agent sessions. On 2026-07-28 that was 3 open repos (`graphite`, `aramid`, `pawscout-worker`) against 32 the daemon was tracking.

- [ ] **Update issues:** #6 closed by Task 6, #22 closed by Task 5, #18 re-scoped (rebuild-on-open covers the engine-change case — record what remains). #21 is untouched and stays open.

- [ ] **Re-install the daemon launcher — but only AFTER Task 2 merges.** The daemon was stopped and `daemon-uninstall-startup-windows` was run on 2026-07-28 so nothing could resurrect full supervision. Once Task 2 ships, the daemon is the component that builds active repos, so rebuild-on-open does nothing without it: markers get written and no one acts on them. Re-install with `graphite daemon-install-startup-windows F:\Projects` and confirm `active_projects` matches the repos actually open. **Ordering matters** — re-installing before Task 2 merges force-rebuilds all 32 projects.

- [ ] **Known coverage gap to record, not fix here.** Open repos are currently detectable for Claude Code (per-project session directories under `~/.claude/projects/`) but **not for Codex** — a Codex-opened repo is invisible until it invokes graphite and trips the Task 3 backstop. Worth its own issue after this lands.

## Self-review notes

- **Spec coverage:** registry §1 → Task 1; writers §2 → Tasks 3–5; daemon §3 → Task 2; rebuild-on-open §4 → Task 2 (`needs_initial_build` on activation) ; doctrine §5 → Task 5; error handling → Task 1 tests; testing §1–8 → Tasks 1–6, including the ratchet test (§8) in Task 3.
- **Type consistency:** `mark_active(root, agent, *, now)` and `read_active(*, ttl_seconds, now)` are used with those exact signatures in Tasks 2–5. `ENV_DAEMON_CHILD` is referenced by name in Tasks 1 and 3.
- **Known-uncertain identifiers** are flagged inline with implementer notes rather than guessed silently: `BuildResult` fields (Task 2), `init_repo` entry point (Task 5), `evaluate_daemon_health` signature (Task 6), the argparse subcommand attribute (Task 3).
