# Savings Display Implementation Plan (Spec B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Toggleable per-repo display of estimated time/token savings versus working without graphite: a usage ledger fed by the canonical read commands, a result-scaled estimator, a Stop-hook turn-end summary, and a `graphite savings` CLI.

**Architecture:** Two new modules — `usage_ledger.py` (machine-local `.graphite/local/` ledger, toggle, stop-cursor; every public function swallows its own errors) and `savings.py` (estimator constants + aggregation + formatting). `cli.py` records usage from `query`/`search`/`context`/`impact`, adds the `savings` subcommand, and dispatches the new `stop` agent-hook event; `agent_settings.py` wires the Stop hook; `agent_hooks.py` gains `handle_stop`.

**Tech Stack:** Python 3.14 stdlib only. Builds on Spec A's `agent_hooks.py` / `agent_settings.py` / `cmd_agent_hook` (explicit event dispatch; unknown events are silent no-ops).

**Spec:** `docs/superpowers/specs/2026-07-24-savings-display-design.md` (operator-approved). Read it before starting.

## Global Constraints

- Worktree: `F:\tmp\graphite-graphite-first`, branch `feat/graphite-first`. **NEVER `pip install -e .` from this worktree.**
- Python: `C:\Python314\python.exe`. Before any pytest run (PowerShell, worktree root): `Remove-Item Env:TMPDIR -ErrorAction SilentlyContinue; $env:CI='1'; $env:PYTHONPATH='F:\tmp\graphite-graphite-first\src'` and verify `import graphite` resolves to the worktree; STOP if not.
- Never-fatal invariant: ledger writes, toggle IO, cursor IO, and `handle_stop` are invisible to the command/hook on ANY failure (swallow, exit 0, no output). The Stop hook never blocks a turn.
- Estimator constants (starting values, printed by the report): `GREP_TOKENS = 1000`, `GREP_SECONDS = 20`, `READ_SECONDS = 10`, `FILE_TOKEN_CAP = 2000`. Formula: `grep_rounds = 1 + K // 10`; manual tokens = `grep_rounds*GREP_TOKENS + Σ min(file_bytes//4, FILE_TOKEN_CAP)`; manual seconds = `grep_rounds*GREP_SECONDS + K*READ_SECONDS`; graphite cost = `output_bytes//4` tokens + measured wall seconds; savings floored at 0. Every displayed figure labeled estimated.
- Ledger: `.graphite/local/usage.jsonl`, rotation at `5 * 1024 * 1024` bytes to `usage.jsonl.1` (single generation), ≤ 100 files per entry, corrupt lines skipped by readers. `.graphite/local/` is ALREADY gitignored by the existing `**/.graphite/` line in `GRAPHITE_GITIGNORE_LINES` (bootstrap.py:20) — no gitignore change; do not add one.
- Toggle: `.graphite/local/settings.json` `{"savings_display": bool}`, default ON when absent. Toggle silences the turn-end line only; ledger keeps recording; `savings report` always works.
- Cursor: `.graphite/local/stop-cursor.json`, keyed by hook `session_id`, pruned to the 20 most recent sessions.
- `savings` joins `_INFERENCE_FREE_EXTRA_COMMANDS` (rejected by the `--llm*` gate, exit 2) and must NOT appear in `capabilities` output. `_CANONICAL_COMMANDS` untouched.
- Spec-A compatibility: `stop` must be a new EVENT on `agent-hook` (never a new flag — old packages tolerate unknown events but argparse-fail on unknown flags).
- Recording rule: record only real answers — `search` only when `result.get("ok") is True`; `query`/`query-natural` only when `"error" not in result`; `context`/`impact` always (partial answers with `missing` are still answers).
- No new dependencies. Ruff clean per task. Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Commit only named files; never `git add -A`.

## File Structure

- `src/graphite/usage_ledger.py` — NEW: ledger/toggle/cursor IO + answer-file extraction (Task 1)
- `src/graphite/savings.py` — NEW: model constants, per-entry estimate, aggregation, formatting (Task 2)
- `src/graphite/cli.py` — usage recording in 4 commands (Task 3); `stop` dispatch (Task 5); `savings` subcommand (Task 6)
- `src/graphite/agent_hooks.py` — `handle_stop` (Task 4)
- `src/graphite/agent_settings.py` — Stop wiring entry (Task 5)
- `tests/test_usage_ledger.py` — NEW (Tasks 1, 3)
- `tests/test_savings.py` — NEW (Tasks 2, 6)
- `tests/test_agent_hooks.py` — stop-event tests (Tasks 4, 5)
- `tests/test_agent_settings.py` + `tests/test_init.py` — Stop-wiring assertions (Task 5)

---

### Task 1: `usage_ledger.py` — ledger, toggle, cursor

**Files:**
- Create: `src/graphite/usage_ledger.py`
- Test: `tests/test_usage_ledger.py`

**Interfaces:**
- Consumes: nothing new (stdlib + `atomic_write_text` from `graphite.io` for settings/cursor writes; the ledger itself uses plain append).
- Produces (later tasks import these exactly): `local_dir(root) -> Path`, `ledger_path(root) -> Path`, `rotated_ledger_path(root) -> Path`, `collect_answer_files(result, root) -> list[dict]`, `record_usage(root, *, cmd: str, wall_ms: int, result) -> None`, `iter_entries(root) -> Iterator[dict]`, `savings_display_enabled(root) -> bool`, `set_savings_display(root, enabled: bool) -> dict`, `read_cursor(root) -> dict`, `write_cursor(root, cursor) -> None`; constants `MAX_LEDGER_BYTES`, `MAX_FILES_PER_ENTRY`.

