# Honest Answer Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every canonical graph answer carries an `answer` block (scoped health, grade, caveats, empty-meaning); the Python resolver binds `from pkg import submodule` correctly; `imported_by`/`depends_on` entries carry `source_file`.

**Architecture:** One new module `src/graphite/answer_contract.py` (caveat registry + `build_answer_block`) wired at exactly three seams: `execute_plan` (query.py — all verbs incl. `--natural`), `_impact` (cli.py), and the context builder (context.py). Resolver fix adds submodule import edges at the existing emission site in `extract/ast.py`, gated by cache v8.

**Tech Stack:** Python 3.11+, networkx, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-26-honest-answer-contract-design.md` (commit 7c929e2). Read it before starting your task.

## Global Constraints

- Health threshold is the existing `RESOLUTION_HEALTHY_RATIO = 0.8` (`src/graphite/health.py:15`) — never a new constant.
- Fail-open (spec R6): `build_answer_block` returns `None` on ANY internal failure; callers omit the `answer` key; no query may error or change its primary result because the block could not be computed.
- Healthy non-empty human output stays byte-identical to today (spec R2).
- `cmd_query` output is pure JSON today and stays pure JSON — the `answer` block IS its disclosure; never print prose around it.
- Grade strings exactly: `"decision_grade"`, `"advisory"`, `"inconclusive"`. Human rendering replaces `_` with `-`.
- Legacy `inconclusive` keys are kept, derivation upgraded to `grade == "inconclusive"` only where a block was built; when the block is `None` the legacy expression stands.
- `stats` gets NO answer block; `search` untouched.
- Cache version: `"v7"` → `"v8"` at `src/graphite/config.py:32` AND `:159` (Task 4 only).
- `DOC_VERSION`: 8 → 9 at `src/graphite/init.py:17` (Task 5 only).
- Worktree: `F:/tmp/graphite-honest-answer`. Tests run as `PYTHONPATH="F:/tmp/graphite-honest-answer/src" python -m pytest <file> -q` (bash, forward slashes). Redirect suite output to a file and read `$?` directly — never pipe to `tail`.
- Commits end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `answer_contract.py` module

**Files:**
- Create: `src/graphite/answer_contract.py`
- Test: `tests/test_answer_contract.py` (new)

**Interfaces:**
- Consumes: `resolution_health`, `RESOLUTION_HEALTHY_RATIO`, `_edge_language` from `graphite.health` (private import within the package is deliberate — DRY over visibility).
- Produces (later tasks rely on these exact names):
  - `ANSWER_SCHEMA = 1`
  - `GRADE_DECISION = "decision_grade"`, `GRADE_ADVISORY = "advisory"`, `GRADE_INCONCLUSIVE = "inconclusive"`
  - `CAVEAT_REGISTRY: tuple[dict[str, Any], ...]`
  - `active_caveats() -> list[dict[str, Any]]` — entries without `retired_by`, published shape (all fields).
  - `languages_for_nodes(g, node_ids) -> list[str]` — sorted unique languages of the nodes' `source_file`s, `"other"` filtered out; `[]` when none derivable.
  - `build_answer_block(g, *, relations, languages, total, empty_meaning=None) -> dict | None`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the answer-scoped confidence contract (spec 2026-07-26)."""
import networkx as nx
import pytest

from graphite.answer_contract import (
    ANSWER_SCHEMA,
    CAVEAT_REGISTRY,
    GRADE_ADVISORY,
    GRADE_DECISION,
    GRADE_INCONCLUSIVE,
    active_caveats,
    build_answer_block,
    languages_for_nodes,
)


def _graph(*, py_calls_bound=9, py_calls_unbound=1, ts_calls_bound=1, ts_calls_unbound=9):
    """Graph with tunable per-language calls health (imports left empty)."""
    g = nx.DiGraph()
    g.add_node("caller_py", kind="function", source_file="a.py")
    g.add_node("caller_ts", kind="function", source_file="a.ts")
    g.add_node("bound", kind="function", source_file="b.py")
    g.add_node("phantom", kind="unknown")
    for i in range(py_calls_bound):
        g.add_edge("caller_py", "bound", relation="calls", source_file="a.py", key=i)
    for i in range(py_calls_unbound):
        g.add_edge("caller_py", "phantom", relation="calls", source_file="a.py")
    for i in range(ts_calls_bound):
        g.add_edge("caller_ts", "bound", relation="calls", source_file="a.ts")
    for i in range(ts_calls_unbound):
        g.add_edge("caller_ts", "phantom", relation="calls", source_file="a.ts")
    return g


# NOTE: nx.DiGraph collapses parallel edges; the loops above produce ONE
# edge per (u, v) pair. Build distinct phantom targets instead:
def _graph_ratio(lang_ext, bound_n, unbound_n):
    g = nx.DiGraph()
    src = f"caller{lang_ext.replace('.', '_')}"
    g.add_node(src, kind="function", source_file=f"a{lang_ext}")
    for i in range(bound_n):
        t = f"bound{i}"
        g.add_node(t, kind="function", source_file=f"b{lang_ext}")
        g.add_edge(src, t, relation="calls", source_file=f"a{lang_ext}")
    for i in range(unbound_n):
        t = f"phantom{i}"
        g.add_node(t, kind="unknown")
        g.add_edge(src, t, relation="calls", source_file=f"a{lang_ext}")
    return g


def _merged(g1, g2):
    return nx.compose(g1, g2)


def test_grade_decision_when_all_cells_healthy():
    g = _graph_ratio(".py", 9, 1)  # python calls 0.9 >= 0.8
    block = build_answer_block(g, relations=("calls",), languages=["python"], total=5)
    assert block["schema"] == ANSWER_SCHEMA
    assert block["grade"] == GRADE_DECISION
    assert block["health"]["calls"]["python"]["healthy"] is True
    assert "empty_meaning" not in block


def test_grade_advisory_when_degraded_and_nonempty():
    g = _graph_ratio(".ts", 1, 9)  # typescript calls 0.1 < 0.8
    block = build_answer_block(g, relations=("calls",), languages=["typescript"], total=3)
    assert block["grade"] == GRADE_ADVISORY


def test_grade_inconclusive_when_degraded_and_empty():
    g = _graph_ratio(".ts", 1, 9)
    block = build_answer_block(
        g, relations=("calls",), languages=["typescript"], total=0,
        empty_meaning="no bound callers found",
    )
    assert block["grade"] == GRADE_INCONCLUSIVE
    assert block["empty_meaning"] == "no bound callers found"


def test_scoped_cells_ignore_other_languages():
    """The firescraper regression: healthy python must not mask degraded ts."""
    g = _merged(_graph_ratio(".py", 9, 1), _graph_ratio(".ts", 1, 9))
    block = build_answer_block(g, relations=("calls",), languages=["typescript"], total=0)
    assert block["grade"] == GRADE_INCONCLUSIVE
    assert "python" not in block["health"]["calls"]


def test_language_fallback_is_graph_wide():
    g = _merged(_graph_ratio(".py", 9, 1), _graph_ratio(".ts", 1, 9))
    block = build_answer_block(g, relations=("calls",), languages=[], total=1)
    assert set(block["languages"]) == {"python", "typescript"}
    assert block["grade"] == GRADE_ADVISORY  # ts cell degraded


def test_missing_cells_are_omitted_and_do_not_degrade():
    g = _graph_ratio(".py", 9, 1)  # no imports edges at all
    block = build_answer_block(g, relations=("calls", "imports"), languages=["python"], total=1)
    assert "imports" not in block["health"] or block["health"]["imports"] == {}
    assert block["grade"] == GRADE_DECISION


def test_caveat_filtering_by_relation_and_language():
    g = _graph_ratio(".py", 9, 1)
    block = build_answer_block(g, relations=("calls",), languages=["python"], total=1)
    codes = {c["code"] for c in block["caveats"]}
    assert "python-dynamic-dispatch" in codes
    assert "ts-external-calls-unclassified" not in codes
    imports_block = build_answer_block(g, relations=("imports",), languages=["python"], total=1)
    assert imports_block["caveats"] == []


def test_caveats_project_only_code_and_summary():
    g = _graph_ratio(".py", 9, 1)
    block = build_answer_block(g, relations=("calls",), languages=["python"], total=1)
    assert set(block["caveats"][0].keys()) == {"code", "summary"}


def test_retired_caveats_never_emitted(monkeypatch):
    import graphite.answer_contract as ac
    retired = {
        "code": "test-retired", "relations": ("calls",), "languages": ("python",),
        "summary": "x", "since": "2026-07-26", "retired_by": "v8",
    }
    monkeypatch.setattr(ac, "CAVEAT_REGISTRY", (*ac.CAVEAT_REGISTRY, retired))
    assert all(e["code"] != "test-retired" for e in ac.active_caveats())
    g = _graph_ratio(".py", 9, 1)
    block = ac.build_answer_block(g, relations=("calls",), languages=["python"], total=1)
    assert all(c["code"] != "test-retired" for c in block["caveats"])


def test_registry_initial_entries():
    codes = {e["code"] for e in active_caveats()}
    assert codes == {"python-dynamic-dispatch", "ts-external-calls-unclassified"}


def test_fail_open_returns_none(monkeypatch):
    import graphite.answer_contract as ac
    def boom(_g):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(ac, "resolution_health", boom)
    g = _graph_ratio(".py", 1, 0)
    assert build_answer_block(g, relations=("calls",), languages=["python"], total=1) is None


def test_languages_for_nodes():
    g = _merged(_graph_ratio(".py", 1, 0), _graph_ratio(".ts", 1, 0))
    assert languages_for_nodes(g, ["caller_py", "caller_ts"]) == ["python", "typescript"]
    assert languages_for_nodes(g, ["phantom0"]) == []
    assert languages_for_nodes(g, ["no-such-node"]) == []
```

