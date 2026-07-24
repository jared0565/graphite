# Graphite-First Hardening Implementation Plan (Spec A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make graphite-first strictly observed on onboarded repos: hardened DOC_VERSION 5 templates, an in-package `agent-hook` CLI endpoint (session-start context + deny-capable pre-tool-use), and an idempotent `.claude/settings.json` installer in `init` so re-init propagates enforcement.

**Architecture:** Hook logic lives in a new `agent_hooks.py` module exposed via `python -m graphite agent-hook <event>` (reads Claude Code hook JSON on stdin, writes hook JSON on stdout, always exits 0). A new `agent_settings.py` module does the non-destructive settings merge, called from `init_project`. Template hardening rides the existing managed-block DOC_VERSION refresh.

**Tech Stack:** Python 3.14 stdlib only (json, re, threading, pathlib, argparse). Existing internals: `check_graph_freshness`, `load_validated_graph_bundle`, `atomic_write_text`, `_ensure_managed_text`. pytest.

**Spec:** `docs/superpowers/specs/2026-07-24-graphite-first-hardening-design.md` (operator-approved). Read it before starting.

## Global Constraints

- Worktree: `F:\tmp\graphite-graphite-first`, branch `feat/graphite-first`. **NEVER `pip install -e .` from this worktree** (machine-wide editable install must stay pointed at `F:\Projects\graphite`).
- Python: `C:\Python314\python.exe`. Tests MUST run with the worktree's src first: in PowerShell, from the worktree root: `Remove-Item Env:TMPDIR -ErrorAction SilentlyContinue; $env:CI='1'; $env:PYTHONPATH='F:\tmp\graphite-graphite-first\src'` before any pytest command. Task 1 Step 0 verifies this actually wins over the editable install; stop and report if it doesn't.
- Fail-open invariant: `agent-hook` NEVER exits non-zero, never blocks on error — malformed stdin, missing graph, oversized graph, any exception → exit 0, no denial.
- `agent-hook` is inference-free: the `--llm*` gate must reject it with exit 2, but it must NOT appear in `capabilities` output (`_CANONICAL_COMMANDS` is untouched — published capabilities schema/compat stays stable).
- Byte cap for strict-mode graph loads: 32 MiB (`32 * 1024 * 1024`). Session-start freshness budget: 2.0 s.
- Template phrases that existing tests pin and MUST survive in the new templates: `# Graphite Development Context`, `## Required Workflow`, `python -m graphite check .`, `python -m graphite context <target-file>`, `## Canonical Graph Isolation`, `do not read`, `provider credentials`, and in pointers `Follow \`GRAPHITE.md\`` and `graph-out/graph.json`. Forbidden in templates: `_tools`, `F:\Projects`, `GRAPHITE_LLM_API_KEY`.
- No new dependencies. Frequent commits, message style `feat(...):` / `test(...):` as below, each ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Commit only what each task names; never `git add -A`.

## File Structure

- `src/graphite/init.py` — templates + DOC_VERSION (Task 1); init_project integration (Task 7)
- `src/graphite/agent_hooks.py` — NEW: hook payload handling, session-start, pre-tool-use remind/strict (Tasks 2–4)
- `src/graphite/agent_settings.py` — NEW: `.claude/settings.json` merge installer (Task 6)
- `src/graphite/cli.py` — `agent-hook` subcommand + LLM-gate extension (Task 5); init flags (Task 7)
- `tests/test_agent_hooks.py` — NEW (Tasks 2–5)
- `tests/test_agent_settings.py` — NEW (Task 6)
- `tests/test_init.py` — template + integration additions (Tasks 1, 7)

---

### Task 1: Hardened templates, DOC_VERSION 5

**Files:**
- Modify: `src/graphite/init.py:16` (DOC_VERSION), `:24-62` (GRAPHITE_DOC), `:64-68` (SHARED_POINTER), `:70-78` (CURSOR_POINTER)
- Test: `tests/test_init.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `DOC_VERSION = 5`; template constants used verbatim by later tasks' e2e assertions (phrase `Graphite-first is required`).

- [ ] **Step 0: Verify worktree import precedence**

Run (PowerShell, cwd = worktree root):
```powershell
Remove-Item Env:TMPDIR -ErrorAction SilentlyContinue; $env:CI='1'; $env:PYTHONPATH='F:\tmp\graphite-graphite-first\src'
C:\Python314\python.exe -c "import graphite; print(graphite.__file__)"
```
Expected: `F:\tmp\graphite-graphite-first\src\graphite\__init__.py`. If it prints `F:\Projects\graphite\...`, STOP — report to the operator; do not proceed with a mis-resolved import path.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_init.py`:

```python
def test_v5_templates_state_graphite_first_contract(tmp_path: Path) -> None:
    init_project(tmp_path, platforms=["claude", "cursor"])

    doc = (tmp_path / "GRAPHITE.md").read_text(encoding="utf-8")
    pointer = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    cursor = (tmp_path / ".cursor" / "rules" / "graphite.mdc").read_text(encoding="utf-8")

    assert "Graphite-first is required" in doc
    assert "| Question shape | Run first |" in doc
    assert 'python -m graphite query "callers <symbol>"' in doc
    assert "say so" in doc  # fallback disclosure rule
    assert "Graphite-first is required" in pointer
    assert "literal text and filename lookups" in pointer
    assert "Graphite-first is required" in cursor
```

- [ ] **Step 2: Run to verify the new test fails and the digest pin still passes**

Run: `C:\Python314\python.exe -m pytest tests/test_init.py -v`
Expected: `test_v5_templates_state_graphite_first_contract` FAILS (assert "Graphite-first is required"); all others PASS.

- [ ] **Step 3: Implement the v5 templates**

