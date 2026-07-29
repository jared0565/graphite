# Git-Hook Graph Autonomy (Plan B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A repo keeps its own graph fresh from git events alone — no agent open, no daemon required — while the daemon still accelerates freshness between commits.

**Architecture:** Committed `.githooks/` trampolines for graphite's three triggers, wired up by `core.hooksPath`, with `init.templateDir` covering fresh clones. Each trampoline calls a **fast-path Python entry** that acquires Plan A's build lock and spawns a detached build. Hooks graphite does not trigger on are **relocated byte-identically** and never touched again.

**Tech Stack:** Python 3.11+, `sh` (Git-for-Windows), git `core.hooksPath` / `init.templateDir`. Builds on Plan A (`buildlock.py`, `detach.py`, `build --detach`).

**Spec:** `docs/superpowers/specs/2026-07-28-autonomous-repo-hooks-design.md`

## Global Constraints

Every task inherits these. They are measured or hard-won, not preferences.

- **The hook must never import `graphite.cli`.** Measured on this machine, best of 3: bare python **79ms**, `import graphite` **86ms**, `import graphite.detach` **137ms**, detach+buildlock+os+pathlib **158ms**, `import graphite.cli` **1281ms**. The CLI import alone is ~1.2s on *every commit*. `graphite/__init__.py` is nearly free (+7ms), so the constraint is specifically `cli.py`, not "avoid Python".
- **Chain with `if`/`fi`, never `[ -f … ] && { … }`.** As a script's final command with the chained file absent, the `&&` form exits **1** — measured — which would block every commit and push on a fresh clone. Both forms propagate real failures identically.
- **Render shim bytes LF-only via `Path.write_bytes`.** A text-mode Windows write reintroduces CRLF and Git-for-Windows' `sh` chokes on a bare CR.
- **Never invoke a bare `python` from a hook.** This machine exposes several interpreters to hook `sh`, including the WindowsApps store stub. Bake the absolute interpreter in `/c/...` form with a `command -v py` / `py -3` fallback.
- **Bake no chaining state into rendered bytes.** The shim always contains the chain-check; only the installer creates the `.local` sibling. This is what makes regeneration idempotent.
- **Marker:** `# >>> graphite managed >>>` / `# <<< graphite managed <<<`.
- **Relocate, never trampoline, a hook graphite does not trigger on.** Stamping graphite's marker onto `pre-commit`/`pre-push` makes aramid's `install()` refuse them (`0f24609`).
- **`core.hooksPath` is set LAST**, so no window exists where nothing fires. If it is *already* set (husky et al.), do not hijack — install into that directory.
- Graphite's triggers: **`post-commit`, `post-merge`, `post-rewrite`**. `post-checkout` is excluded (bisect cost).

## File Structure

| File | Responsibility |
|---|---|
| `src/graphite/hookshim.py` | **new** — pure byte rendering + interpreter resolution. No filesystem writes, no git. |
| `src/graphite/hook_entry.py` | **new** — the fast path a trampoline execs. Imports only `buildlock`/`detach`. |
| `src/graphite/hookinstall.py` | **new** — migration, relocation, `core.hooksPath`, uninstall. All filesystem/git effects. |
| `src/graphite/init.py` | modify — call `hookinstall` during `graphite init`. |
| `src/graphite/doctor.py` | modify — "configured but not enforced" probe. |
| `src/graphite/cli.py` | modify — register `--no-hooks` on `init`; template subcommand. |

Rendering is split from installation deliberately: rendering is pure and exhaustively testable, installation is effectful. aramid learned the same split the hard way.

**Blast radius** (from `graphite impact src/graphite/init.py`, decision_grade): `cli.py`, `daemon.py`, `agent_hooks.py`, `engine_identity.py`, `llm_probe.py`, `__init__.py`. Likely tests include `test_init.py`, `test_init_activation_doctrine.py`, `test_daemon.py`, `test_agent_hooks.py`. Run those on every task touching `init.py`.

---

### Task 1: Shim rendering

**Files:**
- Create: `src/graphite/hookshim.py`
- Test: `tests/test_hookshim.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces: `MARKER_START: str`, `MARKER_END: str`, `TRIGGERS: tuple[str, ...]`, `sh_interpreter_path(p: Path) -> str`, `render_trigger_shim(hook: str, interpreter: Path) -> bytes`.

- [ ] **Step 1: Write the failing tests**

```python
from pathlib import Path
from graphite.hookshim import (MARKER_START, TRIGGERS, render_trigger_shim,
                               sh_interpreter_path)