Delete the unused first `_graph` helper before committing (the `_graph_ratio` note explains why it exists in this plan: parallel-edge collapse is a real DiGraph pitfall — do not reintroduce it).

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="F:/tmp/graphite-honest-answer/src" python -m pytest tests/test_answer_contract.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphite.answer_contract'`

- [ ] **Step 3: Write the module**

```python
"""Answer-scoped confidence contract (spec 2026-07-26).

Every canonical graph answer carries an `answer` block: the relations the
verb walked, the languages in scope, per-relation per-language health
cells, a derived grade, applicable caveat codes, and — when the primary
result is empty — what the emptiness means.

Fail-open: build_answer_block returns None on any internal failure and
callers omit the key; the block may be dropped, never wrong.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

import networkx as nx

from .health import RESOLUTION_HEALTHY_RATIO, _edge_language, resolution_health

ANSWER_SCHEMA = 1

GRADE_DECISION = "decision_grade"
GRADE_ADVISORY = "advisory"
GRADE_INCONCLUSIVE = "inconclusive"

# Confirmed blindspot classes. Process rule (spec §5): a confirmed class
# gets an entry the day it is confirmed, decoupled from its fix; fixed
# classes get retired_by and are never emitted again.
CAVEAT_REGISTRY: tuple[dict[str, Any], ...] = (
    {
        "code": "python-dynamic-dispatch",
        "relations": ("calls",),
        "languages": ("python",),
        "summary": "dynamically dispatched calls (getattr, decorator rebinding) are not modeled",
        "since": "2026-07-26",
    },
    {
        "code": "ts-external-calls-unclassified",
        "relations": ("calls",),
        "languages": ("typescript", "javascript"),
        "summary": "calls to external-package symbols, runtime globals, and destructured locals count as unbound",
        "since": "2026-07-26",
    },
)


def active_caveats() -> list[dict[str, Any]]:
    """Registry entries that are live (no retired_by), full published shape."""
    return [dict(e) for e in CAVEAT_REGISTRY if not e.get("retired_by")]


def languages_for_nodes(g: nx.DiGraph, node_ids: Iterable[str]) -> list[str]:
    """Sorted unique languages of the nodes' source files ('other' dropped)."""
    langs: set[str] = set()
    for node_id in node_ids:
        if node_id is None or node_id not in g:
            continue
        language = _edge_language(g.nodes[node_id].get("source_file"))
        if language != "other":
            langs.add(language)
    return sorted(langs)


def build_answer_block(
    g: nx.DiGraph,
    *,
    relations: Sequence[str],
    languages: Sequence[str] | None,
    total: int,
    empty_meaning: str | None = None,
) -> dict[str, Any] | None:
    """The `answer` block for one graph answer, or None (fail-open)."""
    try:
        if not relations:
            return None
        health = resolution_health(g)
        by_language = health.get("by_language") or {}
        threshold = health.get("threshold", RESOLUTION_HEALTHY_RATIO)
        langs = sorted(languages) if languages else sorted(by_language)
        cells: dict[str, dict[str, dict[str, Any]]] = {}
        degraded = False
        for relation in relations:
            for language in langs:
                cell = (by_language.get(language) or {}).get(relation)
                if not cell or cell.get("ratio") is None:
                    continue
                healthy = cell["ratio"] >= threshold
                degraded = degraded or not healthy
                cells.setdefault(relation, {})[language] = {**cell, "healthy": healthy}
        empty = total == 0
        if degraded:
            grade = GRADE_INCONCLUSIVE if empty else GRADE_ADVISORY
        else:
            grade = GRADE_DECISION
        relation_set = set(relations)
        language_set = set(langs)
        caveats = [
            {"code": e["code"], "summary": e["summary"]}
            for e in CAVEAT_REGISTRY
            if not e.get("retired_by")
            and relation_set.intersection(e["relations"])
            and language_set.intersection(e["languages"])
        ]
        block: dict[str, Any] = {
            "schema": ANSWER_SCHEMA,
            "relations": sorted(relation_set),
            "languages": langs,
            "health": cells,
            "grade": grade,
            "caveats": caveats,
        }
        if empty and empty_meaning:
            block["empty_meaning"] = empty_meaning
        return block
    except Exception:
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="F:/tmp/graphite-honest-answer/src" python -m pytest tests/test_answer_contract.py -q`
Expected: all PASS. Also run `python -m ruff check src/graphite/answer_contract.py tests/test_answer_contract.py`.