- [ ] **Step 0: Environment sanity** — env vars + worktree import check per Global Constraints.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_usage_ledger.py`:

```python
"""Tests for the machine-local usage ledger (never-fatal by contract)."""
from __future__ import annotations

import json
from pathlib import Path

from graphite import usage_ledger
from graphite.usage_ledger import (
    collect_answer_files,
    iter_entries,
    ledger_path,
    read_cursor,
    record_usage,
    savings_display_enabled,
    set_savings_display,
    write_cursor,
)


def _entries(root: Path) -> list[dict]:
    return list(iter_entries(root))


def test_record_usage_appends_entry_with_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n" * 100, encoding="utf-8")
    result = {"results": [{"id": "a.py", "source_file": "a.py"}], "ok": True}

    record_usage(tmp_path, cmd="search", wall_ms=42, result=result)
    entries = _entries(tmp_path)

    assert len(entries) == 1
    entry = entries[0]
    assert entry["cmd"] == "search"
    assert entry["wall_ms"] == 42
    assert entry["output_bytes"] == len(json.dumps(result))
    assert entry["files"] == [{"path": "a.py", "bytes": (tmp_path / "a.py").stat().st_size}]
    assert "ts" in entry


def test_collect_answer_files_walks_nested_and_list_keys(tmp_path: Path) -> None:
    for name in ("x.py", "y.py", "t.py"):
        (tmp_path / name).write_text("pass\n", encoding="utf-8")
    result = {
        "context": [{"file": "x.py", "neighbors": [{"source_file": "y.py"}]}],
        "impacted_files": ["x.py"],
        "likely_tests": ["t.py"],
        "missing": [],
    }

    files = collect_answer_files(result, tmp_path)

    assert [f["path"] for f in files] == ["x.py", "y.py", "t.py"]  # deduped, order-preserving


def test_collect_answer_files_caps_and_tolerates_missing(tmp_path: Path) -> None:
    result = {"impacted_files": [f"gone{i}.py" for i in range(150)]}
    files = collect_answer_files(result, tmp_path)
    assert len(files) == usage_ledger.MAX_FILES_PER_ENTRY
    assert all(f["bytes"] == 0 for f in files)  # stat failures -> 0, never raise


def test_record_usage_rotates_at_cap(tmp_path: Path, monkeypatch) -> None:
    from graphite.usage_ledger import rotated_ledger_path

    monkeypatch.setattr(usage_ledger, "MAX_LEDGER_BYTES", 200)
    for i in range(20):
        record_usage(tmp_path, cmd="query", wall_ms=i, result={"r": "x" * 50})

    assert ledger_path(tmp_path).exists()
    assert rotated_ledger_path(tmp_path).exists()
    assert _entries(tmp_path)  # reader sees rotated + current, in order


def test_iter_entries_skips_corrupt_lines(tmp_path: Path) -> None:
    record_usage(tmp_path, cmd="query", wall_ms=1, result={})
    with open(ledger_path(tmp_path), "a", encoding="utf-8") as f:
        f.write("{corrupt\n")
    record_usage(tmp_path, cmd="impact", wall_ms=2, result={})

    assert [e["cmd"] for e in _entries(tmp_path)] == ["query", "impact"]


def test_record_usage_never_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(usage_ledger, "ledger_path", lambda root: 1 / 0)
    record_usage(tmp_path, cmd="query", wall_ms=1, result={})  # must not raise


def test_savings_display_toggle_default_on(tmp_path: Path) -> None:
    assert savings_display_enabled(tmp_path) is True
    out = set_savings_display(tmp_path, False)
    assert out["savings_display"] is False
    assert savings_display_enabled(tmp_path) is False
    set_savings_display(tmp_path, True)
    assert savings_display_enabled(tmp_path) is True