def test_triggers_are_the_three_agreed_hooks():
    assert TRIGGERS == ("post-commit", "post-merge", "post-rewrite")

def test_rendered_bytes_are_lf_only():
    out = render_trigger_shim("post-commit", Path("C:/Python314/python.exe"))
    assert b"\r" not in out

def test_chain_check_uses_if_fi_not_and_shortcircuit():
    # `[ -f x ] && { ...; }` as a final command exits 1 when x is absent,
    # which would block every commit. Must be `if ... fi`.
    out = render_trigger_shim("post-commit", Path("C:/Python314/python.exe")).decode()
    assert "if [ -f" in out
    assert "] && {" not in out

def test_never_invokes_a_bare_python():
    out = render_trigger_shim("post-commit", Path("C:/Python314/python.exe")).decode()
    for line in out.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("python ")
        assert not stripped.startswith('"python"')

def test_shim_never_references_the_cli_module():
    out = render_trigger_shim("post-commit", Path("C:/Python314/python.exe")).decode()
    assert "graphite.cli" not in out
    assert "graphite.hook_entry" in out

def test_marker_present_and_regeneration_is_byte_stable():
    p = Path("C:/Python314/python.exe")
    a = render_trigger_shim("post-merge", p)
    assert MARKER_START.encode() in a
    assert a == render_trigger_shim("post-merge", p)

def test_windows_interpreter_becomes_sh_style_path():
    assert sh_interpreter_path(Path(r"C:\Python314\python.exe")) == "/c/Python314/python.exe"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_hookshim.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphite.hookshim'`

- [ ] **Step 3: Implement**

```python
"""Pure rendering of graphite's git-hook trampolines.

Deliberately has NO filesystem or git side effects -- installation lives in
`hookinstall`. Rendering is where the Windows correctness rules live, and they
are only cheaply testable while this stays pure.
"""
from pathlib import Path

MARKER_START = "# >>> graphite managed >>>"
MARKER_END = "# <<< graphite managed <<<"

# post-checkout is excluded on purpose: branch switching is frequent and
# transient, and hooking it makes `git bisect` expensive.
TRIGGERS: tuple[str, ...] = ("post-commit", "post-merge", "post-rewrite")

CHAINED_SUFFIX = ".local"


def sh_interpreter_path(interpreter: Path) -> str:
    """`C:\\Python314\\python.exe` -> `/c/Python314/python.exe`.

    Git-for-Windows' `sh` cannot exec a drive-letter path.
    """
    p = interpreter.resolve()
    drive = p.drive.rstrip(":").lower()
    rest = p.as_posix()[len(p.drive):].lstrip("/")
    return f"/{drive}/{rest}" if drive else p.as_posix()


def render_trigger_shim(hook: str, interpreter: Path) -> bytes:
    """A trampoline for one of graphite's triggers.

    The chain-check is ALWAYS present regardless of whether a `.local` sibling
    exists -- baking that state into the bytes is what would break idempotent
    regeneration.

    `if`/`fi`, never `[ -f "$C" ] && { ...; }`: as a script's final command the
    latter exits 1 when `$C` is absent, blocking every commit on a fresh clone.
    All triggers here are `post-*`, where git ignores the exit code, so the
    chained hook's failure is swallowed with `|| true` -- but the form still
    matters because the shim ends with an explicit `exit 0`.
    """
    interp = sh_interpreter_path(interpreter)
    lines = [
        "#!/bin/sh",
        MARKER_START,
        'DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)',
        f'CHAINED="$DIR/{hook}{CHAINED_SUFFIX}"',
        'if [ -f "$CHAINED" ]; then',
        '    "$CHAINED" "$@" || true',
        "fi",
        f'INTERP="{interp}"',
        'if [ -x "$INTERP" ]; then',
        '    "$INTERP" -m graphite.hook_entry >/dev/null 2>&1 || true',
        "elif command -v py >/dev/null 2>&1; then",
        "    py -3 -m graphite.hook_entry >/dev/null 2>&1 || true",
        "fi",
        MARKER_END,
        "exit 0",
        "",
    ]
    return "\n".join(lines).encode("utf-8")
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_hookshim.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/graphite/hookshim.py tests/test_hookshim.py
git commit -m "feat(hookshim): render git-hook trampolines, LF-only and if/fi"
```

---

### Task 2: The fast-path hook entry

**Files:**
- Create: `src/graphite/hook_entry.py`
- Test: `tests/test_hook_entry.py`

**Interfaces:**
- Consumes: `graphite.buildlock.build_lock`, `graphite.detach.spawn_detached` (Plan A).
- Produces: `main(argv: list[str] | None = None) -> int`, module runnable as `python -m graphite.hook_entry`.