- [ ] **Step 5: Commit**

```bash
git add src/graphite/answer_contract.py tests/test_answer_contract.py
git commit -m "feat(answer): answer_contract module — grades, caveat registry, fail-open block builder

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Wire query verbs through `execute_plan` (+ #5 field fix + spec truth-up)

**Files:**
- Modify: `docs/superpowers/specs/2026-07-26-honest-answer-contract-design.md` (truth-up, Step 1)
- Modify: `src/graphite/query.py` (QueryVerb fields ~201-251, `execute_plan` ~340-356, `_neighbor_listing` ~139-142, `_attach_resolution` ~18-23)
- Test: `tests/test_query_plan.py` (extend)

**Interfaces:**
- Consumes from Task 1: `build_answer_block`, `languages_for_nodes`, `GRADE_INCONCLUSIVE`.
- Produces: `QueryVerb.relations: tuple[str, ...]` and `QueryVerb.empty_meaning: str` fields; every `execute_plan` success envelope (and `no_path` errors) may carry `answer`.

- [ ] **Step 1: Spec truth-up (two corrections found while reading code)**

Edit the spec: (a) §3 "Human printers updated" — replace the `cmd_query` mention with: "`cmd_query` emits pure JSON today and keeps doing so; the `answer` block is its entire disclosure. Human epistemology lines apply to the surfaces that have human rendering: `impact`, `context`." (b) §7 table row `reaches`: relations declared = `calls` only (the verb walks `_CALL_RELATIONS` exclusively — query.py:75; the spec's `calls, imports` was wrong).

```bash
git add docs/superpowers/specs/2026-07-26-honest-answer-contract-design.md
git commit -m "docs(spec): truth-up — cmd_query is JSON-only; reaches declares calls only

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 2: Write the failing tests** (append to `tests/test_query_plan.py`; follow its existing graph-fixture style — it already builds small `nx.DiGraph`s and calls `execute_plan`/`query`)