In `src/graphite/init.py` set `DOC_VERSION = 5` and replace the three template constants. Replace the whole `## Required Workflow` opening of `GRAPHITE_DOC` (everything between the intro paragraphs and `Before non-trivial code changes:`) and keep the numbered lists, `## Canonical Graph Isolation`, and `## Operating Rules` sections exactly as they are, except one added Operating Rules bullet shown below.

New `GRAPHITE_DOC` workflow section (inserted after the `## Required Workflow` heading, before `Before non-trivial code changes:`):

```python
GRAPHITE_DOC = """# Graphite Development Context

Graphite is the shared local code graph for this project. Codex, Claude Code, Gemini CLI, Antigravity, Visual Studio, and other coding agents should use the same graph instead of rebuilding separate mental maps.

All commands below use `python -m graphite`, which works in every shell and for every agent as long as the Python environment has Graphite installed. A bare `graphite` command is equivalent where the console script is on PATH.

## Required Workflow

Graphite-first is required, not advisory. Before any cross-file exploration, consult the graph first. Manual search (grep, glob, directory walking) is the fallback, not the default: use it for literal text and filename lookups, or after a Graphite answer proved insufficient — and say so when you fall back.

| Question shape | Run first |
| --- | --- |
| Who calls / reads / imports this symbol? | `python -m graphite query "callers <symbol>"` |
| What does this symbol call? | `python -m graphite query "calls <symbol>"` |
| Where is this symbol defined? | `python -m graphite search "<symbol>"` |
| What breaks if this file changes? | `python -m graphite impact <file>` |
| What surrounds this file (callers, tests, neighbors)? | `python -m graphite context <file>` |
| How is the project structured? | `python -m graphite query "stats"` |
| Literal string or filename lookup | grep/glob — Graphite not required |

Before non-trivial code changes:

1. Run `python -m graphite check .`
2. Run `python -m graphite context <target-file>` before editing important files.
3. Run `python -m graphite impact <target-file>` before changing shared logic, APIs, data flow, auth, persistence, deployment behavior, or other high-risk paths.
4. Use `python -m graphite search "<symbol, path, or concept>"` to locate nodes; use `python -m graphite query "stats"` when project structure is unclear.
5. Discover supported commands, query verbs, and limits with `python -m graphite capabilities --json` — do not guess query verbs. `query` takes structured verbs; `query --natural "<question>"` accepts only the fixed deterministic grammar listed by capabilities (no inference — unmatched questions fall back to ranked search).

After edits:

1. Run `python -m graphite build .` (skip if a Graphite daemon/watcher keeps this repo fresh; verify with `python -m graphite check .`)
2. Run relevant tests, typechecks, or validation commands.
3. Do not edit `graph-out/` manually.

## Canonical Graph Isolation

`scan`, `build`, `report`, `check`, `validate`, `query`, `context`, `impact`,
`watch`, and `daemon` are inference-free canonical operations. They do not read
provider credentials, ignore ambient `GRAPHITE_LLM*` configuration, and reject
legacy non-`none` LLM flags. Model-generated annotations belong only in the
explicit, non-authoritative overlay boundary and must never replace or modify
canonical `graph-out` artifacts.

## Operating Rules

- Treat Graphite as a project map, not as proof of correctness.
- Always read the source files and tests that Graphite identifies before changing behavior.
- Graphite-first: prefer graph commands over manual cross-file search; fall back only when the graph answer is insufficient, and say so.
- If `python -m graphite check .` reports stale output, rebuild before relying on context or impact data.
- Canonical Graphite operations run locally and never use LLM or network inference.
- For TypeScript resolver issues, use `python -m graphite --typescript-resolver disabled build .` only as a fallback.
"""
```

New pointers:

```python
SHARED_POINTER = """## Shared Graphite Instructions

Graphite-first is required in this repo. Follow `GRAPHITE.md` before making non-trivial code changes: for cross-file questions (who-calls, where-defined, impact, data flow, structure) run the Graphite commands first; grep/glob are for literal text and filename lookups only. Fall back to manual search only after a Graphite answer proved insufficient, and say so. Use the existing `graph-out/graph.json` as the shared project graph, and do not edit `graph-out/` manually.
"""

CURSOR_POINTER = """---
description: Graphite-first project context is required before non-trivial code changes
alwaysApply: true
---

# Graphite Instructions

Graphite-first is required in this repo. Follow `GRAPHITE.md` before making non-trivial code changes: for cross-file questions (who-calls, where-defined, impact, data flow, structure) run the Graphite commands first; grep/glob are for literal text and filename lookups only. Fall back to manual search only after a Graphite answer proved insufficient, and say so. Use the existing `graph-out/graph.json` as the shared project graph, and do not edit `graph-out/` manually.
"""
```

- [ ] **Step 4: Re-pin the template digest**

Run: `C:\Python314\python.exe -m pytest tests/test_init.py::test_template_change_requires_doc_version_bump -v`
Expected: FAIL showing the new sha256. Update the pin in that test to `(5, "<new digest from the failure output>")`.

- [ ] **Step 5: Run the whole init test file**

Run: `C:\Python314\python.exe -m pytest tests/test_init.py -v`
Expected: ALL PASS (the pinned-phrase tests in the Global Constraints list must not have broken).

- [ ] **Step 6: Commit**

```bash
git add src/graphite/init.py tests/test_init.py
git commit -m "feat(init): DOC_VERSION 5 graphite-first contract templates"
```

---

### Task 2: `agent_hooks.py` — payload plumbing + session-start

**Files:**
- Create: `src/graphite/agent_hooks.py`
- Test: `tests/test_agent_hooks.py`