This task exists solely to satisfy the ~1.2s constraint. Its headline test is a **subprocess** assertion, because `sys.modules` inside the test process is already polluted by pytest importing the CLI elsewhere.

- [ ] **Step 1: Write the failing tests**

```python
import subprocess, sys, json, textwrap
from pathlib import Path

def _run(code: str, cwd: Path):
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)],
                          cwd=str(cwd), capture_output=True, text=True)

def test_hook_entry_does_not_import_the_cli(tmp_path):
    # The whole point of this module. Measured: importing graphite.cli costs
    # ~1.2s, versus ~80ms over bare python for detach+buildlock.
    r = _run("""
        import sys, json
        import graphite.hook_entry
        print(json.dumps({"cli": "graphite.cli" in sys.modules}))
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["cli"] is False

def test_running_it_outside_a_repo_is_a_silent_noop(tmp_path):
    r = _run("import graphite.hook_entry as h; raise SystemExit(h.main([]))", tmp_path)
    assert r.returncode == 0

def test_spawns_a_detached_build_in_a_graphite_repo(tmp_path, monkeypatch):
    calls = []
    import graphite.hook_entry as h
    monkeypatch.setattr(h, "spawn_detached", lambda cmd, cwd: calls.append((cmd, cwd)) or 4321)
    (tmp_path / "GRAPHITE.md").write_text("x", encoding="utf-8")
    assert h.main([str(tmp_path)]) == 0
    assert len(calls) == 1
    cmd, _ = calls[0]
    assert "--detach" not in cmd   # the child must build, not re-spawn

def test_skips_when_another_build_holds_the_lock(tmp_path, monkeypatch):
    import graphite.hook_entry as h
    from graphite import buildlock
    (tmp_path / "GRAPHITE.md").write_text("x", encoding="utf-8")
    calls = []
    monkeypatch.setattr(h, "spawn_detached", lambda cmd, cwd: calls.append(cmd) or 1)
    with buildlock.build_lock(tmp_path / ".cache") as acquired:
        assert acquired
        assert h.main([str(tmp_path)]) == 0
    assert calls == []   # contention is a clean skip, never an error
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_hook_entry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphite.hook_entry'`

- [ ] **Step 3: Implement**

Import **only** `buildlock` and `detach`. Do not add convenience imports; the
first `from .cli import …` silently costs every commit ~1.2s and the test above
is the only thing standing between the repo and that regression.

```python
"""The fast path a git-hook trampoline execs.

MUST NOT import `graphite.cli`. Measured, best of 3: bare python 79ms,
`import graphite` 86ms, detach+buildlock+os+pathlib 158ms, `import
graphite.cli` 1281ms. The CLI import would put ~1.2s on every commit purely to
spawn a background build. `tests/test_hook_entry.py` enforces this in a
subprocess.
"""
import os
import sys
from pathlib import Path

from .buildlock import build_lock
from .detach import spawn_detached


def _repo_root(start: Path) -> Path | None:
    for d in (start, *start.parents):
        if (d / "GRAPHITE.md").exists():
            return d
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    start = Path(args[0]).resolve() if args else Path.cwd()
    root = _repo_root(start)
    if root is None:
        return 0          # not an onboarded repo -- fail open, silently

    cache_dir = root / ".graphite-cache"
    with build_lock(cache_dir) as acquired:
        if not acquired:
            return 0      # another builder owns it; the daemon retries
        spawn_detached(
            [sys.executable, "-B", "-m", "graphite", "build", str(root)], root
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> **Naming trap:** `spawn_detached` is imported into this module's namespace so
> `monkeypatch.setattr(h, "spawn_detached", …)` works. Patching
> `graphite.detach.spawn_detached` instead would not intercept it.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_hook_entry.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Measure the real cost and record it**

Run: `python -X importtime -m graphite.hook_entry 2>&1 | Select-Object -Last 5`
Expected: total well under 300ms; `graphite.cli` absent from the listing.

- [ ] **Step 6: Commit**

```bash
git add src/graphite/hook_entry.py tests/test_hook_entry.py
git commit -m "feat(hook-entry): fast path that never imports the CLI"
```

---

### Task 3: Installation, migration, relocation

**Files:**
- Create: `src/graphite/hookinstall.py`
- Test: `tests/test_hookinstall.py`

**Interfaces:**
- Consumes: `hookshim.TRIGGERS`, `render_trigger_shim`, `MARKER_START`.
- Produces: `install_hooks(root: Path, interpreter: Path) -> list[str]`, `uninstall_hooks(root: Path) -> list[str]`, `hooks_dir(root: Path) -> Path`.

This is the task that carries the aramid interop contract. **Relocation, not trampolines**, for anything outside `TRIGGERS`.

- [ ] **Step 1: Write the failing tests**

```python
import subprocess
from pathlib import Path
import pytest
from graphite.hookinstall import install_hooks, uninstall_hooks, hooks_dir
from graphite.hookshim import MARKER_START, TRIGGERS