```python
def _contract_graph():
    """Python-healthy graph: one caller -> callee, plus an isolated leaf."""
    g = nx.DiGraph()
    g.add_node("a", kind="file", name="a.py", source_file="a.py")
    g.add_node("b", kind="file", name="b.py", source_file="b.py")
    g.add_node("a_fn", kind="function", name="fn", source_file="a.py")
    g.add_node("b_fn", kind="function", name="gn", source_file="b.py")
    g.add_edge("a", "b", relation="imports", source_file="a.py")
    g.add_edge("a_fn", "b_fn", relation="calls", source_file="a.py")
    return g


def test_execute_plan_attaches_answer_block():
    g = _contract_graph()
    result = execute_plan(g, make_plan("imported-by", [("node", "b")], {}))
    assert result["answer"]["schema"] == 1
    assert result["answer"]["relations"] == ["imports"]
    assert result["answer"]["grade"] == "decision_grade"
    assert "empty_meaning" not in result["answer"]


def test_execute_plan_empty_answer_carries_meaning():
    g = _contract_graph()
    result = execute_plan(g, make_plan("callers", [("node", "a_fn")], {}))
    assert result["total"] == 0
    assert result["answer"]["empty_meaning"] == "no bound callers found"


def test_stats_has_no_answer_block():
    g = _contract_graph()
    result = execute_plan(g, make_plan("stats", [], {}))
    assert "answer" not in result


def test_no_path_error_carries_answer_block():
    g = _contract_graph()
    result = execute_plan(g, make_plan("reaches", [("source", "b_fn"), ("target", "a_fn")], {}))
    assert result["error_code"] == "no_path"
    assert result["answer"]["relations"] == ["calls"]
    assert result["answer"]["empty_meaning"] == "no call path found within depth"


def test_neighbor_listing_entries_carry_source_file():
    g = _contract_graph()
    result = execute_plan(g, make_plan("imported-by", [("node", "b")], {}))
    entry = result["imported_by"][0]
    assert entry["source_file"] == "a.py"
    assert set(entry.keys()) == {"id", "name", "kind", "source_file"}


def test_legacy_inconclusive_upgrades_to_scoped(monkeypatch):
    """Empty answer on a degraded scoped cell => inconclusive even if
    aggregate healthy (firescraper regression, query path)."""
    g = _contract_graph()
    # Degrade typescript calls while python stays healthy.
    g.add_node("t", kind="function", name="t", source_file="t.ts")
    for i in range(9):
        ph = f"tsph{i}"
        g.add_node(ph, kind="unknown")
        g.add_edge("t", ph, relation="calls", source_file="t.ts")
    # Enough bound python calls to keep the AGGREGATE ratio >= 0.8
    # (9 unbound ts + 41 bound py -> calls 41/50 = 0.82): this is the
    # firescraper shape — aggregate healthy, ts cell degraded.
    for i in range(40):
        fn = f"py_fn{i}"
        g.add_node(fn, kind="function", name=f"f{i}", source_file="a.py")
        g.add_edge(fn, "b_fn", relation="calls", source_file="a.py")
    result = execute_plan(g, make_plan("callers", [("node", "t")], {}))
    assert result["total"] == 0
    assert result["answer"]["grade"] == "inconclusive"
    assert result["inconclusive"] is True
    assert result["resolution_health"]["healthy"] is True  # aggregate masks; scoped does not
```