**Interfaces:**
- Consumes: `check_graph_freshness(root, cfg)` from `graphite.freshness`; `Config` from `graphite.config` (construct with ABSOLUTE dirs: `Config(output_dir=root / "graph-out", cache_dir=root / ".cache" / "graphite")` — `check_graph_freshness` resolves `cfg.output_dir` as given, it does NOT join `root`).
- Produces: `handle_session_start(payload: dict) -> dict | None`; `_payload_root(payload: dict) -> Path`; module constant `SESSION_CONTRACT: str`. Task 5's CLI calls `handle_session_start`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_hooks.py`:

```python
"""Tests for the Claude Code agent-hook handlers (fail-open by design)."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.agent_hooks import handle_session_start


def _payload(root: Path) -> dict:
    return {"session_id": "s1", "cwd": str(root), "hook_event_name": "SessionStart"}


def test_session_start_missing_graph_reports_missing(tmp_path: Path) -> None:
    out = handle_session_start(_payload(tmp_path))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "graphite-first" in ctx
    assert "missing" in ctx
    assert "python -m graphite build ." in ctx


def test_session_start_stale_graph_warns(tmp_path: Path) -> None:
    (tmp_path / "graph-out").mkdir()
    (tmp_path / "graph-out" / "graph.json").write_text("{}", encoding="utf-8")
    # No manifest -> check_graph_freshness reports stale ("missing manifest").
    out = handle_session_start(_payload(tmp_path))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "STALE" in ctx


def test_session_start_fresh_graph(tmp_path: Path, monkeypatch) -> None:
    from graphite.cli import main

    (tmp_path / "alpha.py").write_text("def target_symbol():\n    return 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # cmd_build writes cfg.output_dir relative to CWD
    assert main(["build", "."]) == 0
    out = handle_session_start(_payload(tmp_path))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "fresh" in ctx


def test_session_start_bad_cwd_fails_open(tmp_path: Path) -> None:
    # Nonexistent cwd must not raise; missing graph messaging is acceptable.
    out = handle_session_start({"cwd": str(tmp_path / "nope")})
    assert out is None or "graphite-first" in out["hookSpecificOutput"]["additionalContext"]
```

Note: `cmd_build` uses `_config_from_args` (NOT the project-scoped config), so its relative `output_dir` (`graph-out`) resolves against the **current working directory**, not the path argument. Every test that builds a fixture graph MUST `monkeypatch.chdir(tmp_path)` first and build `"."` — otherwise the graph lands in the test runner's cwd.

- [ ] **Step 2: Run to verify failure**

Run: `C:\Python314\python.exe -m pytest tests/test_agent_hooks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'graphite.agent_hooks'`.

- [ ] **Step 3: Implement the module**

Create `src/graphite/agent_hooks.py`:

```python
"""Claude Code agent-hook handlers enforcing graphite-first on onboarded repos.

Every handler is fail-open by contract: any parse, IO, graph, or timing problem
yields ``None`` (no hook output) rather than an exception, so a hook bug can
never break a tool call or a session. The CLI wrapper adds a second catch-all.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .config import Config
from .freshness import check_graph_freshness

SESSION_CONTRACT = (
    "graphite-first: this repo uses a shared code graph. For cross-file questions "
    "(who-calls / who-reads / where-defined / data-flow / blast-radius / structure), run "
    'python -m graphite context <file> | impact <file> | query "..." | search "..." BEFORE '
    "grep/glob exploration; manual search is for literal text and filename lookups. Fall "
    "back only when a graph answer proved insufficient, and say so. See GRAPHITE.md."
)

_FRESHNESS_BUDGET_SECONDS = 2.0


def _payload_root(payload: dict[str, Any]) -> Path:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    return Path.cwd()


def _freshness_within_budget(root: Path) -> str | None:
    """Return 'fresh' | 'stale' | None (unknown), never raising, within budget."""
    outcome: list[str | None] = [None]

    def _worker() -> None:
        try:
            cfg = Config(output_dir=root / "graph-out", cache_dir=root / ".cache" / "graphite")
            status = check_graph_freshness(root, cfg)
            outcome[0] = "stale" if status.get("stale") else "fresh"
        except Exception:
            outcome[0] = None

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    worker.join(_FRESHNESS_BUDGET_SECONDS)
    return outcome[0]


def handle_session_start(payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        root = _payload_root(payload)
        if (root / "graph-out" / "graph.json").is_file():
            freshness = _freshness_within_budget(root)
            if freshness == "stale":
                status = "Graph status: STALE - run `python -m graphite build .` before relying on graph answers."
            elif freshness == "fresh":
                status = "Graph status: fresh."
            else:
                status = "Graph status: unknown - verify with `python -m graphite check .`."
        else:
            status = "Graph status: missing - run `python -m graphite build .` to create it."
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"{SESSION_CONTRACT}\n{status}",
            }
        }
    except Exception:
        return None
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\Python314\python.exe -m pytest tests/test_agent_hooks.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/graphite/agent_hooks.py tests/test_agent_hooks.py
git commit -m "feat(agent-hooks): session-start graphite-first context with freshness budget"
```

---

### Task 3: pre-tool-use remind mode

**Files:**
- Modify: `src/graphite/agent_hooks.py`
- Test: `tests/test_agent_hooks.py`

**Interfaces:**
- Consumes: `_payload_root` from Task 2.
- Produces: `handle_pre_tool_use(payload: dict, mode: str) -> dict | None`; module constant `PRE_TOOL_REMINDER: str`. Task 4 extends it; Task 5's CLI calls it with `mode` from argv.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent_hooks.py`:

```python
from graphite.agent_hooks import handle_pre_tool_use


def _grep_payload(root: Path, pattern: str, **tool_input) -> dict:
    return {
        "session_id": "s1",
        "cwd": str(root),
        "hook_event_name": "PreToolUse",
        "tool_name": "Grep",
        "tool_input": {"pattern": pattern, **tool_input},
    }


def _with_graph(tmp_path: Path) -> Path:
    (tmp_path / "graph-out").mkdir(exist_ok=True)
    (tmp_path / "graph-out" / "graph.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_remind_emits_context_when_graph_present(tmp_path: Path) -> None:
    _with_graph(tmp_path)
    out = handle_pre_tool_use(_grep_payload(tmp_path, "anything"), "remind")
    hook = out["hookSpecificOutput"]
    assert hook["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hook
    assert "graph-first" in hook["additionalContext"]


def test_remind_silent_without_graph(tmp_path: Path) -> None:
    assert handle_pre_tool_use(_grep_payload(tmp_path, "anything"), "remind") is None


def test_remind_ignores_other_tools(tmp_path: Path) -> None:
    _with_graph(tmp_path)
    payload = _grep_payload(tmp_path, "x")
    payload["tool_name"] = "Read"
    assert handle_pre_tool_use(payload, "remind") is None


def test_glob_gets_remind_never_deny_even_in_strict(tmp_path: Path) -> None:
    _with_graph(tmp_path)
    payload = _grep_payload(tmp_path, "target_symbol")
    payload["tool_name"] = "Glob"
    out = handle_pre_tool_use(payload, "strict")
    assert "permissionDecision" not in out["hookSpecificOutput"]
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\Python314\python.exe -m pytest tests/test_agent_hooks.py -v`
Expected: new tests FAIL with `ImportError: cannot import name 'handle_pre_tool_use'`.

- [ ] **Step 3: Implement remind mode**

Append to `src/graphite/agent_hooks.py`:

```python
PRE_TOOL_REMINDER = (
    "graph-first: this repo's graphite graph answers relationship questions. For who-calls / "
    "who-reads / data-flow / blast-radius / where-defined questions, use python -m graphite "
    'context <file> | impact <file> | query "..." instead of grepping or globbing across '
    "files. Literal text and filename searches are fine. Falling back after an insufficient "
    "graph answer is allowed - say so when you do."
)


def handle_pre_tool_use(payload: dict[str, Any], mode: str) -> dict[str, Any] | None:
    try:
        if payload.get("tool_name") not in ("Grep", "Glob"):
            return None
        root = _payload_root(payload)
        if not (root / "graph-out" / "graph.json").is_file():
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": PRE_TOOL_REMINDER,
            }
        }
    except Exception:
        return None
```

(Strict denial is Task 4; the strict-mode Glob test already passes because deny never happens here.)

- [ ] **Step 4: Run to verify pass**

Run: `C:\Python314\python.exe -m pytest tests/test_agent_hooks.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/graphite/agent_hooks.py tests/test_agent_hooks.py
git commit -m "feat(agent-hooks): pre-tool-use remind mode for Grep/Glob"
```

---

### Task 4: strict-mode graph-backed deny classifier

**Files:**
- Modify: `src/graphite/agent_hooks.py`
- Test: `tests/test_agent_hooks.py`

**Interfaces:**
- Consumes: `load_validated_graph_bundle(path, root=...)` from `graphite.graph_io` (returns `(bundle_dict, nx_graph)`; raises `GraphReadError` on any invalid/oversized artifact — caught by the fail-open wrapper). Graph node data: `name` attribute (as used by `search_graph` tiers 0/1 in `query.py:409-416`).
- Produces: strict behavior inside `handle_pre_tool_use`; helpers `_pattern_tokens(pattern: str) -> list[str]`, `_graph_symbol(root: Path, tokens: list[str]) -> tuple[str, str] | None` (returns `(node_id, matched_token)`). Constants `MAX_HOOK_GRAPH_BYTES = 32 * 1024 * 1024`, `_MAX_TOKENS_CHECKED = 5`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent_hooks.py` (the build fixture gives a REAL graph whose function node has `name == "target_symbol"`):

```python
import pytest