def test_cursor_roundtrip_and_default(tmp_path: Path) -> None:
    assert read_cursor(tmp_path) == {}
    write_cursor(tmp_path, {"sessions": {"s1": {"offset": 10}}})
    assert read_cursor(tmp_path) == {"sessions": {"s1": {"offset": 10}}}
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\Python314\python.exe -m pytest tests/test_usage_ledger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'graphite.usage_ledger'`.

- [ ] **Step 3: Implement the module**

Create `src/graphite/usage_ledger.py`:

```python
"""Machine-local usage ledger backing the savings display.

Everything here is best-effort by contract: recording, toggling, and cursor
IO must never break the command or hook that calls them, so every public
function swallows its own errors. The ledger lives under ``.graphite/local/``,
which the standard gitignore lines already ignore (``**/.graphite/``).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .io import atomic_write_text

MAX_LEDGER_BYTES = 5 * 1024 * 1024
MAX_FILES_PER_ENTRY = 100
_FILE_KEYS = frozenset({"source_file", "file", "path"})
_FILE_LIST_KEYS = frozenset({"impacted_files", "likely_tests", "files"})


def local_dir(root: Path) -> Path:
    return root / ".graphite" / "local"


def ledger_path(root: Path) -> Path:
    return local_dir(root) / "usage.jsonl"


def _settings_path(root: Path) -> Path:
    return local_dir(root) / "settings.json"


def _cursor_path(root: Path) -> Path:
    return local_dir(root) / "stop-cursor.json"


def rotated_ledger_path(root: Path) -> Path:
    path = ledger_path(root)
    return path.parent / (path.name + ".1")


def collect_answer_files(result: Any, root: Path, cap: int = MAX_FILES_PER_ENTRY) -> list[dict[str, Any]]:
    """File paths named by an answer, with on-disk sizes; best-effort, capped."""
    seen: dict[str, None] = {}

    def _walk(value: Any) -> None:
        if len(seen) >= cap:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in _FILE_KEYS and isinstance(item, str) and item:
                    seen.setdefault(item)
                elif key in _FILE_LIST_KEYS and isinstance(item, list):
                    for path in item:
                        if isinstance(path, str) and path:
                            seen.setdefault(path)
                        if len(seen) >= cap:
                            return
                else:
                    _walk(item)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(result)
    files: list[dict[str, Any]] = []
    for path in list(seen)[:cap]:
        size = 0
        try:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = root / candidate
            size = candidate.stat().st_size
        except OSError:
            size = 0
        files.append({"path": path, "bytes": size})
    return files


def record_usage(root: Path, *, cmd: str, wall_ms: int, result: Any) -> None:
    """Append one usage record; rotate at the byte cap; never raise."""
    try:
        try:
            output_bytes = len(json.dumps(result, ensure_ascii=False))
        except (TypeError, ValueError):
            output_bytes = 0
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cmd": cmd,
            "wall_ms": int(wall_ms),
            "output_bytes": output_bytes,
            "files": collect_answer_files(result, root),
        }
        path = ledger_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_LEDGER_BYTES:
            os.replace(path, rotated_ledger_path(root))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        return


def iter_entries(root: Path) -> Iterator[dict[str, Any]]:
    """All entries, rotated generation first; corrupt lines skipped; never raises."""
    try:
        current = ledger_path(root)
        for path in (rotated_ledger_path(root), current):
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except Exception:
        return


def _read_local_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def savings_display_enabled(root: Path) -> bool:
    value = _read_local_json(_settings_path(root)).get("savings_display")
    return True if not isinstance(value, bool) else value


def set_savings_display(root: Path, enabled: bool) -> dict[str, Any]:
    settings = _read_local_json(_settings_path(root))
    settings["savings_display"] = bool(enabled)
    try:
        atomic_write_text(_settings_path(root), json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        pass
    return settings


def read_cursor(root: Path) -> dict[str, Any]:
    return _read_local_json(_cursor_path(root))


def write_cursor(root: Path, cursor: dict[str, Any]) -> None:
    try:
        atomic_write_text(_cursor_path(root), json.dumps(cursor, ensure_ascii=False) + "\n")
    except Exception:
        return
```

Note `test_record_usage_never_raises` monkeypatches `usage_ledger.ledger_path`, and `record_usage` calls it via the module namespace (it does — same module, unqualified call resolves through the module globals), so the patched version raises inside the guarded block.

- [ ] **Step 4: Run to verify pass**

Run: `C:\Python314\python.exe -m pytest tests/test_usage_ledger.py -v` — expected: 8 passed.
Run: `C:\Python314\python.exe -m ruff check src/graphite/usage_ledger.py tests/test_usage_ledger.py` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/graphite/usage_ledger.py tests/test_usage_ledger.py
git commit -m "feat(savings): machine-local usage ledger with toggle and stop cursor"
```

---

### Task 2: `savings.py` — estimator + aggregation + formatting

**Files:**
- Create: `src/graphite/savings.py`
- Test: `tests/test_savings.py`

**Interfaces:**
- Consumes: ledger entry dicts (shape from Task 1: `cmd`, `wall_ms`, `output_bytes`, `files: [{"path","bytes"}]`).
- Produces: `SavingsModel` frozen dataclass (fields `grep_tokens=1000`, `grep_seconds=20`, `read_seconds=10`, `file_token_cap=2000`), module constant `MODEL = SavingsModel()`, `estimate_entry(entry, model=MODEL) -> dict` (`{"tokens_saved": int, "seconds_saved": float}`), `summarize(entries, model=MODEL) -> dict` (`{"count", "tokens_saved", "seconds_saved", "by_cmd": {cmd: {"count","tokens_saved","seconds_saved"}}}`), `methodology(model=MODEL) -> str`, `format_compact(tokens: int, seconds: float) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_savings.py`:

```python
"""Tests for the result-scaled savings estimator."""
from __future__ import annotations

from graphite.savings import MODEL, estimate_entry, format_compact, methodology, summarize


def _entry(cmd: str = "context", files: int = 0, file_bytes: int = 4000, wall_ms: int = 100, output_bytes: int = 400) -> dict:
    return {
        "cmd": cmd,
        "wall_ms": wall_ms,
        "output_bytes": output_bytes,
        "files": [{"path": f"f{i}.py", "bytes": file_bytes} for i in range(files)],
    }


def test_zero_file_answer_still_counts_grep_round_baseline() -> None:
    est = estimate_entry(_entry(files=0, wall_ms=0, output_bytes=0))
    assert est["tokens_saved"] == MODEL.grep_tokens  # 1 round, no reads, zero cost
    assert est["seconds_saved"] == float(MODEL.grep_seconds)


def test_estimate_scales_with_files_and_caps_per_file() -> None:
    est = estimate_entry(_entry(files=12, file_bytes=100_000, wall_ms=0, output_bytes=0))
    # 12 files -> grep_rounds = 1 + 12//10 = 2; each file capped at file_token_cap
    assert est["tokens_saved"] == 2 * MODEL.grep_tokens + 12 * MODEL.file_token_cap
    assert est["seconds_saved"] == 2 * MODEL.grep_seconds + 12 * MODEL.read_seconds


def test_estimate_floors_at_zero_when_graphite_cost_exceeds_manual() -> None:
    est = estimate_entry(_entry(files=0, wall_ms=10_000_000, output_bytes=10_000_000))
    assert est["tokens_saved"] == 0
    assert est["seconds_saved"] == 0.0


def test_summarize_totals_and_by_cmd() -> None:
    entries = [_entry(cmd="context", files=2), _entry(cmd="impact", files=1), _entry(cmd="context", files=0)]
    summary = summarize(entries)
    assert summary["count"] == 3
    assert set(summary["by_cmd"]) == {"context", "impact"}
    assert summary["by_cmd"]["context"]["count"] == 2
    assert summary["tokens_saved"] == sum(v["tokens_saved"] for v in summary["by_cmd"].values())


def test_methodology_names_constants_and_estimate_label() -> None:
    text = methodology()
    for token in ("1000", "20", "10", "2000", "estimate"):
        assert token in text


def test_format_compact_units() -> None:
    assert format_compact(12_345, 250.0) == "~12.3k tokens / ~4 min"
    assert format_compact(900, 45.0) == "~900 tokens / ~45s"
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\Python314\python.exe -m pytest tests/test_savings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'graphite.savings'`.

- [ ] **Step 3: Implement the module**

Create `src/graphite/savings.py`:

```python
"""Result-scaled counterfactual estimator for the savings display.