def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "GRAPHITE.md").write_text("x", encoding="utf-8")
    return tmp_path

def _write_hook(root: Path, name: str, body: str) -> Path:
    p = root / ".git" / "hooks" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body.encode())
    return p

def test_non_trigger_hook_is_relocated_byte_identically(tmp_path):
    root = _repo(tmp_path)
    body = "#!/bin/sh\n# >>> aramid managed >>>\nexit 0\n"
    original = _write_hook(root, "pre-push", body).read_bytes()
    install_hooks(root, Path(__import__("sys").executable))
    moved = root / ".githooks" / "pre-push"
    assert moved.read_bytes() == original          # unchanged
    assert MARKER_START.encode() not in moved.read_bytes()   # no graphite marker
    assert not (root / ".githooks" / "pre-push.local").exists()

def test_trigger_hook_is_chained_not_relocated(tmp_path):
    root = _repo(tmp_path)
    body = "#!/bin/sh\necho original\n"
    _write_hook(root, "post-commit", body)
    install_hooks(root, Path(__import__("sys").executable))
    assert (root / ".githooks" / "post-commit.local").read_bytes() == body.encode()
    assert MARKER_START.encode() in (root / ".githooks" / "post-commit").read_bytes()

def test_hooks_path_is_set_and_set_last(tmp_path):
    root = _repo(tmp_path)
    install_hooks(root, Path(__import__("sys").executable))
    got = subprocess.run(["git", "-C", str(root), "config", "--get", "core.hooksPath"],
                         capture_output=True, text=True).stdout.strip()
    assert got == ".githooks"
    for t in TRIGGERS:
        assert (root / ".githooks" / t).exists()   # existed before the config

def test_existing_hookspath_is_not_hijacked(tmp_path):
    root = _repo(tmp_path)
    subprocess.run(["git", "-C", str(root), "config", "core.hooksPath", ".husky"], check=True)
    install_hooks(root, Path(__import__("sys").executable))
    got = subprocess.run(["git", "-C", str(root), "config", "--get", "core.hooksPath"],
                         capture_output=True, text=True).stdout.strip()
    assert got == ".husky"
    assert (root / ".husky" / "post-commit").exists()

def test_install_is_idempotent(tmp_path):
    root = _repo(tmp_path)
    interp = Path(__import__("sys").executable)
    install_hooks(root, interp)
    first = (root / ".githooks" / "post-commit").read_bytes()
    install_hooks(root, interp)
    assert (root / ".githooks" / "post-commit").read_bytes() == first
    assert not (root / ".githooks" / "post-commit.local").exists()  # never self-chains

def test_uninstall_restores_the_chained_hook(tmp_path):
    root = _repo(tmp_path)
    body = "#!/bin/sh\necho original\n"
    _write_hook(root, "post-commit", body)
    interp = Path(__import__("sys").executable)
    install_hooks(root, interp)
    uninstall_hooks(root)
    assert (root / ".githooks" / "post-commit").read_bytes() == body.encode()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_hookinstall.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphite.hookinstall'`

- [ ] **Step 3: Implement**

Order inside `install_hooks` is load-bearing:

1. Resolve `hooks_dir` — honour an existing `core.hooksPath`, else `.githooks`.
2. For each non-sample `.git/hooks/*`: if in `TRIGGERS`, move to `<hook>.local`; **otherwise move to `<hook>` unchanged** (relocation), preserving the executable bit.
3. Write trampolines for `TRIGGERS`.
4. `git config core.hooksPath` **last** — and only if it was unset.

A hook already carrying `MARKER_START` is graphite's own: regenerate in place, never chain it to itself.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_hookinstall.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the blast radius**

Run: `python -m pytest tests/test_init.py tests/test_init_activation_doctrine.py tests/test_daemon.py tests/test_agent_hooks.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/graphite/hookinstall.py tests/test_hookinstall.py
git commit -m "feat(hookinstall): relocate non-trigger hooks, chain only our own"
```

---

### Task 4: Wire into `graphite init`

