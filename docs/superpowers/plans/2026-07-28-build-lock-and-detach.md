# Build Lock + Detached Build Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give graphite a cross-process build lock and a `--detach` build flag, so that multiple builders on one repo coexist instead of racing.

**Architecture:** A lock file under the repo's cache dir, acquired by any builder via a context manager. The CLI acquires it around a build; the daemon acquires it before spawning its build child and tells that child the lock is already held via an environment variable, mirroring the existing `GRAPHITE_DAEMON_CHILD` convention. `--detach` re-spawns `graphite build` as a detached process and returns immediately, with all platform-specific spawning done in Python where tests can reach it.

**Tech Stack:** Python 3.14, pytest, stdlib only (`os`, `json`, `socket`, `subprocess`, `contextlib`).

**Why this is Plan A of two:** this is the foundation layer of `docs/superpowers/specs/2026-07-28-autonomous-repo-hooks-design.md`. Plan B (git hook shims, `init` migration, `core.hooksPath`, `init.templateDir`, `doctor` probe) depends on both deliverables here. This plan is independently valuable: graphite has **no cross-process locking today**, so a manual `graphite build` run while the daemon is building the same repo already races.

## Global Constraints

- Lock file path: `<repo>/.cache/graphite/.build.lock` (i.e. `cfg.cache_dir / ".build.lock"`). Already gitignored in all consumer repos; unlike `graph-out/` it is not rewritten wholesale by builds.
- Lock TTL: **600.0 seconds** — 2× the daemon's `build_timeout_seconds` default of 240.0 (`daemon.py` `DaemonOptions`).
- **Never use `os.kill(pid, 0)` for liveness.** On Windows, Python's `os.kill` ignores signal 0 and calls `TerminateProcess` — it would kill the process being probed. Staleness is TTL-only.
- Lock acquisition must be atomic: `os.open(..., os.O_CREAT | os.O_EXCL | os.O_WRONLY)`.
- A builder that cannot acquire the lock exits **0**, not an error. A skipped build is a normal outcome, not a failure.
- Never break the existing suite: baseline is **2427 passed, 44 skipped**.
- Windows is the primary platform. Prefer `Path` operations; never assume POSIX-only APIs.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/graphite/buildlock.py` (create) | The lock primitive and its env-var escape hatch. Nothing else. |
| `tests/test_buildlock.py` (create) | Lock semantics: acquire, held, stale, garbage, release-on-exception. |
| `src/graphite/cli.py` (modify) | `cmd_build` acquires the lock; `--detach` flag registration and dispatch. |
| `src/graphite/detach.py` (create) | Platform-specific detached spawning, isolated so it is testable. |
| `tests/test_build_detach.py` (create) | Lock wiring in the CLI, and detached-spawn flags. |
| `src/graphite/daemon.py` (modify) | Acquire the lock before spawning; pass the escape hatch to the child. |
| `tests/test_daemon_build_lock.py` (create) | Daemon skips a locked project and does not mark it built. |

---

### Task 1: The build lock primitive

**Files:**
- Create: `src/graphite/buildlock.py`
- Test: `tests/test_buildlock.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `DEFAULT_TTL_SECONDS: float = 600.0`
  - `ENV_LOCK_HELD: str = "GRAPHITE_BUILD_LOCK_HELD"`
  - `lock_path(cache_dir: Path) -> Path`
  - `build_lock(cache_dir: Path, *, ttl_seconds: float = DEFAULT_TTL_SECONDS, clock: Callable[[], float] = time.time) -> ContextManager[bool]` — yields `True` if acquired, `False` if another live builder holds it. Releases on exit only when acquired.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_buildlock.py`:

```python
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

from graphite.buildlock import DEFAULT_TTL_SECONDS, build_lock, lock_path


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_buildlock.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'graphite.buildlock'`

- [ ] **Step 3: Write minimal implementation**

Create `src/graphite/buildlock.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_buildlock.py -q`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add src/graphite/buildlock.py tests/test_buildlock.py
git commit -m "feat(buildlock): cross-process build lock with TTL staleness"
```

---

### Task 2: Acquire the lock in `graphite build`

**Files:**
- Modify: `src/graphite/cli.py` (`cmd_build`, currently at `cli.py:523-526`)
- Test: `tests/test_build_detach.py`