(Adjust imports at the top of the file to whatever it already imports — it uses `execute_plan`, `make_plan`, `nx` already; add only what is missing.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `PYTHONPATH="F:/tmp/graphite-honest-answer/src" python -m pytest tests/test_query_plan.py -q`
Expected: new tests FAIL (`KeyError: 'answer'`, `source_file` missing); existing tests PASS.

- [ ] **Step 4: Implement**

(a) `QueryVerb` gains two fields (after `limits`):

```python
    relations: tuple[str, ...] = ()
    empty_meaning: str = ""
```

(b) `QUERY_VERBS` entries gain keyword args (append to each constructor call):

| verb | relations | empty_meaning |
|---|---|---|
| callers | `("calls",)` | `"no bound callers found"` |
| calls | `("calls",)` | `"no bound callees found"` |
| reaches | `("calls",)` | `"no call path found within depth"` |
| path | `("calls", "imports")` | `"no path found within depth"` |
| depends-on | `("calls", "imports")` | `"no bound dependencies found"` |
| imported-by | `("imports",)` | `"no bound importers found"` |
| community-of | `("calls", "imports")` | `"no community assigned"` |
| stats | `()` (unchanged — no block) | `""` |

Because `QueryVerb` entries are positional today, pass the new fields as keywords: e.g. `QueryVerb("callers", ..., (("max_results", DEFAULT_MAX_RESULTS),), relations=("calls",), empty_meaning="no bound callers found")`.

(c) `_neighbor_listing` (#5 fix): replace lines 139-142's inline dict with

```python
        key: [_node_view(g, n) for n in shown],
```

(d) `execute_plan` — attach the block:

```python
def _is_empty(spec: QueryVerb, result: dict[str, Any]) -> bool:
    if result.get("error_code") == "no_path":
        return True
    if "total" in result:
        return result["total"] == 0
    if spec.name == "community-of":
        return result.get("community") is None
    return False


def execute_plan(g: nx.DiGraph, plan: object) -> dict[str, Any]:
    """Validate a plan against schema v1 and the verb registry, then run it."""
    reason = plan_error(plan, _EXPECTED_ROLES)
    if reason is not None:
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "error": f"invalid query plan: {reason}",
            "error_code": "invalid_plan",
        }
    assert isinstance(plan, dict)  # narrowed by plan_error
    spec = _VERB_INDEX[plan["operation"]]
    inputs = [target["input"] for target in plan["targets"]]
    result = spec.handler(g, inputs, plan["options"])
    envelope = {"schema_version": RESULT_SCHEMA_VERSION, **result}
    is_error = "error" in result
    if not is_error:
        envelope["resolution"] = _resolution(spec, result)
    if spec.relations and (not is_error or result.get("error_code") == "no_path"):
        seeds = [] if is_error else [entry.get("node") for entry in _resolution(spec, result)]
        block = build_answer_block(
            g,
            relations=spec.relations,
            languages=languages_for_nodes(g, seeds),
            total=0 if _is_empty(spec, result) else 1,
            empty_meaning=spec.empty_meaning or None,
        )
        if block is not None:
            envelope["answer"] = block
            if "inconclusive" in envelope:
                envelope["inconclusive"] = block["grade"] == GRADE_INCONCLUSIVE
    return envelope
```

Imports at top of query.py: `from .answer_contract import GRADE_INCONCLUSIVE, build_answer_block, languages_for_nodes`.

`_attach_resolution` itself is UNCHANGED (its aggregate-based value is the fail-open fallback when the block is `None`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH="F:/tmp/graphite-honest-answer/src" python -m pytest tests/test_query_plan.py tests/test_answer_contract.py tests/test_natural_query.py tests/test_search.py -q`
Expected: all PASS (natural-query inherits through `execute_plan` — its tests prove no regression).

- [ ] **Step 6: Commit**

```bash
git add src/graphite/query.py tests/test_query_plan.py
git commit -m "feat(query): answer block on every verb via execute_plan; imported_by/depends_on gain source_file

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Wire `impact` and `context` (+ human epistemology lines)

**Files:**
- Modify: `src/graphite/cli.py` (`_impact` ~342-387, `cmd_impact` ~1016-1043)
- Modify: `src/graphite/context.py` (builder ~52-82, `format_context_markdown` ~106-129)
- Test: `tests/test_health.py` (extend — it owns the cmd_impact human-surface tests), `tests/test_context.py` (extend)

**Interfaces:**
- Consumes from Task 1: `build_answer_block`, `languages_for_nodes`, `GRADE_INCONCLUSIVE`.
- Produces: `_impact` result dict and `build_context` dict gain optional `"answer"`; shared human formatter `_answer_lines(block) -> list[str]` in cli.py (context.py duplicates the two-line format in markdown style — two renderers, one pinned format).

Human line format (pinned; tests snapshot it):

```
  answer health: calls (python) 0.94, imports (python) 0.90 — decision-grade
  known limits: dynamically dispatched calls (getattr, decorator rebinding) are not modeled
```

- Line 1: `answer health: ` + `f"{relation} ({language}) {ratio:.2f}"` joined by `", "`, over `block["health"]` in sorted relation-then-language order + ` — ` + `block["grade"].replace("_", "-")`.
- Line 2: `known limits: ` + `"; ".join(c["summary"] for c in block["caveats"])`; omitted when no caveats.
- Printed only when the block exists AND (the answer is empty OR any cell has `healthy: false`).

- [ ] **Step 1: Write the failing tests**

Two direct tests (self-contained, `from graphite import cli` + networkx — no CLI plumbing needed):

```python
def _answer_graph(*, degraded_ts=False):
    g = nx.DiGraph()
    g.add_node("src_a", kind="file", name="a.py", source_file="src/a.py")
    g.add_node("src_b", kind="file", name="b.py", source_file="src/b.py")
    g.add_edge("src_b", "src_a", relation="imports", source_file="src/b.py")
    if degraded_ts:
        g.add_node("t", kind="file", name="t.ts", source_file="src/t.ts")
        for i in range(9):
            ph = f"tsph{i}"
            g.add_node(ph, kind="unknown")
            g.add_edge("t", ph, relation="calls", source_file="src/t.ts")
        # Keep the AGGREGATE calls ratio >= 0.8 (41 bound py + 9 unbound ts
        # = 41/50 = 0.82) so the test proves scoped grading sees what the
        # aggregate masks (the firescraper shape).
        g.add_node("py_callee", kind="function", name="callee", source_file="src/a.py")
        for i in range(41):
            fn = f"py_fn{i}"
            g.add_node(fn, kind="function", name=f"f{i}", source_file="src/a.py")
            g.add_edge(fn, "py_callee", relation="calls", source_file="src/a.py")
    return g


def test_impact_result_carries_answer_block():
    g = _answer_graph()
    result = cli._impact(g, ["src/a.py"], 2)
    assert result["answer"]["relations"] == ["calls", "imports"]
    assert result["answer"]["grade"] == "decision_grade"
    assert result["impacted_files"] == ["src/b.py"]


def test_impact_inconclusive_upgrades_to_scoped():
    g = _answer_graph(degraded_ts=True)
    result = cli._impact(g, ["src/t.ts"], 2)
    assert result["impacted_files"] == [] and result["likely_tests"] == []
    assert result["answer"]["grade"] == "inconclusive"
    assert result["inconclusive"] is True
    assert result["resolution_health"]["healthy"] is True  # aggregate masks; scoped does not
```

Two output tests, modeled EXACTLY on `test_cmd_impact_human_inconclusive_line` (`tests/test_health.py:264` — read it first and copy its args-namespace + monkeypatch idiom, including the `_record_canonical_usage` AND `_record_inconclusive` no-ops from commit 36e2528):

- `test_cmd_impact_prints_epistemology_on_empty`: run the cmd path on the healthy `_answer_graph()` seeded at `src/b.py` (it has no importers → empty); assert stdout contains `"answer health: "` and `"decision-grade"` and does NOT contain `"INCONCLUSIVE"`.
- `test_cmd_impact_epistemology_absent_on_healthy_nonempty`: cmd path seeded at `src/a.py` (non-empty, healthy); assert stdout does NOT contain `"answer health:"` (byte-identical guarantee).

In `tests/test_context.py` add the mirror pair using its existing graph fixtures: `build_context` result carries `answer`; `format_context_markdown` output contains `"answer health: "` when empty/degraded and not when healthy non-empty.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="F:/tmp/graphite-honest-answer/src" python -m pytest tests/test_health.py tests/test_context.py -q`
Expected: new tests FAIL (`KeyError: 'answer'`, missing lines); existing PASS.

- [ ] **Step 3: Implement**

(a) cli.py `_impact` — after `health = resolution_health(g)` (line ~377), build the block and refine `inconclusive`:

```python
    health = resolution_health(g)
    total = len(impacted_files) + len(likely_tests)
    block = build_answer_block(
        g,
        relations=("calls", "imports"),
        languages=languages_for_nodes(g, start_nodes),
        total=total,
        empty_meaning="no impacted files or tests reachable through bound edges",
    )
    inconclusive = (
        block["grade"] == GRADE_INCONCLUSIVE
        if block is not None
        else (not impacted_files and not likely_tests and not health["healthy"])
    )
    result = {
        "changed": changes,
        "matched_nodes": sorted(start_nodes),
        "missing": missing,
        "depth": depth,
        "impacted_files": sorted(impacted_files),
        "likely_tests": sorted(likely_tests),
        "resolution_health": health,
        "inconclusive": inconclusive,
    }
    if block is not None:
        result["answer"] = block
    return result
```

(b) cli.py shared formatter (place directly above `cmd_impact`):

```python
def _answer_lines(block: dict[str, Any] | None, *, empty: bool) -> list[str]:
    """Human epistemology lines; [] unless empty or a scoped cell is degraded."""
    if not block:
        return []
    degraded = any(
        not cell.get("healthy", True)
        for langs in block.get("health", {}).values()
        for cell in langs.values()
    )
    if not empty and not degraded:
        return []
    cells = ", ".join(
        f"{relation} ({language}) {langs[language]['ratio']:.2f}"
        for relation, langs in sorted(block.get("health", {}).items())
        for language in sorted(langs)
    )
    grade = block.get("grade", "").replace("_", "-")
    lines = [f"  answer health: {cells} — {grade}"] if cells else [f"  answer health: — {grade}"]
    if block.get("caveats"):
        lines.append("  known limits: " + "; ".join(c["summary"] for c in block["caveats"]))
    return lines
```

(c) `cmd_impact` non-JSON branch — after the existing prints (both the INCONCLUSIVE branch and the normal branch), add:

```python
        empty = not result["impacted_files"] and not result["likely_tests"]
        for line in _answer_lines(result.get("answer"), empty=empty):
            print(line)
```

Also, in the normal branch, when `empty` and not inconclusive, print the empty meaning after "Impacted files:" heading logic — replace the plain headers with:

```python
        else:
            if result["impacted_files"] or result["likely_tests"]:
                print("Impacted files:")
                for path in result["impacted_files"]:
                    print(f"  - {path}")
                print("Likely tests:")
                for path in result["likely_tests"]:
                    print(f"  - {path}")
            else:
                meaning = (result.get("answer") or {}).get(
                    "empty_meaning", "none found"
                )
                print(f"Impacted files: none found — {meaning}")
```

(keep the existing `note: resolution health low` tail unchanged).

(d) context.py builder — mirror (a): compute `block` with the same arguments (`start_nodes` are in scope), set `"answer": block` when not None, and replace the `inconclusive = (...)` expression with the same block-first/legacy-fallback pattern. Import at top: `from .answer_contract import GRADE_INCONCLUSIVE, build_answer_block, languages_for_nodes`.

(e) `format_context_markdown` — after the Impact section's existing lines (after line ~129's low-health note), append:

```python
    answer = context.get("answer")
    empty = not impact["impacted_files"] and not impact["likely_tests"]
    if answer:
        degraded = any(
            not cell.get("healthy", True)
            for langs in answer.get("health", {}).values()
            for cell in langs.values()
        )
        if empty or degraded:
            cells = ", ".join(
                f"{relation} ({language}) {langs[language]['ratio']:.2f}"
                for relation, langs in sorted(answer.get("health", {}).items())
                for language in sorted(langs)
            )
            lines.append(f"answer health: {cells} — {answer['grade'].replace('_', '-')}")
            if answer.get("caveats"):
                lines.append("known limits: " + "; ".join(c["summary"] for c in answer["caveats"]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="F:/tmp/graphite-honest-answer/src" python -m pytest tests/test_health.py tests/test_context.py tests/test_review.py tests/test_watch.py -q`
Expected: all PASS (review/watch consume `_impact`'s dict — their tests prove the additive key breaks nothing).

- [ ] **Step 5: Commit**

```bash
git add src/graphite/cli.py src/graphite/context.py tests/test_health.py tests/test_context.py
git commit -m "feat(impact,context): answer block + human epistemology lines on empty/degraded

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Resolver fix — `from pkg import submodule` binds the submodule (cache v8)

**Files:**
- Modify: `src/graphite/extract/ast.py` (new helper near `_python_import_modules` ~487; emission site ~660-677)
- Modify: `src/graphite/config.py:32` and `:159` (`"v7"` → `"v8"`, comment gains `v8: from-package submodule import edges`)
- Test: `tests/test_python_resolver.py` (extend — it owns the Python import-binding fixtures from the resolver round; follow its existing tmp-repo + extraction idiom exactly)

**Interfaces:**
- Consumes: `_python_import_modules`, `source_index.resolve_python_module(rel_path, dotted, dots)`, `_file_node_id`, `_edge` — all existing in ast.py.
- Produces: additional `imports` edges `file -> submodule-file` with `confidence="EXACT_IMPORT"`; the existing base-module edge is kept.

- [ ] **Step 1: Write the failing tests**

Extend `tests/test_python_resolver.py` with one test per spec §9 idiom row. Fixture layout (per test, via the file's existing tmp-repo builder):

```
pkg/__init__.py          (empty)
pkg/pipeline.py          (def run(): ...)
pkg/runners/__init__.py  (empty)
pkg/runners/tests.py     (def go(): ...)
consumer.py              (the idiom under test)
```

Assertions per idiom (exact edge expectations; node ids follow the repo's `_file_node_id` convention — assert via source/target lookup, not hardcoded ids):

1. `from pkg import pipeline` → imports edges from consumer.py to BOTH `pkg/__init__.py` AND `pkg/pipeline.py`, both `EXACT_IMPORT`.
2. `from pkg import pipeline, runners` → edges to `pkg/__init__.py`, `pkg/pipeline.py`, `pkg/runners/__init__.py`.
3. `from pkg.runners import tests as t` → edges to `pkg/runners/__init__.py` AND `pkg/runners/tests.py`.
4. relative: a file `pkg/sibling_user.py` containing `from . import pipeline` → edges to `pkg/__init__.py` AND `pkg/pipeline.py`.
5. parenthesized: `from pkg import (pipeline, runners)` → same as idiom 2.
6. symbol: `pkg/pipeline.py` defines `run`; consumer has `from pkg.pipeline import run` → edge to `pkg/pipeline.py` ONLY (no new edge; count unchanged vs today).

Plus the end-to-end regression: build the graph for the fixture repo, run `cli._impact(g, ["pkg/pipeline.py"], 2)` where `tests/unit-style consumer` is a test file `tests/test_consumer.py` containing `from pkg import pipeline` — assert `"tests/test_consumer.py" in result["likely_tests"]` (this is aramid's #7 in miniature).

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="F:/tmp/graphite-honest-answer/src" python -m pytest tests/test_python_resolver.py -q`
Expected: new tests FAIL (submodule edges absent); existing PASS.

- [ ] **Step 3: Implement**

(a) New helper directly below `_python_import_modules` (ast.py ~524):

```python
def _python_from_import_submodules(
    node: Any, rel_path: str, source_index: SourceIndex | None
) -> list[Any]:
    """Resolved submodule paths for `from P import a, b` when a/b are modules.

    Mirrors _collect_python_import_maps' module-first probe (ast.py:583-587)
    at the import-EDGE layer: the emission site only ever saw the base
    module, which is how `from aramid import pipeline` bound to the package
    __init__ and hid test files from impact (issue #7).
    """
    if node.type != "import_from_statement" or source_index is None:
        return []
    modules = _python_import_modules(node)
    if not modules:
        return []
    base_module, dots = modules[0]
    module_field = node.child_by_field_name("module_name")

    def _text(n: Any) -> str:
        return n.text.decode("utf-8", errors="ignore") if n is not None and n.text else ""

    out: list[Any] = []
    for child in node.children:
        if module_field is not None and child.id == module_field.id:
            # Identity-skip the module_name's own dotted_name (paren-safe;
            # see _collect_python_import_maps for the sibling-token trap).
            continue
        original = None
        if child.type == "dotted_name":
            original = _text(child)
        elif child.type == "aliased_import":
            original = _text(child.child_by_field_name("name"))
        if not original or "." in original:
            continue
        sub = f"{base_module}.{original}" if base_module else original
        resolved = source_index.resolve_python_module(rel_path, sub, dots)
        if resolved:
            out.append(resolved)
    return out
```

(b) At the emission site (inside the `elif node.type in ("import_statement", "import_from_statement"):` branch, after the existing `for module, dots in _python_import_modules(node):` loop, before `walk_children`):

```python
            for sub in _python_from_import_submodules(node, rel_path, source_index):
                result.edges.append(_edge(
                    file_id, _file_node_id(sub), "imports", rel_path,
                    _line(node), confidence="EXACT_IMPORT",
                ))
```

(c) config.py:32 → `cache_version: str = "v8"  # bump on extraction-format changes (v8: from-package submodule import edges; v7: python import/file-node resolution, ...)` (keep the existing history tail); config.py:159 → default `"v8"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="F:/tmp/graphite-honest-answer/src" python -m pytest tests/test_python_resolver.py tests/test_health.py tests/test_call_graph.py tests/test_monorepo.py tests/test_engine_identity.py -q`
Expected: all PASS. If an engine-identity or freshness test pins `"v7"`, update the pin in the same commit — that pin exists to force exactly this deliberate review.

- [ ] **Step 5: Commit**

```bash
git add src/graphite/extract/ast.py src/graphite/config.py tests/test_python_resolver.py
git commit -m "fix(resolver): bind 'from pkg import submodule' to the submodule file node (cache v8)

Closes the issue-#7 class: the import-edge layer now runs the same
module-first probe as call binding; package __init__ edges are kept.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Published surfaces — capabilities, schema, agent docs, template v9

**Files:**
- Modify: `src/graphite/cli.py` (`cmd_capabilities` payload ~890-900)
- Modify: `docs/schemas/query-result.v1.schema.json` (additive `answer` property)
- Modify: `docs/agent-integration.md` (new section)
- Modify: `src/graphite/init.py` (`DOC_VERSION` 8→9 at line 17; template item 6 rewrite ~line 52)
- Test: `tests/test_published_schemas.py`, `tests/test_init.py` / `tests/test_documentation.py` (whichever holds `test_template_change_requires_doc_version_bump` — grep for it), `tests/test_smoke.py` if it asserts capabilities keys

**Interfaces:**
- Consumes from Task 1: `ANSWER_SCHEMA`, `GRADE_DECISION`, `GRADE_ADVISORY`, `GRADE_INCONCLUSIVE`, `active_caveats`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_published_schemas.py` (follow its existing validate-instance-against-schema idiom):

```python
def test_query_result_schema_admits_answer_block():
    # a representative result WITH the answer block validates
    # and one WITHOUT it also validates (the key is optional)


def test_capabilities_carries_answer_contract():
    # cmd_capabilities JSON payload includes:
    # answer_contract.schema == 1
    # answer_contract.grades == ["decision_grade", "advisory", "inconclusive"]
    # every entry in answer_contract.caveats has code/relations/languages/summary/since
```

Write both fully against the file's existing helpers (it already loads `docs/schemas/*.json` and runs the repo's subset validator; reuse those fixtures verbatim).

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="F:/tmp/graphite-honest-answer/src" python -m pytest tests/test_published_schemas.py -q`
Expected: new tests FAIL.

- [ ] **Step 3: Implement**

(a) `cmd_capabilities` payload — after `"query_verbs": verb_catalog(),` add:

```python
        "answer_contract": {
            "schema": ANSWER_SCHEMA,
            "grades": [GRADE_DECISION, GRADE_ADVISORY, GRADE_INCONCLUSIVE],
            "caveats": active_caveats(),
        },
```

with the import `from .answer_contract import ANSWER_SCHEMA, GRADE_ADVISORY, GRADE_DECISION, GRADE_INCONCLUSIVE, active_caveats` added to cli.py's existing import block.

(b) `docs/schemas/query-result.v1.schema.json` — add inside top-level `properties` (NOT in `required`); keep the file's `additionalProperties: true` convention with an explicit `"additionalProperties": true` on the new object:

```json
"answer": {
  "type": "object",
  "additionalProperties": true,
  "description": "Answer-scoped confidence: relations walked, per-language health cells for exactly those, derived grade, applicable caveat codes; empty_meaning present iff the primary result is empty. Absent when the block could not be computed (fail-open) or on error results other than no_path.",
  "required": ["schema", "relations", "languages", "health", "grade", "caveats"],
  "properties": {
    "schema": {"type": "integer"},
    "relations": {"type": "array", "items": {"type": "string"}},
    "languages": {"type": "array", "items": {"type": "string"}},
    "health": {"type": "object", "additionalProperties": true},
    "grade": {"enum": ["decision_grade", "advisory", "inconclusive"]},
    "caveats": {"type": "array", "items": {"type": "object", "additionalProperties": true, "required": ["code", "summary"]}},
    "empty_meaning": {"type": "string"}
  }
}
```

(c) `docs/agent-integration.md` — new section after the existing resolution-health/trust-signal section (grep for "resolution_health" to place it):

```markdown
## The answer block: acting on graph answers

Every query/impact/context answer carries `answer` — trust scoped to what
this answer actually walked, not the whole graph:

- `grade: "decision_grade"` — every relation×language cell this answer used
  is healthy. A non-empty result is decision-grade evidence; an EMPTY
  result is a trustworthy absence (subject to `caveats`).
- `grade: "advisory"` — a used cell is below threshold and the result is
  non-empty. Treat the list as incomplete: verify with grep and say so.
- `grade: "inconclusive"` — a used cell is below threshold and the result
  is empty. Unknown, not safe. The legacy `inconclusive` boolean mirrors
  this grade (its derivation is scoped since answer-contract v1; it used
  to consult only the aggregate `healthy`).
- `caveats` — known blindspot classes applying to this answer's
  relations×languages, from `capabilities.answer_contract.caveats`. They
  apply even at decision_grade.
- `empty_meaning` — what an empty primary result asserts ("no bound
  callers found"): a measurement statement, not proof of absence.

The block may be absent (fail-open); treat absence as "no scoped signal —
fall back to resolution_health".
```

(d) init.py: `DOC_VERSION = 9`; rewrite template item 6 (line ~52) to:

```
6. Graph answers carry an `answer` block: `grade: decision_grade` means this answer's own relations/languages are healthy (an empty result is a trustworthy absence, subject to `caveats`); `advisory` means verify with grep and say so; `inconclusive` (also the legacy `"inconclusive": true`) means unknown, not safe. Check `known limits`/`caveats` before trusting empties.
```

Run the template-digest pairing test (grep `test_template_change_requires_doc_version_bump` under `tests/` for its home); update its pinned digest exactly as its failure message instructs — the pin exists to force this deliberate review, and the DOC_VERSION bump is this task.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH="F:/tmp/graphite-honest-answer/src" python -m pytest tests/test_published_schemas.py tests/test_init.py tests/test_documentation.py tests/test_smoke.py -q`
Expected: all PASS.

- [ ] **Step 5: Full suite + ruff**

Run (bash): `cd /f/tmp/graphite-honest-answer && PYTHONPATH="F:/tmp/graphite-honest-answer/src" python -m pytest -q > /tmp/honest-answer-suite.log 2>&1; echo rc=$?` then read the log tail.
Expected: `rc=0`, `2201+N passed, 44 skipped` (N = tests added by Tasks 1-5, ≥ 25). Then `python -m ruff check src tests` → clean.

- [ ] **Step 6: Commit**

```bash
git add src/graphite/cli.py src/graphite/init.py docs/schemas/query-result.v1.schema.json docs/agent-integration.md tests/test_published_schemas.py tests/test_init.py tests/test_documentation.py
git commit -m "feat(contract): publish answer_contract — capabilities, schema, agent docs, template v9

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Post-plan (NOT plan tasks — for the controller)

- Final whole-branch review (superpowers:requesting-code-review, most capable model), then superpowers:finishing-a-development-branch.
- Live acceptance §12 A1–A4 (operator-gated): aramid rebuild @ v8 → impact lists `test_pipeline.py` + `test_runner_tests.py`; `imported-by pipeline` includes it with `source_file`; empty-answer epistemology lines on aramid; firescraper TS-seeded empty → `grade: inconclusive` with aggregate `healthy: true`.
- Rollout §13: consumer re-inits to v9; machine CLAUDE.md doctrine rewrite (operator approves wording); notify aramid's agent; close issues #5/#7 with the two-answer rule. No daemon restart (no daemon-executed surface).