**Files:**
- Modify: `src/graphite/init.py`, `src/graphite/cli.py`
- Test: `tests/test_init_hooks.py`

**Interfaces:**
- Consumes: `hookinstall.install_hooks`.
- Produces: `graphite init` installs hooks by default; `--no-hooks` opts out.

- [ ] **Step 1: Write the failing tests**

```python
def test_init_installs_hooks_by_default(tmp_path): ...
def test_init_no_hooks_flag_skips_installation(tmp_path): ...
def test_init_is_idempotent_over_hooks(tmp_path): ...
def test_init_reports_what_it_relocated(tmp_path, capsys): ...
```

Each mirrors Task 3's fixtures. Write the bodies out in full — do not reference Task 3's tests by name; they run in a different module.

- [ ] **Step 2: Run to verify they fail** — `python -m pytest tests/test_init_hooks.py -v`
- [ ] **Step 3: Implement** — call `install_hooks` from `init`, register `--no-hooks` on the `init` subparser, print relocations.
- [ ] **Step 4: Run to verify they pass**
- [ ] **Step 5: Run the blast radius** — `python -m pytest tests/test_init.py tests/test_init_activation_doctrine.py -q`
- [ ] **Step 6: Commit** — `git commit -m "feat(init): install git hooks; --no-hooks opts out"`

---

### Task 5: `doctor` — configured but not enforced

**Files:**
- Modify: `src/graphite/doctor.py`
- Test: `tests/test_doctor_hooks.py`

**Interfaces:**
- Consumes: `hookinstall.hooks_dir`, `hookshim.MARKER_START`, `TRIGGERS`.
- Produces: a `hooks` probe in doctor's existing report structure.

Severity rule, taken from aramid's `probe_enforcement`: **a repo with no `GRAPHITE.md` is deliberately not onboarded and is NOT a finding.** Nagging every un-onboarded repo is how a real warning gets ignored.

- [ ] **Step 1: Write the failing tests**

```python
def test_reports_ok_when_hooks_installed_and_hookspath_set(tmp_path): ...
def test_warns_when_hookspath_set_but_trampoline_missing(tmp_path): ...
def test_warns_when_trampolines_exist_but_hookspath_unset(tmp_path): ...
def test_un_onboarded_repo_is_not_a_finding(tmp_path): ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement the probe**
- [ ] **Step 4: Run to verify they pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(doctor): probe for hooks configured but not enforced"`

---

### Task 6: `init.templateDir` — the fresh-clone hole

**Files:**
- Modify: `src/graphite/hookinstall.py`, `src/graphite/cli.py`
- Test: `tests/test_hook_template.py`

**Interfaces:**
- Produces: `install_template(template_root: Path, interpreter: Path) -> list[Path]`, `graphite hooks --install-template`.

Git copies `<templateDir>/hooks/<name>` — the `hooks/` subdirectory is required. The templated shim **must fail open**: it runs in *every* new clone on the machine, so a repo without `GRAPHITE.md` must no-op silently. A machine-wide template hook that errors breaks unrelated repos.

- [ ] **Step 1: Write the failing tests**

```python
def test_template_writes_into_hooks_subdirectory(tmp_path): ...
def test_templated_shim_noops_without_graphite_md(tmp_path): ...
def test_templated_shim_is_lf_only(tmp_path): ...
def test_install_template_is_idempotent(tmp_path): ...
```

- [ ] **Step 2–4: Red, implement, green**
- [ ] **Step 5: Commit** — `git commit -m "feat(hooks): machine-wide git template for fresh clones"`

---

## Live verification (not covered by the suite)

Run **after** Task 4, before any rollout to consumer repos:

- [ ] **`BytesAI Learning/app`'s `post-commit` must keep firing.** It auto-pushes to `origin/master` and triggers a Cloudflare deploy. It is a graphite trigger, so it becomes `post-commit.local`. **Verify by watching a real push land, not by reading the file.**
- [ ] **aramid coexistence.** In a repo with aramid installed: run `graphite init`, confirm `.githooks/pre-commit` and `.githooks/pre-push` are aramid's shims byte-identically and carry no graphite marker, then run `aramid init` and confirm it regenerates in place with **no** "refusing to chain" warning on stderr.
- [ ] **Commit latency.** Time 3 commits before and after. The added cost should be ~150-250ms, not ~1.7s. If it is the latter, something imported the CLI.

## Sequencing constraint

Task 3 changes what graphite writes into hook slots that aramid manages. **aramid's agent has been notified of the relocation design and must not object before Task 3 ships.** Tasks 1, 2, 5 are independent of that answer and can proceed regardless.