**Interfaces:**
- Consumes: `graphite.buildlock.build_lock`, `graphite.buildlock.ENV_LOCK_HELD`.
- Produces: `cmd_build` returns 0 both when it builds and when it skips.

Current body for reference:

```python
def cmd_build(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args, canonical=True)
    _build_project(Path(args.path).resolve(), cfg)
    return 0
```

Note `cmd_report` delegates to `cmd_build`, so `report` inherits the lock.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_build_detach.py`:

```python
"""CLI build: lock acquisition and detached spawning."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite import buildlock


def _seed_repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    return tmp_path


def test_build_skips_when_another_builder_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from graphite.cli import main

    repo = _seed_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GRAPHITE_DAEMON_CHILD", "1")

    with buildlock.build_lock(repo / ".cache" / "graphite") as held:
        assert held is True
        assert main(["build", "."]) == 0
        out = capsys.readouterr().out

    assert "skipped" in out.lower(), f"a locked build must say so: {out!r}"
    assert not (repo / "graph-out" / "graph.json").exists(), "locked build produced a graph"


def test_build_proceeds_when_the_lock_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graphite.cli import main

    repo = _seed_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GRAPHITE_DAEMON_CHILD", "1")

    assert main(["build", "."]) == 0

    assert (repo / "graph-out" / "graph.json").exists()
    assert not buildlock.lock_path(repo / ".cache" / "graphite").exists()


def test_env_escape_hatch_bypasses_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child whose parent already holds the lock must not deadlock on it."""
    from graphite.cli import main

    repo = _seed_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GRAPHITE_DAEMON_CHILD", "1")
    monkeypatch.setenv(buildlock.ENV_LOCK_HELD, "1")

    with buildlock.build_lock(repo / ".cache" / "graphite") as held:
        assert held is True
        assert main(["build", "."]) == 0

    assert (repo / "graph-out" / "graph.json").exists(), "escape hatch did not build"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_build_detach.py -q`
Expected: `test_build_skips_when_another_builder_holds_the_lock` FAILS — the build runs anyway and `graph.json` exists.

- [ ] **Step 3: Write minimal implementation**

In `src/graphite/cli.py`, add to the imports near the other `from .` imports:

```python
from . import buildlock
```

Replace `cmd_build`:

```python
def cmd_build(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args, canonical=True)
    root = Path(args.path).resolve()

    # A parent that already holds the lock (the daemon) sets this for its child,
    # which would otherwise deadlock against its own parent.
    if os.environ.get(buildlock.ENV_LOCK_HELD):
        _build_project(root, cfg)
        return 0

    with buildlock.build_lock(cfg.cache_dir) as acquired:
        if not acquired:
            print("[graphite] build skipped: another build is already running for this repo")
            return 0
        _build_project(root, cfg)
    return 0
```

`os` is already imported in `cli.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_build_detach.py -q`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add src/graphite/cli.py tests/test_build_detach.py
git commit -m "feat(build): acquire the repo build lock, skip cleanly when held"
```

---

### Task 3: Daemon acquires the lock before spawning