The numbers are ESTIMATES of what equivalent manual exploration would have
cost, scaled by what each graphite answer actually contained; they are never
measurements. ``methodology()`` prints the formula and constants next to any
report so the claim stays auditable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class SavingsModel:
    grep_tokens: int = 1000
    grep_seconds: int = 20
    read_seconds: int = 10
    file_token_cap: int = 2000


MODEL = SavingsModel()


def estimate_entry(entry: dict[str, Any], model: SavingsModel = MODEL) -> dict[str, Any]:
    files = entry.get("files") or []
    count = len(files)
    grep_rounds = 1 + count // 10
    manual_tokens = grep_rounds * model.grep_tokens + sum(
        min(int(f.get("bytes", 0)) // 4, model.file_token_cap) for f in files if isinstance(f, dict)
    )
    manual_seconds = float(grep_rounds * model.grep_seconds + count * model.read_seconds)
    cost_tokens = int(entry.get("output_bytes", 0)) // 4
    cost_seconds = float(entry.get("wall_ms", 0)) / 1000.0
    return {
        "tokens_saved": max(0, manual_tokens - cost_tokens),
        "seconds_saved": max(0.0, manual_seconds - cost_seconds),
    }


def summarize(entries: Iterable[dict[str, Any]], model: SavingsModel = MODEL) -> dict[str, Any]:
    total_tokens = 0
    total_seconds = 0.0
    count = 0
    by_cmd: dict[str, dict[str, Any]] = {}
    for entry in entries:
        est = estimate_entry(entry, model)
        cmd = str(entry.get("cmd", "unknown"))
        bucket = by_cmd.setdefault(cmd, {"count": 0, "tokens_saved": 0, "seconds_saved": 0.0})
        bucket["count"] += 1
        bucket["tokens_saved"] += est["tokens_saved"]
        bucket["seconds_saved"] += est["seconds_saved"]
        total_tokens += est["tokens_saved"]
        total_seconds += est["seconds_saved"]
        count += 1
    return {
        "count": count,
        "tokens_saved": total_tokens,
        "seconds_saved": total_seconds,
        "by_cmd": by_cmd,
    }


def methodology(model: SavingsModel = MODEL) -> str:
    return (
        "All figures are estimates of avoided manual exploration, never measurements. "
        f"Model: an answer covering K files ~ (1 + K//10) grep rounds x {model.grep_tokens} tokens "
        f"/ {model.grep_seconds}s each, plus per-file read of min(bytes/4, {model.file_token_cap}) tokens "
        f"/ {model.read_seconds}s; minus graphite's actual cost (output bytes/4 tokens, measured wall time); "
        "floored at zero."
    )


def format_compact(tokens: int, seconds: float) -> str:
    token_text = f"~{tokens / 1000:.1f}k tokens" if tokens >= 1000 else f"~{tokens} tokens"
    time_text = f"~{seconds / 60:.0f} min" if seconds >= 90 else f"~{seconds:.0f}s"
    return f"{token_text} / {time_text}"
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\Python314\python.exe -m pytest tests/test_savings.py -v` — expected: 6 passed.
Run: `C:\Python314\python.exe -m ruff check src/graphite/savings.py tests/test_savings.py` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/graphite/savings.py tests/test_savings.py
git commit -m "feat(savings): result-scaled estimator with auditable methodology"
```

---

### Task 3: usage recording in the canonical read commands

**Files:**
- Modify: `src/graphite/cli.py` — `cmd_query` (:702-724), `cmd_search` (:727-742), `cmd_impact` (:815-831), `cmd_context` (:932-939), plus one new helper
- Test: `tests/test_usage_ledger.py`

**Interfaces:**
- Consumes: `usage_ledger.record_usage` (Task 1).
- Produces: helper `_record_canonical_usage(cmd: str, result: Any, started: float) -> None` in cli.py; ledger entries with `cmd` values `"query"`, `"query-natural"`, `"search"`, `"context"`, `"impact"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_usage_ledger.py`:

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


def test_canonical_commands_record_usage(built_repo: Path, capsys) -> None:
    from graphite.cli import main

    assert main(["search", "target_symbol"]) == 0
    assert main(["query", "callers target_symbol"]) == 0
    assert main(["context", "alpha.py"]) == 0
    assert main(["impact", "alpha.py"]) == 0
    capsys.readouterr()

    cmds = [e["cmd"] for e in _entries(built_repo)]
    assert cmds == ["search", "query", "context", "impact"]
    assert all(e["wall_ms"] >= 0 for e in _entries(built_repo))


def test_natural_query_records_as_query_natural(built_repo: Path, capsys) -> None:
    from graphite.cli import main

    assert main(["query", "--natural", "who calls target_symbol?"]) == 0
    capsys.readouterr()

    assert [e["cmd"] for e in _entries(built_repo)] == ["query-natural"]


def test_failed_search_is_not_recorded(built_repo: Path, capsys) -> None:
    from graphite.cli import main

    main(["search", "   "])  # empty search -> ok False
    capsys.readouterr()

    assert _entries(built_repo) == []


def test_ledger_failure_never_breaks_command(built_repo: Path, capsys, monkeypatch) -> None:
    from graphite.cli import main

    def boom(*args, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr("graphite.usage_ledger.record_usage", boom)
    assert main(["search", "target_symbol"]) == 0
    out = capsys.readouterr()
    assert "target_symbol" in out.out
    assert "ledger down" not in out.out + out.err
```

Note for the natural test: if `"who calls target_symbol?"` is not in the deterministic grammar and falls back without a graph answer, pick a grammar-matching phrasing instead — run `C:\Python314\python.exe -m graphite capabilities --json` and use a template from `natural_language.intents` that produces a plan; the assertion stays the same. Say in the report which phrasing you used.

- [ ] **Step 2: Run to verify failure**

Run: `C:\Python314\python.exe -m pytest tests/test_usage_ledger.py -v`
Expected: the 4 new tests FAIL (no ledger entries recorded); Task 1's 8 still pass.

- [ ] **Step 3: Implement the wiring**

In `src/graphite/cli.py` add near `_load_graph`:

```python
def _record_canonical_usage(cmd: str, result: Any, started: float) -> None:
    """Best-effort usage recording for the savings display; never fatal."""
    try:
        from . import usage_ledger

        usage_ledger.record_usage(
            Path.cwd(),
            cmd=cmd,
            wall_ms=int((time.perf_counter() - started) * 1000),
            result=result,
        )
    except Exception:
        return
```

(`import time` at the top of cli.py if not already imported.) Then:

- `cmd_query`: `started = time.perf_counter()` as the FIRST line. In the natural branch, capture the answer before printing: `result = answer_natural(g, args.query)` / `print(json.dumps(result, ...))` / `if "error" not in result: _record_canonical_usage("query-natural", result, started)` / `return 0`. The translated-only / plan-only early returns record NOTHING. In the structured path, after the existing `print(...)`: `if "error" not in result: _record_canonical_usage("query", result, started)`.
- `cmd_search`: `started = time.perf_counter()` first; after the output branches, before `return 0`: `if result.get("ok"): _record_canonical_usage("search", result, started)`.
- `cmd_context`: `started` first; before the return: `_record_canonical_usage("context", result, started)`.
- `cmd_impact`: `started` first; before the return: `_record_canonical_usage("impact", result, started)`.

- [ ] **Step 4: Run to verify pass**

Run: `C:\Python314\python.exe -m pytest tests/test_usage_ledger.py -v` — expected: 12 passed.
Run: `C:\Python314\python.exe -m ruff check src/graphite/cli.py tests/test_usage_ledger.py` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/graphite/cli.py tests/test_usage_ledger.py
git commit -m "feat(savings): record canonical read-command usage in the local ledger"
```

---

### Task 4: `handle_stop` — turn-end summary handler

**Files:**
- Modify: `src/graphite/agent_hooks.py`
- Test: `tests/test_agent_hooks.py`

**Interfaces:**
- Consumes: `usage_ledger` (ledger/cursor/toggle) and `savings` (estimate/format) from Tasks 1-2; `_payload_root` from Spec A.
- Produces: `handle_stop(payload: dict) -> dict | None` returning `{"systemMessage": "graphite: est. <turn> saved this turn (session: <totals>) [estimates]"}` or None; constant `MAX_CURSOR_SESSIONS = 20`. Task 5's CLI dispatches to it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent_hooks.py`:

```python
from graphite.agent_hooks import handle_stop
from graphite.usage_ledger import record_usage, set_savings_display


def _stop_payload(root: Path, session: str = "s1") -> dict:
    return {"session_id": session, "cwd": str(root), "hook_event_name": "Stop"}


def _use_graphite(root: Path, cmd: str = "context") -> None:
    record_usage(root, cmd=cmd, wall_ms=50, result={"files": [{"path": "alpha.py"}]})


def test_stop_emits_summary_after_usage(built_repo: Path) -> None:
    _use_graphite(built_repo)
    out = handle_stop(_stop_payload(built_repo))
    message = out["systemMessage"]
    assert message.startswith("graphite: est. ")
    assert "saved this turn" in message
    assert "[estimates]" in message


def test_stop_silent_with_no_new_usage(built_repo: Path) -> None:
    _use_graphite(built_repo)
    assert handle_stop(_stop_payload(built_repo)) is not None  # consumes entries
    assert handle_stop(_stop_payload(built_repo)) is None  # nothing new this turn


def test_stop_session_totals_accumulate(built_repo: Path) -> None:
    _use_graphite(built_repo)
    first = handle_stop(_stop_payload(built_repo))["systemMessage"]
    _use_graphite(built_repo)
    second = handle_stop(_stop_payload(built_repo))["systemMessage"]
    assert "session:" in first and "session:" in second
    assert first.split("session:")[1] != second.split("session:")[1]  # totals grew


def test_stop_respects_toggle_but_cursor_still_advances(built_repo: Path) -> None:
    set_savings_display(built_repo, False)
    _use_graphite(built_repo)
    assert handle_stop(_stop_payload(built_repo)) is None
    set_savings_display(built_repo, True)
    assert handle_stop(_stop_payload(built_repo)) is None  # toggle-off turn already consumed


def test_stop_without_session_id_is_silent(built_repo: Path) -> None:
    _use_graphite(built_repo)
    assert handle_stop({"cwd": str(built_repo)}) is None


def test_stop_survives_ledger_rotation_between_turns(built_repo: Path, monkeypatch) -> None:
    from graphite import usage_ledger as ul

    _use_graphite(built_repo)
    assert handle_stop(_stop_payload(built_repo)) is not None
    monkeypatch.setattr(ul, "MAX_LEDGER_BYTES", 1)  # force rotation on the next record
    _use_graphite(built_repo)  # rotates the consumed generation away; fresh file, offset resync
    assert handle_stop(_stop_payload(built_repo)) is not None


def test_stop_prunes_cursor_to_max_sessions(built_repo: Path) -> None:
    from graphite.agent_hooks import MAX_CURSOR_SESSIONS
    from graphite.usage_ledger import read_cursor

    for i in range(MAX_CURSOR_SESSIONS + 5):
        _use_graphite(built_repo)
        handle_stop(_stop_payload(built_repo, session=f"s{i}"))

    sessions = read_cursor(built_repo)["sessions"]
    assert len(sessions) == MAX_CURSOR_SESSIONS
    assert "s0" not in sessions  # oldest pruned
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\Python314\python.exe -m pytest tests/test_agent_hooks.py -v`
Expected: new tests FAIL with `ImportError: cannot import name 'handle_stop'`; the pre-existing 21 pass.

- [ ] **Step 3: Implement `handle_stop`**

Append to `src/graphite/agent_hooks.py` (imports at top: `from . import savings as savings_model`, `from . import usage_ledger`):

```python
MAX_CURSOR_SESSIONS = 20


def _entries_between(path: Path, start: int, end: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with open(path, "rb") as handle:
        handle.seek(start)
        blob = handle.read(max(0, end - start))
    for line in blob.decode("utf-8", errors="replace").splitlines():
        try:
            import json

            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def handle_stop(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Turn-end savings summary; silent unless graphite was used this turn."""
    try:
        root = _payload_root(payload)
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            return None
        ledger = usage_ledger.ledger_path(root)
        size = ledger.stat().st_size if ledger.is_file() else 0
        cursor = usage_ledger.read_cursor(root)
        sessions = cursor.get("sessions")
        if not isinstance(sessions, dict):
            sessions = {}
        state = sessions.pop(session_id, None)
        if not isinstance(state, dict):
            state = {"offset": 0, "tokens": 0, "seconds": 0.0}
        offset = state.get("offset", 0)
        if not isinstance(offset, int) or offset > size:
            offset = 0  # ledger rotated or cursor damaged; resync
        new_entries = _entries_between(ledger, offset, size) if size > offset else []
        turn_tokens = 0
        turn_seconds = 0.0
        for entry in new_entries:
            est = savings_model.estimate_entry(entry)
            turn_tokens += est["tokens_saved"]
            turn_seconds += est["seconds_saved"]
        state["offset"] = size
        state["tokens"] = int(state.get("tokens", 0)) + turn_tokens
        state["seconds"] = float(state.get("seconds", 0.0)) + turn_seconds
        sessions[session_id] = state  # re-insert -> newest position
        while len(sessions) > MAX_CURSOR_SESSIONS:
            sessions.pop(next(iter(sessions)))
        usage_ledger.write_cursor(root, {"sessions": sessions})
        if not new_entries or not usage_ledger.savings_display_enabled(root):
            return None
        turn_text = savings_model.format_compact(turn_tokens, turn_seconds)
        session_text = savings_model.format_compact(state["tokens"], state["seconds"])
        return {
            "systemMessage": (
                f"graphite: est. {turn_text} saved this turn "
                f"(session: {session_text}) [estimates]"
            )
        }
    except Exception:
        return None
```

Move the `import json` to the top of `agent_hooks.py` instead of inline in `_entries_between` (the inline form in the sketch above is a reminder that json is now needed — top-level is the correct placement; add it to the existing import block).

- [ ] **Step 4: Run to verify pass**

Run: `C:\Python314\python.exe -m pytest tests/test_agent_hooks.py -v` — expected: 28 passed (21 pre-existing + 7 new).
Run: `C:\Python314\python.exe -m ruff check src/graphite/agent_hooks.py tests/test_agent_hooks.py` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/graphite/agent_hooks.py tests/test_agent_hooks.py
git commit -m "feat(savings): stop-hook turn-end summary with per-session cursor"
```

---

### Task 5: CLI `stop` dispatch + Stop-hook wiring in the installer

**Files:**
- Modify: `src/graphite/cli.py` (`cmd_agent_hook` dispatch), `src/graphite/agent_settings.py` (`desired` dict + constant)
- Test: `tests/test_agent_hooks.py`, `tests/test_agent_settings.py`, `tests/test_init.py`

**Interfaces:**
- Consumes: `handle_stop` (Task 4).
- Produces: `python -m graphite agent-hook stop` live end-to-end; installer writes the third event `Stop` → `python -m graphite agent-hook stop`; constant `_STOP_COMMAND` in agent_settings.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent_hooks.py`:

```python
def test_cli_stop_event_emits_summary(built_repo, monkeypatch, capsys) -> None:
    _use_graphite(built_repo)
    code, out = _run_cli_hook(
        monkeypatch, capsys, ["agent-hook", "stop"], _stop_payload(built_repo)
    )
    assert code == 0
    assert json.loads(out)["systemMessage"].startswith("graphite: est. ")
```

NOTE: Spec A added `test_cli_unknown_event_is_a_silent_noop` using the event name `stop` as its unknown example. Change that test's event to `"future-event"` (assertions unchanged) — `stop` is now a real event; the unknown-event tolerance still needs covering, just with a name that stays unknown.

Append to `tests/test_agent_settings.py`:

```python
def test_fresh_install_wires_stop_hook(tmp_path: Path) -> None:
    ensure_claude_settings(tmp_path)
    settings = _settings(tmp_path)
    assert _commands(settings, "Stop") == ["python -m graphite agent-hook stop"]


def test_spec_a_shaped_settings_gain_stop_on_reinit(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Grep|Glob",
                            "hooks": [{"type": "command", "command": "python -m graphite agent-hook pre-tool-use --mode strict"}],
                        }
                    ],
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "python -m graphite agent-hook session-start"}]}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    result = ensure_claude_settings(tmp_path)
    settings = _settings(tmp_path)

    assert result["changed"] is True
    assert result["mode"] == "strict"  # preserved across the upgrade
    assert _commands(settings, "Stop") == ["python -m graphite agent-hook stop"]
    assert "--mode strict" in _commands(settings, "PreToolUse")[0]
```

Append to `tests/test_init.py`:

```python
def test_init_wires_stop_hook(tmp_path: Path) -> None:
    init_project(tmp_path, platforms=["claude"])
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = [h["command"] for entry in settings["hooks"]["Stop"] for h in entry["hooks"]]
    assert commands == ["python -m graphite agent-hook stop"]
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\Python314\python.exe -m pytest tests/test_agent_hooks.py tests/test_agent_settings.py tests/test_init.py -v`
Expected: the new tests FAIL (unknown event is silent → no systemMessage; installer writes only two events). Everything pre-existing passes (after the `future-event` rename).

- [ ] **Step 3: Implement**

`src/graphite/cli.py`, `cmd_agent_hook` — extend the explicit dispatch (import `handle_stop` alongside the others inside the try):

```python
        if args.event == "session-start":
            out = handle_session_start(payload)
        elif args.event == "pre-tool-use":
            out = handle_pre_tool_use(payload, args.mode)
        elif args.event == "stop":
            out = handle_stop(payload)
        else:
            return 0
```

Also mention `stop` in the `event` argument's help text.

`src/graphite/agent_settings.py` — add beside `_SESSION_START_COMMAND`:

```python
_STOP_COMMAND = f"{HOOK_COMMAND_PREFIX} stop"
```

and extend `desired` in `ensure_claude_settings`:

```python
        "Stop": {
            "hooks": [{"type": "command", "command": _STOP_COMMAND}],
        },
```

(The existing strip-and-append merge makes Spec-A-shaped files upgrade in place; no other installer change needed.)

- [ ] **Step 4: Run to verify pass**

Run: `C:\Python314\python.exe -m pytest tests/test_agent_hooks.py tests/test_agent_settings.py tests/test_init.py -v` — expected: ALL PASS.
Run: `C:\Python314\python.exe -m ruff check src/graphite/cli.py src/graphite/agent_settings.py tests/test_agent_hooks.py tests/test_agent_settings.py tests/test_init.py` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/graphite/cli.py src/graphite/agent_settings.py tests/test_agent_hooks.py tests/test_agent_settings.py tests/test_init.py
git commit -m "feat(savings): stop event dispatch and Stop-hook wiring in the installer"
```

---

### Task 6: `graphite savings` subcommand

**Files:**
- Modify: `src/graphite/cli.py` — `cmd_savings`, parser entry (after the `agent-hook` block), `_INFERENCE_FREE_EXTRA_COMMANDS`
- Test: `tests/test_savings.py`

**Interfaces:**
- Consumes: `usage_ledger.iter_entries` / toggle functions (Task 1), `savings.summarize` / `methodology` / `format_compact` (Task 2).
- Produces: `python -m graphite savings [report|on|off|status] [--json]`; `savings` in `_INFERENCE_FREE_EXTRA_COMMANDS` (LLM-gated, absent from capabilities).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_savings.py`:

```python
import json as _json
from pathlib import Path

from graphite.cli import main
from graphite.usage_ledger import record_usage


def _seed(root: Path) -> None:
    record_usage(root, cmd="context", wall_ms=50, result={"files": [{"path": "a.py"}]})
    record_usage(root, cmd="impact", wall_ms=30, result={"impacted_files": ["a.py"]})


def test_savings_report_json(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)

    assert main(["savings", "--json"]) == 0
    payload = _json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert payload["all_time"]["count"] == 2
    assert set(payload["all_time"]["by_cmd"]) == {"context", "impact"}
    assert payload["today"]["count"] == 2
    assert "estimate" in payload["methodology"]
    assert payload["savings_display"] is True


def test_savings_report_human_has_methodology(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)

    assert main(["savings"]) == 0
    out = capsys.readouterr().out

    assert "estimates" in out.lower()
    assert "all-time" in out.lower()


def test_savings_toggle_roundtrip(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["savings", "off"]) == 0
    capsys.readouterr()
    assert main(["savings", "status", "--json"]) == 0
    status = _json.loads(capsys.readouterr().out)
    assert status["savings_display"] is False
    assert main(["savings", "on"]) == 0


def test_savings_report_works_when_toggle_off(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)
    main(["savings", "off"])
    capsys.readouterr()

    assert main(["savings", "--json"]) == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["all_time"]["count"] == 2


def test_savings_rejects_llm_flags(capsys) -> None:
    assert main(["--llm", "cloud", "savings"]) == 2


def test_capabilities_does_not_list_savings(capsys) -> None:
    assert main(["capabilities", "--json"]) == 0
    payload = _json.loads(capsys.readouterr().out)
    assert "savings" not in payload["commands"]
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\Python314\python.exe -m pytest tests/test_savings.py -v`
Expected: new tests FAIL (`invalid choice: 'savings'`); capabilities test passes already.

- [ ] **Step 3: Implement**

`src/graphite/cli.py`:

Extend the frozenset (Spec A created it):

```python
_INFERENCE_FREE_EXTRA_COMMANDS = frozenset({"agent-hook", "savings"})
```

Add the command (near `cmd_capabilities`):

```python
def cmd_savings(args: argparse.Namespace) -> int:
    from . import savings as savings_model
    from . import usage_ledger

    root = Path.cwd()
    if args.action in ("on", "off"):
        usage_ledger.set_savings_display(root, args.action == "on")
    if args.action in ("on", "off", "status"):
        payload = {"ok": True, "savings_display": usage_ledger.savings_display_enabled(root)}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            state = "on" if payload["savings_display"] else "off"
            print(f"[graphite] savings display: {state}")
        return 0

    entries = list(usage_ledger.iter_entries(root))
    today = datetime.now().astimezone().date().isoformat()
    today_entries = [e for e in entries if str(e.get("ts", "")).startswith(today)]
    payload = {
        "ok": True,
        "schema_version": 1,
        "all_time": savings_model.summarize(entries),
        "today": savings_model.summarize(today_entries),
        "savings_display": usage_ledger.savings_display_enabled(root),
        "methodology": savings_model.methodology(),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    for label, summary in (("today", payload["today"]), ("all-time", payload["all_time"])):
        compact = savings_model.format_compact(summary["tokens_saved"], summary["seconds_saved"])
        print(f"[graphite] {label}: est. {compact} saved across {summary['count']} graphite answers")
        for cmd_name, bucket in sorted(summary["by_cmd"].items()):
            bucket_compact = savings_model.format_compact(bucket["tokens_saved"], bucket["seconds_saved"])
            print(f"  - {cmd_name}: {bucket['count']} calls, est. {bucket_compact}")
    print(f"[graphite] methodology: {payload['methodology']}")
    return 0
```

(`from datetime import datetime` at the top of cli.py if not already there — check; `timezone` not needed here.)

Parser (after the `p_agent_hook` block):

```python
    p_savings = sub.add_parser(
        "savings",
        help="Estimated time/token savings from graphite usage in this repo (local estimates; on/off toggles the turn-end display)",
    )
    p_savings.add_argument("action", nargs="?", choices=["report", "on", "off", "status"], default="report", help="report (default), or toggle the turn-end display")
    p_savings.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_savings.set_defaults(func=cmd_savings)
```

- [ ] **Step 4: Run to verify pass**

Run: `C:\Python314\python.exe -m pytest tests/test_savings.py -v` — expected: 12 passed.
Run: `C:\Python314\python.exe -m ruff check src/graphite/cli.py tests/test_savings.py` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/graphite/cli.py tests/test_savings.py
git commit -m "feat(cli): graphite savings report and per-repo display toggle"
```

---

### Task 7: Full-suite + lint gate

**Files:** none new.

- [ ] **Step 1: Full suite** — `C:\Python314\python.exe -m pytest -q` (env per Global Constraints; 10-min timeout). Expected: 0 failures (Spec A baseline on this branch: 2014 passed / 44 skipped; Spec B adds ~26 tests). Fix anything red before proceeding; report failures honestly.
- [ ] **Step 2: Ruff** — `C:\Python314\python.exe -m ruff check .` from the worktree root. Expected: clean.
- [ ] **Step 3: Commit only if fixes were needed** — `chore: suite + lint fixes for savings display`.

---

## Out of scope for this plan

- Merge to main, consumer-repo rollout, and global-hook retirement (spec A Change 4 + spec B Change 4) — operator-gated, after both specs are reviewed on the branch.
- No published JSON schema for the savings report (spec B non-goal, v1).