@pytest.fixture()
def built_repo(tmp_path: Path, monkeypatch) -> Path:
    from graphite.cli import main

    (tmp_path / "alpha.py").write_text(
        "def target_symbol():\n    return 1\n\n\ndef other():\n    return target_symbol()\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)  # cmd_build writes cfg.output_dir relative to CWD
    assert main(["build", "."]) == 0
    return tmp_path


def test_strict_denies_cross_file_grep_for_known_symbol(built_repo: Path) -> None:
    out = handle_pre_tool_use(_grep_payload(built_repo, "target_symbol"), "strict")
    hook = out["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"
    reason = hook["permissionDecisionReason"]
    assert "target_symbol" in reason
    assert 'query "callers target_symbol"' in reason
    assert "graphite" in reason


def test_strict_allows_unknown_tokens(built_repo: Path) -> None:
    out = handle_pre_tool_use(_grep_payload(built_repo, "no_such_symbol_here"), "strict")
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_strict_allows_single_file_scoped_grep(built_repo: Path) -> None:
    out = handle_pre_tool_use(
        _grep_payload(built_repo, "target_symbol", path=str(built_repo / "alpha.py")), "strict"
    )
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_strict_denies_directory_scoped_grep(built_repo: Path) -> None:
    # A directory-scoped search is still cross-file; only single-FILE scoping opts out.
    out = handle_pre_tool_use(
        _grep_payload(built_repo, "target_symbol", path=str(built_repo)), "strict"
    )
    assert out["hookSpecificOutput"].get("permissionDecision") == "deny"


def test_strict_allows_literal_patterns_without_identifiers(built_repo: Path) -> None:
    out = handle_pre_tool_use(_grep_payload(built_repo, "== 42"), "strict")
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_strict_falls_back_to_remind_on_oversized_graph(built_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr("graphite.agent_hooks.MAX_HOOK_GRAPH_BYTES", 1)
    out = handle_pre_tool_use(_grep_payload(built_repo, "target_symbol"), "strict")
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_remind_mode_never_denies_known_symbols(built_repo: Path) -> None:
    out = handle_pre_tool_use(_grep_payload(built_repo, "target_symbol"), "remind")
    assert "permissionDecision" not in out["hookSpecificOutput"]
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\Python314\python.exe -m pytest tests/test_agent_hooks.py -v`
Expected: `test_strict_denies_cross_file_grep_for_known_symbol` and `test_strict_allows_directory_scoped_grep` FAIL (no deny emitted); the "allows" tests may already pass.

- [ ] **Step 3: Implement the classifier**

In `src/graphite/agent_hooks.py` add imports `import re` and `from .graph_io import load_validated_graph_bundle`, then:

```python
MAX_HOOK_GRAPH_BYTES = 32 * 1024 * 1024
_MAX_TOKENS_CHECKED = 5
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _pattern_tokens(pattern: str) -> list[str]:
    """Identifier-like tokens from a grep pattern, deduplicated, capped."""
    seen: set[str] = set()
    tokens: list[str] = []
    for token in _IDENTIFIER_RE.findall(pattern):
        low = token.lower()
        if low in seen:
            continue
        seen.add(low)
        tokens.append(token)
        if len(tokens) >= _MAX_TOKENS_CHECKED:
            break
    return tokens


def _graph_symbol(root: Path, tokens: list[str]) -> tuple[str, str] | None:
    """First token that IS a graph node (exact id or name match), else None."""
    graph_path = root / "graph-out" / "graph.json"
    if not graph_path.is_file() or graph_path.stat().st_size > MAX_HOOK_GRAPH_BYTES:
        return None
    _, graph = load_validated_graph_bundle(graph_path, root=root)
    lowered = {token.lower(): token for token in tokens}
    for node_id, data in graph.nodes(data=True):
        node_lower = node_id.lower()
        name = str(data.get("name", "")).lower()
        if node_lower in lowered:
            return node_id, lowered[node_lower]
        if name and name in lowered:
            return node_id, lowered[name]
    return None


def _strict_denial(payload: dict[str, Any], root: Path) -> str | None:
    try:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return None
        pattern = tool_input.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return None
        target = tool_input.get("path")
        if isinstance(target, str) and target:
            candidate = Path(target)
            if not candidate.is_absolute():
                candidate = root / candidate
            if candidate.is_file():
                return None  # single-file scoped searches are always allowed
        tokens = _pattern_tokens(pattern)
        if not tokens:
            return None
        match = _graph_symbol(root, tokens)
        if match is None:
            return None
        node_id, token = match
        return (
            f"graphite-first (strict): '{token}' is a symbol in this repo's code graph "
            f"({node_id}). Use the graph instead of cross-file grep: "
            f'python -m graphite query "callers {token}" | python -m graphite query "calls {token}" | '
            f'python -m graphite search "{token}" | python -m graphite context <file>. '
            "Literal-text searches scoped to a single file path are always allowed."
        )
    except Exception:
        return None
```

Then extend `handle_pre_tool_use` — after the graph-exists check, before the remind return:

```python
        if mode == "strict" and payload.get("tool_name") == "Grep":
            denial = _strict_denial(payload, root)
            if denial is not None:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": denial,
                    }
                }
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\Python314\python.exe -m pytest tests/test_agent_hooks.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/graphite/agent_hooks.py tests/test_agent_hooks.py
git commit -m "feat(agent-hooks): strict-mode deny backed by exact graph-symbol match"
```

---

### Task 5: CLI `agent-hook` subcommand + LLM-gate extension

**Files:**
- Modify: `src/graphite/cli.py` — new `_INFERENCE_FREE_EXTRA_COMMANDS` beside `_CANONICAL_COMMANDS` (`:94-109`), gate condition (`:1994`), `cmd_agent_hook`, parser wiring (insert after the `p_capabilities` block, `:1835-1838`)
- Test: `tests/test_agent_hooks.py`

**Interfaces:**
- Consumes: `handle_session_start`, `handle_pre_tool_use` (Tasks 2–4).
- Produces: `python -m graphite agent-hook {session-start|pre-tool-use} [--mode remind|strict]`. Exit code ALWAYS 0 except the `--llm*` gate's 2. Task 6 writes command strings that must match this surface exactly: `python -m graphite agent-hook pre-tool-use --mode remind` etc.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent_hooks.py`:

```python
import io


def _run_cli_hook(monkeypatch, capsys, argv: list[str], payload) -> tuple[int, str]:
    from graphite.cli import main

    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    code = main(argv)
    return code, capsys.readouterr().out


def test_cli_session_start_emits_hook_json(tmp_path, monkeypatch, capsys) -> None:
    code, out = _run_cli_hook(
        monkeypatch, capsys, ["agent-hook", "session-start"], {"cwd": str(tmp_path)}
    )
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_cli_pre_tool_use_strict_denies(built_repo, monkeypatch, capsys) -> None:
    code, out = _run_cli_hook(
        monkeypatch,
        capsys,
        ["agent-hook", "pre-tool-use", "--mode", "strict"],
        _grep_payload(built_repo, "target_symbol"),
    )
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_cli_malformed_stdin_fails_open(tmp_path, monkeypatch, capsys) -> None:
    code, out = _run_cli_hook(monkeypatch, capsys, ["agent-hook", "session-start"], "{not json")
    assert code == 0
    assert out == ""


def test_cli_agent_hook_rejects_llm_flags(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    from graphite.cli import main

    assert main(["--llm", "cloud", "agent-hook", "session-start"]) == 2


def test_capabilities_output_does_not_list_agent_hook(capsys) -> None:
    from graphite.cli import main

    assert main(["capabilities", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "agent-hook" not in payload["commands"]
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\Python314\python.exe -m pytest tests/test_agent_hooks.py -v`
Expected: CLI tests FAIL (argparse: `invalid choice: 'agent-hook'`); capabilities test PASSES already.

- [ ] **Step 3: Implement CLI wiring**

In `src/graphite/cli.py`:

After the `_CANONICAL_COMMANDS` frozenset (line 109) add:

```python
# Hook endpoints are inference-free like canonical commands but are not part of
# the agent-facing query surface, so they stay out of `capabilities` output.
_INFERENCE_FREE_EXTRA_COMMANDS = frozenset({"agent-hook"})
_LLM_GATED_COMMANDS = _CANONICAL_COMMANDS | _INFERENCE_FREE_EXTRA_COMMANDS
```

Change the gate condition (line 1994) from `if args.command in _CANONICAL_COMMANDS and (` to `if args.command in _LLM_GATED_COMMANDS and (`.

Add the command handler (near `cmd_capabilities`):

```python
def cmd_agent_hook(args: argparse.Namespace) -> int:
    from .agent_hooks import handle_pre_tool_use, handle_session_start

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
        if args.event == "session-start":
            out = handle_session_start(payload)
        else:
            out = handle_pre_tool_use(payload, args.mode)
        if out is not None:
            print(json.dumps(out, ensure_ascii=False))
    except Exception:
        pass  # fail-open: a hook problem must never break a tool call
    return 0
```

Parser wiring (insert after the `p_capabilities` block, line ~1838):

```python
    p_agent_hook = sub.add_parser(
        "agent-hook",
        help="Claude Code hook endpoint for graphite-first enforcement (reads hook JSON on stdin; always exits 0)",
    )
    p_agent_hook.add_argument("event", choices=["session-start", "pre-tool-use"], help="Hook event to handle")
    p_agent_hook.add_argument("--mode", choices=["remind", "strict"], default="remind", help="pre-tool-use enforcement mode")
    p_agent_hook.set_defaults(func=cmd_agent_hook)
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\Python314\python.exe -m pytest tests/test_agent_hooks.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/graphite/cli.py tests/test_agent_hooks.py
git commit -m "feat(cli): agent-hook subcommand, inference-free gated, absent from capabilities"
```

---

### Task 6: `agent_settings.py` — idempotent `.claude/settings.json` installer

**Files:**
- Create: `src/graphite/agent_settings.py`
- Test: `tests/test_agent_settings.py`

**Interfaces:**
- Consumes: `atomic_write_text` from `graphite.io` (creates parent dirs).
- Produces: `ensure_claude_settings(root: Path, *, mode: str | None = None) -> dict` with keys `path` (str), `changed` (bool), `action` (one of `"created"`, `"updated"`, `"already current"`, `"malformed settings"`), `mode` (resolved `"remind"`/`"strict"`); `existing_mode(root: Path) -> str | None`; constant `HOOK_COMMAND_PREFIX = "python -m graphite agent-hook"`. Task 7 calls both.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_settings.py`:

```python
"""Tests for the idempotent Claude Code settings installer."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.agent_settings import ensure_claude_settings, existing_mode


def _settings(root: Path) -> dict:
    return json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _commands(settings: dict, event: str) -> list[str]:
    return [
        h["command"]
        for entry in settings.get("hooks", {}).get(event, [])
        for h in entry.get("hooks", [])
    ]


def test_fresh_install_writes_both_events_remind_mode(tmp_path: Path) -> None:
    result = ensure_claude_settings(tmp_path)
    settings = _settings(tmp_path)

    assert result["changed"] is True
    assert result["action"] == "created"
    assert result["mode"] == "remind"
    assert _commands(settings, "PreToolUse") == ["python -m graphite agent-hook pre-tool-use --mode remind"]
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Grep|Glob"
    assert _commands(settings, "SessionStart") == ["python -m graphite agent-hook session-start"]


def test_reinstall_is_idempotent(tmp_path: Path) -> None:
    ensure_claude_settings(tmp_path)
    second = ensure_claude_settings(tmp_path)
    assert second["changed"] is False
    assert second["action"] == "already current"


def test_preserves_foreign_settings_and_hooks(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo custom"}]}
                    ],
                    "Stop": [{"hooks": [{"type": "command", "command": "echo bye"}]}],
                },
            }
        ),
        encoding="utf-8",
    )

    ensure_claude_settings(tmp_path)
    settings = _settings(tmp_path)

    assert settings["model"] == "opus"
    assert "echo custom" in _commands(settings, "PreToolUse")
    assert _commands(settings, "Stop") == ["echo bye"]
    assert any(c.startswith("python -m graphite agent-hook pre-tool-use") for c in _commands(settings, "PreToolUse"))


def test_replaces_stale_graphite_entries_on_reinit(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Grep",
                            "hooks": [{"type": "command", "command": "python -m graphite agent-hook pre-tool-use --old-flag"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = ensure_claude_settings(tmp_path)
    settings = _settings(tmp_path)

    assert result["changed"] is True
    commands = _commands(settings, "PreToolUse")
    assert commands == ["python -m graphite agent-hook pre-tool-use --mode remind"]
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Grep|Glob"


def test_mode_preserved_without_explicit_request(tmp_path: Path) -> None:
    ensure_claude_settings(tmp_path, mode="strict")
    assert existing_mode(tmp_path) == "strict"
    result = ensure_claude_settings(tmp_path)  # routine re-init, no explicit mode
    assert result["mode"] == "strict"
    assert "--mode strict" in _commands(_settings(tmp_path), "PreToolUse")[0]


def test_explicit_mode_overrides(tmp_path: Path) -> None:
    ensure_claude_settings(tmp_path, mode="strict")
    result = ensure_claude_settings(tmp_path, mode="remind")
    assert result["mode"] == "remind"
    assert "--mode remind" in _commands(_settings(tmp_path), "PreToolUse")[0]


def test_malformed_json_is_never_touched(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    result = ensure_claude_settings(tmp_path)

    assert result["changed"] is False
    assert result["action"] == "malformed settings"
    assert path.read_text(encoding="utf-8") == "{not json"
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\Python314\python.exe -m pytest tests/test_agent_settings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'graphite.agent_settings'`.

- [ ] **Step 3: Implement the installer**

Create `src/graphite/agent_settings.py`:

```python
"""Idempotent Claude Code settings wiring for graphite agent hooks.

Graphite owns exactly the hook commands whose command string starts with
``HOOK_COMMAND_PREFIX``; those are stripped and re-written on every install.
Everything else in ``.claude/settings.json`` is preserved untouched, and a file
that fails to parse is never modified.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .io import atomic_write_text

HOOK_COMMAND_PREFIX = "python -m graphite agent-hook"
DEFAULT_MODE = "remind"
_MODES = ("remind", "strict")
_SESSION_START_COMMAND = f"{HOOK_COMMAND_PREFIX} session-start"


def _pre_tool_use_command(mode: str) -> str:
    return f"{HOOK_COMMAND_PREFIX} pre-tool-use --mode {mode}"


def _is_graphite_command(hook: Any) -> bool:
    return isinstance(hook, dict) and str(hook.get("command", "")).startswith(HOOK_COMMAND_PREFIX)


def _strip_graphite(groups: Any) -> list[Any]:
    """Drop graphite-owned commands; keep foreign groups byte-identical."""
    if not isinstance(groups, list):
        return []
    kept: list[Any] = []
    for entry in groups:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            kept.append(entry)
            continue
        remaining = [hook for hook in entry["hooks"] if not _is_graphite_command(hook)]
        if remaining == entry["hooks"]:
            kept.append(entry)
        elif remaining:
            kept.append({**entry, "hooks": remaining})
    return kept


def _load_settings(path: Path) -> dict[str, Any] | None:
    """Parsed settings dict, {} when absent, None when malformed."""
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def existing_mode(root: Path) -> str | None:
    settings = _load_settings(root / ".claude" / "settings.json")
    if not settings:
        return None
    hooks = settings.get("hooks")
    groups = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    for entry in groups if isinstance(groups, list) else []:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else []:
            command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
            if command.startswith(HOOK_COMMAND_PREFIX) and "pre-tool-use" in command:
                return "strict" if "--mode strict" in command else "remind"
    return None


def ensure_claude_settings(root: Path, *, mode: str | None = None) -> dict[str, Any]:
    path = root / ".claude" / "settings.json"
    resolved = mode or existing_mode(root) or DEFAULT_MODE
    if resolved not in _MODES:
        raise ValueError(f"unknown agent-hook mode: {resolved}")
    existed = path.exists()
    original = _load_settings(path)
    if original is None:
        return {"path": str(path), "changed": False, "action": "malformed settings", "mode": resolved}

    hooks_value = original.get("hooks")
    hooks = dict(hooks_value) if isinstance(hooks_value, dict) else {}
    desired = {
        "PreToolUse": {
            "matcher": "Grep|Glob",
            "hooks": [{"type": "command", "command": _pre_tool_use_command(resolved)}],
        },
        "SessionStart": {
            "hooks": [{"type": "command", "command": _SESSION_START_COMMAND}],
        },
    }
    changed = False
    for event, entry in desired.items():
        groups = _strip_graphite(hooks.get(event))
        groups.append(entry)
        if hooks.get(event) != groups:
            changed = True
        hooks[event] = groups

    if not changed:
        return {"path": str(path), "changed": False, "action": "already current", "mode": resolved}
    updated = {**original, "hooks": hooks}
    atomic_write_text(path, json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
    action = "updated" if existed else "created"
    return {"path": str(path), "changed": True, "action": action, "mode": resolved}
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\Python314\python.exe -m pytest tests/test_agent_settings.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/graphite/agent_settings.py tests/test_agent_settings.py
git commit -m "feat(agent-settings): non-destructive .claude/settings.json hook installer"
```

---

### Task 7: init integration (flags, InitResult, allowlist, output)

**Files:**
- Modify: `src/graphite/init.py` (`init_project` `:189-219`, `InitResult` `:134-153`), `src/graphite/cli.py` (`cmd_init` `:580-645`, init parser `:1765-1775`)
- Test: `tests/test_init.py`

**Interfaces:**
- Consumes: `ensure_claude_settings` (Task 6).
- Produces: `init_project(project_root, *, platforms, daemon_base=None, agent_hooks_mode: str | None = None, install_agent_hooks: bool = True)`; `InitResult.agent_hooks: dict[str, Any]` (in `to_dict()` under `"agent_hooks"`); CLI flags `--strict` / `--remind` (mutually exclusive) and `--no-agent-hooks`. Spec B's plan extends the same installer call — do not rename these.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_init.py`:

```python
def test_init_installs_agent_hook_wiring_and_allowlists_it(tmp_path: Path) -> None:
    _write(tmp_path / ".gitignore", "/*\n!/.gitignore\n")

    result = init_project(tmp_path, platforms=["claude"]).to_dict()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))

    assert result["agent_hooks"]["action"] == "created"
    assert result["agent_hooks"]["mode"] == "remind"
    commands = [
        h["command"]
        for entry in settings["hooks"]["PreToolUse"]
        for h in entry["hooks"]
    ]
    assert "python -m graphite agent-hook pre-tool-use --mode remind" in commands
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "!/.claude/" in gitignore
    assert "!/.claude/settings.json" in gitignore


def test_init_strict_flag_and_mode_preserved_on_reinit(tmp_path: Path) -> None:
    first = init_project(tmp_path, platforms=["claude"], agent_hooks_mode="strict").to_dict()
    second = init_project(tmp_path, platforms=["claude"]).to_dict()

    assert first["agent_hooks"]["mode"] == "strict"
    assert second["agent_hooks"]["mode"] == "strict"
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    joined = json.dumps(settings)
    assert "--mode strict" in joined
    assert "--mode remind" not in joined


def test_init_no_agent_hooks_skips_wiring(tmp_path: Path) -> None:
    result = init_project(tmp_path, platforms=["claude"], install_agent_hooks=False).to_dict()

    assert result["agent_hooks"]["action"] == "skipped"
    assert not (tmp_path / ".claude").exists()


def test_reinit_of_v4_repo_refreshes_docs_and_adds_wiring(tmp_path: Path) -> None:
    _write(
        tmp_path / "GRAPHITE.md",
        "<!-- graphite:managed version=4 -->\nOLD V4 BODY\n<!-- graphite:managed-end -->\n",
    )

    result = init_project(tmp_path, platforms=["claude"]).to_dict()

    assert result["graphite_doc"]["action"] == "refreshed"
    assert "Graphite-first is required" in (tmp_path / "GRAPHITE.md").read_text(encoding="utf-8")
    assert (tmp_path / ".claude" / "settings.json").exists()


def test_init_cli_strict_flag(tmp_path: Path, capsys) -> None:
    code = main(
        ["init", str(tmp_path), "--platform", "claude", "--strict", "--no-build", "--no-validate", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["agent_hooks"]["mode"] == "strict"
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\Python314\python.exe -m pytest tests/test_init.py -v`
Expected: new tests FAIL (`KeyError: 'agent_hooks'` / `TypeError: unexpected keyword argument`).

- [ ] **Step 3: Implement init integration**

`src/graphite/init.py` — import `from .agent_settings import ensure_claude_settings`; add the field to `InitResult`:

```python
@dataclass(frozen=True)
class InitResult:
    project_root: Path
    platforms: tuple[str, ...]
    graphite_doc: dict[str, Any]
    gitignore: dict[str, Any]
    platform_files: list[dict[str, Any]]
    allowlist: dict[str, Any]
    daemon: dict[str, Any]
    agent_hooks: dict[str, Any]
```

and `"agent_hooks": self.agent_hooks,` inside `to_dict()`. Extend `init_project`:

```python
def init_project(
    project_root: Path,
    *,
    platforms: Iterable[str],
    daemon_base: Path | None = None,
    agent_hooks_mode: str | None = None,
    install_agent_hooks: bool = True,
) -> InitResult:
```

and between the platform-file loop and the allowlist call:

```python
    if install_agent_hooks:
        agent_hooks = ensure_claude_settings(root, mode=agent_hooks_mode)
        instruction_paths.append(Path(".claude/settings.json"))
    else:
        agent_hooks = {
            "path": str(root / ".claude" / "settings.json"),
            "changed": False,
            "action": "skipped",
            "mode": None,
        }
```

then pass `agent_hooks=agent_hooks` to the `InitResult(...)` constructor call.

`src/graphite/cli.py` — parser additions after the `--json` line of `p_init` (line 1774):

```python
    p_init_mode = p_init.add_mutually_exclusive_group()
    p_init_mode.add_argument("--strict", action="store_true", help="Write strict-mode graphite-first hook wiring (denies provable relationship greps)")
    p_init_mode.add_argument("--remind", action="store_true", help="Write remind-mode hook wiring (non-blocking reminders; default for first-time wiring)")
    p_init.add_argument("--no-agent-hooks", action="store_true", help="Skip Claude Code hook wiring in .claude/settings.json")
```

In `cmd_init` replace the `result = init_project(...)` call (line 594):

```python
    agent_hooks_mode = "strict" if args.strict else ("remind" if args.remind else None)
    result = init_project(
        root,
        platforms=platforms,
        daemon_base=daemon_base,
        agent_hooks_mode=agent_hooks_mode,
        install_agent_hooks=not args.no_agent_hooks,
    ).to_dict()
```

and in the human-readable branch, after the platform-file loop (line 633):

```python
        agent_hooks = result["agent_hooks"]
        print(
            f"  - agent_hooks: {agent_hooks.get('action')} "
            f"({agent_hooks.get('path')}, mode={agent_hooks.get('mode')})"
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\Python314\python.exe -m pytest tests/test_init.py tests/test_agent_settings.py -v`
Expected: ALL PASS (including the pre-existing init tests — `init_project`'s new parameters are keyword-optional so old call sites are unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/graphite/init.py src/graphite/cli.py tests/test_init.py
git commit -m "feat(init): install graphite-first hook wiring with strict/remind modes"
```

---

### Task 8: Full-suite + lint gate

**Files:** none new.

- [ ] **Step 1: Full suite**

Run (PowerShell, worktree root, env from Task 1 Step 0 still set):
```powershell
C:\Python314\python.exe -m pytest -q
```
Expected: 0 failures (baseline before this program: 1979 passed / 44 skipped; new tests add to the pass count). Any failure: fix before proceeding — do not skip, do not mark complete with red.

- [ ] **Step 2: Ruff**

Run: `C:\Python314\python.exe -m ruff check src/graphite/agent_hooks.py src/graphite/agent_settings.py src/graphite/init.py src/graphite/cli.py tests/test_agent_hooks.py tests/test_agent_settings.py tests/test_init.py`
Expected: no findings. Fix anything reported.

- [ ] **Step 3: Commit (only if fixes were needed)**

```bash
git add -u
git commit -m "chore: suite + lint fixes for graphite-first hardening"
```

---

## Out of scope for this plan

- Spec A Change 4 (consumer-repo rollout + global-hook retirement) is operator-gated and happens after Spec B lands — not a plan task.
- The Stop hook event and savings ledger are Spec B (separate plan).