**Files:**
- Modify: `src/graphite/daemon.py` (`run_graphite_build` at `daemon.py:361`, and `_build_command`'s env at `daemon.py:354`)
- Test: `tests/test_daemon_build_lock.py`

**Interfaces:**
- Consumes: `graphite.buildlock.build_lock`, `ENV_LOCK_HELD`.
- Produces: `run_graphite_build` returns `BuildResult(success=False, returncode=None, error="build_lock_held")` when the lock is held, without spawning.

**Naming trap — read before editing.** `build_project` is NOT a function. It is
the *injectable parameter* on `run_daemon` (`daemon.py:564`,
`build_project: BuildProject = run_graphite_build`), which tests replace with a
fake. The real implementation you are modifying is **`run_graphite_build`**.
Putting the lock in the parameter's call site (`daemon.py:695`) instead would
mean every test that injects a fake builder also acquires a real file lock.

- [ ] **Step 1: Write the failing test**

Create `tests/test_daemon_build_lock.py`:

```python
"""The daemon must not race another builder on the same repo."""
from __future__ import annotations

from pathlib import Path

from graphite import buildlock
from graphite.config import Config
from graphite.daemon import run_graphite_build


def test_daemon_build_is_refused_while_the_lock_is_held(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    cfg = Config(cache_dir=repo / ".cache" / "graphite", output_dir=repo / "graph-out")

    with buildlock.build_lock(cfg.cache_dir) as held:
        assert held is True
        result = run_graphite_build(repo, cfg, 30.0)

    assert result.success is False
    assert result.error == "build_lock_held"
    assert not (repo / "graph-out" / "graph.json").exists(), "daemon built despite the lock"


def test_daemon_build_runs_when_the_lock_is_free(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    cfg = Config(cache_dir=repo / ".cache" / "graphite", output_dir=repo / "graph-out")

    result = run_graphite_build(repo, cfg, 120.0)

    assert result.success is True, f"build failed: {result.error} {result.stderr}"
    assert (repo / "graph-out" / "graph.json").exists()
    assert not buildlock.lock_path(cfg.cache_dir).exists(), "daemon leaked the lock"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_daemon_build_lock.py -q`
Expected: `test_daemon_build_is_refused_while_the_lock_is_held` FAILS — `result.success is True` and `graph.json` exists.

- [ ] **Step 3: Write minimal implementation**

In `src/graphite/daemon.py`, add to imports:

```python
from . import buildlock
```

In `_build_command`, add the escape hatch to the child environment so the spawned `-m graphite build` does not contend with the lock its parent holds. Add it immediately after the existing `env[activation.ENV_DAEMON_CHILD] = "1"` line (`daemon.py:354`):

```python
env[buildlock.ENV_LOCK_HELD] = "1"
```

**Add a key to the existing filtered `env` dict — never rebuild `env` from `os.environ`.** The comment above that line spells out why: the filtering is what keeps provider credentials out of a daemon child, guarded by `test_daemon_child_build_has_no_provider_argv_or_environment`.

Wrap the spawn in `run_graphite_build`. Immediately inside the function, before the existing `cmd, env = _build_command(cfg, root)` line:

```python
    with buildlock.build_lock(cfg.cache_dir) as acquired:
        if not acquired:
            return BuildResult(
                success=False,
                returncode=None,
                duration_seconds=0.0,
                error="build_lock_held",
            )
        return _run_build_locked(root, cfg, timeout_seconds)
```

Move the existing body of `run_graphite_build` (from `start = time.time()` / `cmd, env = _build_command(...)` through its final `return`) into a new module-level helper `_run_build_locked(root: Path, cfg: Config, timeout_seconds: float) -> BuildResult` with that body unchanged, and have `run_graphite_build` call `_run_build_locked(root, cfg, timeout_seconds)` inside the `with` block. Keep the argument order `(root, cfg, timeout_seconds)` to match `run_graphite_build` and the `BuildProject` protocol.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_daemon_build_lock.py tests/test_daemon.py -q`
Expected: all pass. `test_daemon.py` is included because it exercises `build_project` via a fake and must not regress.

- [ ] **Step 5: Commit**

```bash
git add src/graphite/daemon.py tests/test_daemon_build_lock.py
git commit -m "feat(daemon): acquire the build lock before spawning a build child"
```

---

### Task 4: `graphite build --detach`

**Files:**
- Create: `src/graphite/detach.py`
- Modify: `src/graphite/cli.py` (`cmd_build`, and the `build` subparser at `cli.py:2233`)
- Test: `tests/test_build_detach.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `spawn_detached(cmd: list[str], cwd: Path) -> int` returning the child PID.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_build_detach.py`:

```python
def test_spawn_detached_uses_platform_isolation_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detaching is done in Python, not the shell, so it is testable.

    Git hooks on Windows run under Git Bash, where backgrounding is unreliable.
    """
    import subprocess
    import sys

    from graphite import detach

    seen: dict[str, object] = {}

    class FakePopen:
        pid = 4321

        def __init__(self, cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs

    monkeypatch.setattr(detach.subprocess, "Popen", FakePopen)

    pid = detach.spawn_detached(["python", "-m", "graphite", "build", "."], Path("."))

    assert pid == 4321
    kwargs = seen["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    if sys.platform == "win32":
        assert kwargs["creationflags"] & subprocess.DETACHED_PROCESS
    else:
        assert kwargs["start_new_session"] is True


def test_detach_flag_spawns_a_child_without_detach_and_returns_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child must NOT carry --detach, or it would re-spawn forever."""
    from graphite import cli
    from graphite.cli import main

    repo = _seed_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GRAPHITE_DAEMON_CHILD", "1")

    seen: dict[str, object] = {}
    monkeypatch.setattr(cli, "spawn_detached", lambda cmd, cwd: seen.setdefault("cmd", cmd) or 999)

    assert main(["build", ".", "--detach"]) == 0

    cmd = seen["cmd"]
    assert "--detach" not in cmd, f"detached child would re-spawn forever: {cmd}"
    assert "build" in cmd
    assert not (repo / "graph-out" / "graph.json").exists(), "--detach built inline"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_build_detach.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphite.detach'`, and `main(["build", ".", "--detach"])` errors with `unrecognized arguments: --detach`.

- [ ] **Step 3: Write minimal implementation**

Create `src/graphite/detach.py`:

```python
"""Spawn a fully detached child process, portably.

This lives in Python rather than in the shell hook on purpose: git hooks on
Windows run under Git Bash, where backgrounding is unreliable and untestable.
The hook stays a trampoline; the platform logic lives here where the test suite
can reach it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def spawn_detached(cmd: list[str], cwd: Path) -> int:
    """Start `cmd` detached from this process and return its pid."""
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
    return proc.pid
```

In `src/graphite/cli.py`, add the import:

```python
from .detach import spawn_detached
```

Register the flag next to the `build` subparser at `cli.py:2233`:

```python
    p_build.add_argument(
        "--detach",
        action="store_true",
        help="Start the build as a detached background process and return immediately",
    )
```

Add the detach branch at the very top of `cmd_build`, before the lock logic:

```python
    if getattr(args, "detach", False):
        root = Path(args.path).resolve()
        # Deliberately omits --detach: the child must build, not re-spawn.
        pid = spawn_detached(
            [sys.executable, "-B", "-m", "graphite", "build", str(root)], root
        )
        print(f"[graphite] detached build started (pid {pid})")
        return 0
```

`sys` is already imported in `cli.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_build_detach.py -q`
Expected: `5 passed`

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q > /tmp/suite.txt 2>&1; echo "EXIT=$?"; tail -3 /tmp/suite.txt`

Redirect to a file and read `$?` directly — piping to `tail` reports the *pipe's* status and has made failing runs look like exit 0.

Expected: `EXIT=0`, and at least `2427 passed` (baseline) plus the new tests.

- [ ] **Step 6: Commit**

```bash
git add src/graphite/detach.py src/graphite/cli.py tests/test_build_detach.py
git commit -m "feat(build): --detach spawns a background build and returns immediately"
```

---

## Self-Review

**Spec coverage (foundation layer only):**

| Spec requirement | Task |
|---|---|
| Repo-level build lock, used by hook builds and the daemon | 1, 2, 3 |
| Lock at `.cache/graphite/.build.lock` | 1 (`lock_path`) |
| TTL 600s, no PID liveness check | 1 (`DEFAULT_TTL_SECONDS`, `_is_stale`) |
| Held → exit 0 silently | 2 |
| Daemon acquires the same lock | 3 |
| `build --detach`, platform logic in Python | 4 |
| Detached child must not re-spawn | 4 (asserted explicitly) |

Deferred to Plan B by design: `.githooks/` shims, `init` migration, `core.hooksPath`, `init.templateDir`, the `doctor` probe. Each depends on Tasks 1–4 landing first.

**Type consistency:** `build_lock(cache_dir, *, ttl_seconds, clock)` yields `bool` in Tasks 1, 2, 3. `lock_path(cache_dir) -> Path` used in Tasks 1, 3. `spawn_detached(cmd, cwd) -> int` defined in Task 4 and monkeypatched with the same two-arg signature in its test. `ENV_LOCK_HELD` set by the daemon (Task 3) and read by the CLI (Task 2).

**Placeholder scan:** no TBD/TODO; every code step carries real code; no "similar to Task N" references.

## Notes for the implementer

- `Config` field names used here (`cache_dir`, `output_dir`) are real — see `config.py:31`.
- `cmd_report` delegates to `cmd_build`, so `report` inherits both the lock and `--detach`. That is intended; no separate wiring.
- Tests that invoke `main(["build", ...])` set `GRAPHITE_DAEMON_CHILD=1` to stop the CLI activation backstop from writing the tmp repo into the machine's live activation registry. Omitting it pollutes the real daemon's supervised set — this has happened before with `graphite doctor` scratch dirs.
