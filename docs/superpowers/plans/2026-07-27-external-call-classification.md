# External-Call Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the `calls` health ratio from counting external-package symbols, injected test globals, and missing language builtins as resolver failures.

**Architecture:** A new edge confidence value `EXTERNAL_CALL` is assigned at extraction time from two sources — names bound by imports that did not resolve in-repo, and a curated set of never-imported runtime globals. `health.py` excludes those edges from `calls` totals and reports them in a new `external` field, mirroring what `EXTERNAL_IMPORT` already does for `imports`. Health schema goes 2→3 and cache version v8→v9.

**Tech Stack:** Python 3, tree-sitter (via `tree_sitter_typescript` et al.), networkx, pytest.

**Spec:** `docs/superpowers/specs/2026-07-26-external-call-classification-design.md`

## Global Constraints

- **Pure relabel.** No `calls` edge may be added, removed, or re-targeted. Only the `confidence` value changes. Task 7 enforces this.
- **Nothing moves between `_LANGUAGE_BUILTIN_GLOBALS` and `_EXTERNAL_GLOBALS`.** The existing drop-list is frozen for this round (spec §4.3).
- **Externality only excuses an UNBOUND edge.** An edge marked external whose target resolved to a real node is counted normally, in numerator and denominator (spec §5). This is the guard against name-based matching masking a repo's own `test()`/`process()`.
- Confidence string is exactly `"EXTERNAL_CALL"`. Health schema value is exactly `3`. Cache version is exactly `"v9"`.
- `RESOLUTION_HEALTHY_RATIO` (0.8), the `healthy` rule, `placeholder_nodes`, `ratio_percent`, and `persisted_resolution` are unchanged.
- Never pipe pytest output — redirect to a file and read `$?`/`$LASTEXITCODE` directly. `cmd | tail` reports the pager's exit status and has made failing runs look green.
- One commit per task.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/graphite/health.py` | schema 3, `external` on calls, unbound-guarded exclusion | 1 |
| `src/graphite/extract/ast.py` | `_EXTERNAL_GLOBALS`, `_call_confidence`, external import-binding collection, tagging at 4 call sites | 2, 3, 4 |
| `src/graphite/config.py` | cache version v9 | 2 |
| `src/graphite/answer_contract.py` | retire + replace the TS caveat | 5 |
| `docs/agent-integration.md` | schema-3 consumer contract | 6 |
| `tests/test_health.py` | health unit tests (existing, extended) | 1 |
| `tests/test_call_graph.py` | existing calls-cell assertions | 1 |
| `tests/test_python_resolver.py` | Python extraction + cache-version pin | 2, 4 |
| `tests/test_external_calls.py` | **new** — globals tagging, TS bindings, invariant, falsifier | 2, 3, 7 |
| `tests/test_answer_contract.py` | caveat registry | 5 |

---

## Task 1: Health schema 3 — exclude external calls, guarded on unbound

**Files:**
- Modify: `src/graphite/health.py:43-51` (`_cell`), `:60-84` (the edge loop), `:87` (schema)
- Test: `tests/test_health.py`, `tests/test_call_graph.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `resolution_health(g)` returns `schema: 3`; every `by_relation` and `by_language` cell (both `calls` and `imports`) carries `"external": int`. Edges with `confidence == "EXTERNAL_CALL"` on a `calls` relation, or `"EXTERNAL_IMPORT"` on an `imports` relation, are excluded from `total`/`bound`/`ratio` **only when their target is unbound**.

- [ ] **Step 1: Extend the test-graph helper to carry confidence**

In `tests/test_health.py`, replace `_graph` (currently lines 16-22) with a version that accepts an optional 5th edge element. Existing 4-tuple call sites keep working unchanged:

```python
def _graph(nodes, edges):
    g = nx.DiGraph()
    for node_id, kind, source_file in nodes:
        g.add_node(node_id, kind=kind, source_file=source_file)
    for edge in edges:
        src, dst, relation, source_file = edge[:4]
        attrs = {"relation": relation, "source_file": source_file}
        if len(edge) > 4 and edge[4] is not None:
            attrs["confidence"] = edge[4]
        g.add_edge(src, dst, **attrs)
    return g
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_health.py`:

```python
def test_external_call_edges_are_excluded_and_counted():
    g = _graph(
        nodes=[
            ("f1", "function", "a.ts"),
            ("f2", "function", "b.ts"),
            ("expect", "unknown", None),
        ],
        edges=[
            ("f1", "f2", "calls", "a.ts", "LOCAL_CALL"),
            ("f1", "expect", "calls", "a.ts", "EXTERNAL_CALL"),
        ],
    )
    block = resolution_health(g)
    assert block["by_relation"]["calls"] == {
        "total": 1, "bound": 1, "ratio": 1.0, "external": 1
    }
    assert block["by_language"]["typescript"]["calls"]["external"] == 1


def test_schema_is_three():
    assert resolution_health(nx.DiGraph())["schema"] == 3


def test_external_call_that_bound_is_counted_normally():
    """Externality only excuses an UNBOUND edge (spec §5).

    Guards the name-based matching in _EXTERNAL_GLOBALS: a repo that defines
    its own test()/process() must not have that real binding excluded.
    """
    g = _graph(
        nodes=[
            ("f1", "function", "a.ts"),
            ("mine", "function", "a.ts"),
        ],
        edges=[("f1", "mine", "calls", "a.ts", "EXTERNAL_CALL")],
    )
    block = resolution_health(g)
    assert block["by_relation"]["calls"] == {
        "total": 1, "bound": 1, "ratio": 1.0, "external": 0
    }


def test_calls_cell_with_only_external_edges_reports_null_ratio():
    g = _graph(
        nodes=[("f1", "function", "a.ts"), ("expect", "unknown", None)],
        edges=[("f1", "expect", "calls", "a.ts", "EXTERNAL_CALL")],
    )
    cell = resolution_health(g)["by_relation"]["calls"]
    assert cell == {"total": 0, "bound": 0, "ratio": None, "external": 1}


def test_external_call_confidence_on_imports_relation_is_not_external():
    """The mapping is per-relation: EXTERNAL_CALL means nothing on imports."""
    g = _graph(
        nodes=[("f1", "file", "a.ts"), ("ghost", "unknown", None)],
        edges=[("f1", "ghost", "imports", "a.ts", "EXTERNAL_CALL")],
    )
    cell = resolution_health(g)["by_relation"]["imports"]
    assert cell == {"total": 1, "bound": 0, "ratio": 0.0, "external": 0}
```

- [ ] **Step 3: Run the tests to verify they fail**

```
python -m pytest tests/test_health.py -q > out.txt 2>&1; echo $LASTEXITCODE
```
Expected: FAIL — `schema` is 2, and `calls` cells have no `external` key.

- [ ] **Step 4: Implement**

In `src/graphite/health.py`, add below `_COUNTED_RELATIONS` (line 17):

```python
# Per-relation marker for "this edge leaves the repo". An edge counts as
# external ONLY when it also failed to bind -- externality excuses an unbound
# edge, it never removes a bound one (spec §5).
_EXTERNAL_CONFIDENCE: Final = {
    "imports": "EXTERNAL_IMPORT",
    "calls": "EXTERNAL_CALL",
}
```

Replace `_cell` (lines 43-51) — `external` is now unconditional, so the `relation` parameter goes away:

```python
def _cell(bound: int, total: int, external: int) -> dict[str, Any]:
    return {
        "total": total,
        "bound": bound,
        "ratio": None if total == 0 else round(bound / total, 3),
        "external": external,
    }
```

Replace the body of the edge loop (lines 71-79) with:

```python
        bound = int(g.nodes[v].get("kind", "unknown") != "unknown")
        if not bound and data.get("confidence") == _EXTERNAL_CONFIDENCE.get(relation):
            counts[2] += 1
            buckets[relation][2] += 1
            continue
        counts[0] += bound
        counts[1] += 1
        buckets[relation][0] += bound
        buckets[relation][1] += 1
```

Update the two `_cell` call sites (lines 80-84) to drop the `rel` argument:

```python
    by_relation = {rel: _cell(c[0], c[1], c[2]) for rel, c in relation_counts.items()}
    by_language = {
        language: {rel: _cell(c[0], c[1], c[2]) for rel, c in buckets.items()}
        for language, buckets in sorted(language_counts.items())
    }
```

Change `"schema": 2` (line 87) to `"schema": 3`.

- [ ] **Step 5: Update the existing assertions that now gain `external`**

Eight assertions assert a `calls` cell by equality and must gain `"external": 0`:

- `tests/test_health.py:27` — change `block["schema"] == 2` to `== 3`
- `tests/test_health.py:31` → `{"total": 0, "bound": 0, "ratio": None, "external": 0}`
- `tests/test_health.py:50` → `{"total": 2, "bound": 1, "ratio": 0.5, "external": 0}`
- `tests/test_health.py:88` → `{"total": 1, "bound": 1, "ratio": 1.0, "external": 0}`
- `tests/test_health.py:89` → `{"total": 1, "bound": 0, "ratio": 0.0, "external": 0}`
- `tests/test_health.py:101` → `{"total": 1, "bound": 0, "ratio": 0.0, "external": 0}`
- `tests/test_health.py:148` — multi-line `calls` cell; add `"external": 0`
- `tests/test_call_graph.py:123` and `:128` → add `"external": 0` to both

Then search for any remaining schema-2 assertion:

```
python -m pytest tests/ -q -k "health or call_graph" > out.txt 2>&1; echo $LASTEXITCODE
```

- [ ] **Step 6: Run the full suite**

```
python -m pytest tests/ -q > out.txt 2>&1; echo $LASTEXITCODE
```
Expected: exit 0. Any other failure is a schema-2 assertion elsewhere — fix it in this task.

- [ ] **Step 7: Commit**

```bash
git add src/graphite/health.py tests/test_health.py tests/test_call_graph.py
git commit -m "feat(health): schema 3 -- exclude unbound external calls, report external on calls cells"
```

---

## Task 2: `_EXTERNAL_GLOBALS` and `_call_confidence` — tag never-imported globals

**Files:**
- Modify: `src/graphite/extract/ast.py:26-48` (add set below), `:276-279` (TS/JS), `:730-741` (Python), `:834-835` (Go), `:934-935` (Rust)
- Modify: `src/graphite/config.py:32`, `:159`
- Modify: `tests/test_python_resolver.py:504-505`
- Create: `tests/test_external_calls.py`

**Interfaces:**
- Consumes: Task 1's health behaviour (an `EXTERNAL_CALL` edge is already excluded when unbound).
- Produces: `_call_confidence(called: str, external_names: Collection[str] = ()) -> str`, returning `"EXTERNAL_CALL"` or `"LOCAL_CALL"`. Tasks 3 and 4 pass a real `external_names`. Also `_EXTERNAL_GLOBALS: frozenset[str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_external_calls.py`:

```python
"""EXTERNAL_CALL classification: never-imported globals and external imports."""
from __future__ import annotations

from pathlib import Path

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.ingest import collect_files


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(tmp_path: Path):
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite")
    return extract_all(collect_files(tmp_path, cfg), cfg)


def _calls(result, source_file):
    return [
        e for e in result.edges
        if e.get("relation") == "calls" and e.get("source_file") == source_file
    ]


def _confidence_by_target_suffix(result, source_file):
    return {
        e["target"].rsplit("_", 1)[-1]: e["confidence"]
        for e in _calls(result, source_file)
    }


def test_injected_test_globals_are_tagged_external(tmp_path):
    _write(tmp_path / "src" / "sum.ts", "export function sum(a: number) { return a; }\n")
    _write(
        tmp_path / "src" / "sum.test.ts",
        "import { sum } from './sum';\n"
        "describe('sum', () => {\n"
        "  it('adds', () => {\n"
        "    expect(sum(1)).toBe(1);\n"
        "  });\n"
        "});\n",
    )
    result = _extract(tmp_path)
    conf = _confidence_by_target_suffix(result, "src/sum.test.ts")
    assert conf["expect"] == "EXTERNAL_CALL"
    assert conf["describe"] == "EXTERNAL_CALL"
    assert conf["it"] == "EXTERNAL_CALL"


def test_in_repo_call_stays_local(tmp_path):
    _write(tmp_path / "src" / "sum.ts", "export function sum(a: number) { return a; }\n")
    _write(
        tmp_path / "src" / "use.ts",
        "import { sum } from './sum';\nexport function go() { return sum(1); }\n",
    )
    result = _extract(tmp_path)
    confidences = {e["confidence"] for e in _calls(result, "src/use.ts")}
    assert confidences == {"LOCAL_CALL"}


def test_python_builtin_absent_from_drop_list_is_tagged(tmp_path):
    _write(
        tmp_path / "m.py",
        "def go():\n"
        "    raise ValueError('x')\n",
    )
    result = _extract(tmp_path)
    conf = _confidence_by_target_suffix(result, "m.py")
    assert conf["valueerror"] == "EXTERNAL_CALL"


def test_drop_list_names_still_produce_no_edge(tmp_path):
    """_LANGUAGE_BUILTIN_GLOBALS is frozen for this round (spec §4.3).

    `len` stays dropped -- it must NOT become an EXTERNAL_CALL edge, because
    that would add edges and break the pure-relabel invariant.
    """
    _write(tmp_path / "m.py", "def go(xs):\n    return len(xs)\n")
    result = _extract(tmp_path)
    assert not any(t.endswith("len") for t in
                   (e["target"] for e in _calls(result, "m.py")))


def test_member_call_root_decides_externality(tmp_path):
    _write(
        tmp_path / "src" / "t.ts",
        "export function go() { return crypto.randomUUID(); }\n",
    )
    result = _extract(tmp_path)
    confidences = {e["confidence"] for e in _calls(result, "src/t.ts")}
    assert confidences == {"EXTERNAL_CALL"}
```

- [ ] **Step 2: Run to verify they fail**

```
python -m pytest tests/test_external_calls.py -q > out.txt 2>&1; echo $LASTEXITCODE
```
Expected: FAIL — every call currently carries `LOCAL_CALL`.

- [ ] **Step 3: Add the globals set and the helper**

In `src/graphite/extract/ast.py`, immediately after `_LANGUAGE_BUILTIN_GLOBALS` (after line 48):

```python
# Names that are never defined in-repo: test-framework injections, runtime
# globals, and language builtins missing from _LANGUAGE_BUILTIN_GLOBALS.
# These are TAGGED EXTERNAL_CALL and excluded from the health ratio by
# health.py -- NOT dropped -- so the excluded evidence stays visible and
# countable in graph.json. Nothing moves between this set and the drop-list
# above; see spec §4.3.
#
# Deliberately absent: generic words a repo plausibly defines itself
# (`context`, `run`, `setup`, `main`). A false external costs more than a
# missed one, because it would mask real code. Also absent: `process`,
# `console`, `window`, `document` -- already in resolve.py's _BUILTIN_OBJECTS,
# so their member calls are dropped before reaching this classifier.
_EXTERNAL_GLOBALS: frozenset[str] = frozenset({
    # test-framework injected globals (vitest / jest / mocha)
    "expect", "it", "describe", "test", "vi", "jest",
    "beforeEach", "afterEach", "beforeAll", "afterAll",
    "suite", "xit", "xdescribe", "fit", "fdescribe",
    # JS / Web runtime globals absent from the drop-list
    "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "fetch", "queueMicrotask", "structuredClone", "atob", "btoa",
    "crypto", "performance", "Buffer", "require",
    # Python builtins absent from the drop-list
    "ValueError", "OSError", "AssertionError", "KeyError", "IndexError",
    "RuntimeError", "NotImplementedError", "StopIteration",
    "frozenset", "bytearray", "complex", "object", "Exception",
    "BaseException", "property", "staticmethod", "classmethod",
    "slice", "divmod", "format",
})


def _call_confidence(called: str, external_names: Collection[str] = ()) -> str:
    """`LOCAL_CALL`, or `EXTERNAL_CALL` when the call provably leaves the repo.

    ``called`` may be dotted (``z.object``); the ROOT carries the binding, so
    that is what is tested. ``external_names`` holds local names bound by
    imports that did not resolve in-repo (Tasks 3 and 4); it is empty here.
    """
    root = called.split(".", 1)[0]
    if root in _EXTERNAL_GLOBALS or root in external_names:
        return "EXTERNAL_CALL"
    return "LOCAL_CALL"
```

Add `Collection` to the `typing` import on line 10: `from typing import Any, Collection`.

- [ ] **Step 4: Wire the four extractor call sites**

Each site currently hardcodes `confidence="LOCAL_CALL"`. Replace with `confidence=_call_confidence(<name>)`:

- **TS/JS**, line 279 — the name in scope is `called`:
  ```python
                    edge = _edge(scope.id, target_id, "calls", rel_path, _line(node), confidence=_call_confidence(called))
  ```
- **Python**, line 732 (bare identifier branch) — the name is `bare`:
  ```python
                edge = _edge(scope_id, target, "calls", rel_path, _line(node), confidence=_call_confidence(bare))
  ```
- **Python**, line 740 (unresolved member branch) — the name is `dotted`:
  ```python
                    edge = _edge(scope_id, _resolve_call(file_id, dotted), "calls", rel_path, _line(node), confidence=_call_confidence(dotted))
  ```
  Leave line 736 (the `alias_map` branch) as `confidence="LOCAL_CALL"` — that branch fires only when the module resolved to an in-repo file, so it is local by construction.
- **Go**, line 835 and **Rust**, line 935 — the name is `called`:
  ```python
                    edge = _edge(scope_id, _resolve_call(file_id, called), "calls", rel_path, _line(node), confidence=_call_confidence(called))
  ```

- [ ] **Step 5: Bump the cache version**

Extraction output changed, so cached ASTs must be invalidated. In `src/graphite/config.py` line 32, change the default and prepend the new reason to the comment:

```python
    cache_version: str = "v9"  # bump on extraction-format changes (v9: EXTERNAL_CALL confidence on calls edges; v8: from-package submodule import edges; v7: python import/file-node resolution, import maps, python method dispatch; v6: per-package tsconfig path aliases; v5: resolve JS-extension + ".."-relative imports; v4: full-path node ids, workspace imports, phantom-edge drop)
```

Line 159: change `env.get("graphite_cache_version", "v8")` to `"v9"`.

Update the pin at `tests/test_python_resolver.py:504-505`:

```python
def test_cache_version_is_v9():
    assert Config().cache_version == "v9"
```

- [ ] **Step 6: Run the tests**

```
python -m pytest tests/test_external_calls.py tests/test_python_resolver.py -q > out.txt 2>&1; echo $LASTEXITCODE
```
Expected: exit 0.

- [ ] **Step 7: Run the full suite**

```
python -m pytest tests/ -q > out.txt 2>&1; echo $LASTEXITCODE
```
Expected: exit 0.

- [ ] **Step 8: Commit**

```bash
git add src/graphite/extract/ast.py src/graphite/config.py tests/test_external_calls.py tests/test_python_resolver.py
git commit -m "feat(extract): tag never-imported globals EXTERNAL_CALL; cache v9"
```

---

## Task 3: TypeScript import-derived external bindings

**Files:**
- Modify: `src/graphite/extract/ast.py:199` (call site), `:277-279` (pass the set), `:317-347` (`_collect_ts_import_symbols`), `:350-368` (add sibling iterator)
- Test: `tests/test_external_calls.py`

**Interfaces:**
- Consumes: `_call_confidence(called, external_names)` from Task 2.
- Produces: `_collect_ts_import_symbols` now returns `_ImportBindings(resolved: dict[str, str], external: frozenset[str])` instead of a bare dict. The only caller is `_extract_ts_like` at line 199.

**Grammar note (verified against `tree_sitter_typescript` in this repo):** an `import_statement` has an optional `import_clause` child. Inside it, a default import is a bare `identifier`; a namespace import is a `namespace_import` node containing an `identifier`; named imports sit in a `named_imports` node as `import_specifier` children. A side-effect-only import (`import './x'`) has **no** `import_clause`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_external_calls.py`:

```python
def test_named_import_from_external_package_is_tagged(tmp_path):
    _write(
        tmp_path / "src" / "t.ts",
        "import { z } from 'zod';\n"
        "export function go() { return z.object({}); }\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "src/t.ts")} == {"EXTERNAL_CALL"}


def test_default_import_from_external_package_is_tagged(tmp_path):
    _write(
        tmp_path / "src" / "t.ts",
        "import axios from 'axios';\n"
        "export function go() { return axios.get('/x'); }\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "src/t.ts")} == {"EXTERNAL_CALL"}


def test_namespace_import_from_external_package_is_tagged(tmp_path):
    _write(
        tmp_path / "src" / "t.ts",
        "import * as lib from 'some-lib';\n"
        "export function go() { return lib.run(); }\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "src/t.ts")} == {"EXTERNAL_CALL"}


def test_aliased_named_import_uses_the_local_name(tmp_path):
    _write(
        tmp_path / "src" / "t.ts",
        "import { parse as p } from 'yaml';\n"
        "export function go() { return p('x'); }\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "src/t.ts")} == {"EXTERNAL_CALL"}


def test_in_repo_import_is_not_tagged_external(tmp_path):
    """The discriminating case: same syntax, resolvable module."""
    _write(tmp_path / "src" / "dep.ts", "export function dep() { return 1; }\n")
    _write(
        tmp_path / "src" / "t.ts",
        "import { dep } from './dep';\nexport function go() { return dep(); }\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "src/t.ts")} == {"LOCAL_CALL"}
```

- [ ] **Step 2: Run to verify they fail**

```
python -m pytest tests/test_external_calls.py -q > out.txt 2>&1; echo $LASTEXITCODE
```
Expected: FAIL — the four external cases report `LOCAL_CALL`. `test_in_repo_import_is_not_tagged_external` should already PASS; if it fails, stop — the fixture is wrong, not the code.

- [ ] **Step 3: Add the bindings container**

In `src/graphite/extract/ast.py`, above `_collect_ts_import_symbols` (before line 317):

```python
@dataclass(frozen=True)
class _ImportBindings:
    """What a file's import statements bind.

    ``resolved`` maps a local name to the definition node id in the in-repo
    file that exports it (used for call resolution). ``external`` holds local
    names bound by imports that did NOT resolve in-repo, in every binding form
    -- those calls leave the repo and are tagged EXTERNAL_CALL.
    """
    resolved: dict[str, str]
    external: frozenset[str]
```

- [ ] **Step 4: Collect external bindings**

Replace the body of `_collect_ts_import_symbols` (lines 326-347) with:

```python
    symbols: dict[str, str] = {}
    external: set[str] = set()
    if source_index is None:
        return _ImportBindings(symbols, frozenset())
    for node in root.children:
        if node.type != "import_statement":
            continue
        source_lit = None
        clause = None
        for child in node.children:
            if child.type == "string":
                source_lit = child.text.decode("utf-8", errors="ignore").strip("'\"")
            elif child.type == "import_clause":
                clause = child
        if not source_lit or clause is None:
            continue
        resolved = _resolve_import(rel_path, source_lit, source_index)
        if resolved is None:
            # Unresolved module: every name it binds leaves the repo.
            external.update(_iter_bound_local_names(clause))
            continue
        target_file_id = _file_node_id(resolved.rel_path)
        for local, original in _iter_named_imports(clause):
            symbols[local] = _make_id(target_file_id, original)
    return _ImportBindings(symbols, frozenset(external))
```

Update the docstring's first line to `"""What this file's imports bind: resolved definitions and external names."""` and keep the existing explanation of why only named imports are mapped for *resolution*.

Add the sibling iterator after `_iter_named_imports` (after line 368):

```python
def _iter_bound_local_names(clause: Any):
    """Yield every local name a TS import clause binds.

    `_iter_named_imports` is deliberately narrower: resolution needs the
    original export name, so it handles only `named_imports`. Externality needs
    only the local name, so all three binding forms count here -- otherwise
    `import axios from 'axios'` and `import * as lib from 'lib'` stay invisible.
    """
    for child in clause.children:
        if child.type == "identifier":          # import axios from 'axios'
            if child.text:
                yield child.text.decode("utf-8", errors="ignore")
        elif child.type == "namespace_import":  # import * as lib from 'lib'
            for sub in child.children:
                if sub.type == "identifier" and sub.text:
                    yield sub.text.decode("utf-8", errors="ignore")
        elif child.type == "named_imports":     # import { a, b as c } from 'lib'
            for spec in child.children:
                if spec.type != "import_specifier":
                    continue
                alias_node = spec.child_by_field_name("alias")
                name_node = spec.child_by_field_name("name")
                chosen = alias_node if alias_node is not None else name_node
                if chosen is not None and chosen.text:
                    yield chosen.text.decode("utf-8", errors="ignore")
```

- [ ] **Step 5: Use the bindings at the call site**

Line 199 becomes:

```python
    bindings = _collect_ts_import_symbols(root, rel_path, source_index)
```

Line 277 uses the resolved map, and line 279 passes the external set:

```python
                    target_id = _resolve_call(file_id, called, bindings.resolved)
                    _materialize(scope)
                    edge = _edge(scope.id, target_id, "calls", rel_path, _line(node), confidence=_call_confidence(called, bindings.external))
```

- [ ] **Step 6: Run the tests**

```
python -m pytest tests/test_external_calls.py -q > out.txt 2>&1; echo $LASTEXITCODE
```
Expected: exit 0.

- [ ] **Step 7: Run the full suite**

```
python -m pytest tests/ -q > out.txt 2>&1; echo $LASTEXITCODE
```
Expected: exit 0. `tests/test_typescript_resolver.py` exercises the resolved path — if it fails, `bindings.resolved` was not wired at line 277.

- [ ] **Step 8: Commit**

```bash
git add src/graphite/extract/ast.py tests/test_external_calls.py
git commit -m "feat(extract): tag TS calls bound by unresolved imports as EXTERNAL_CALL"
```

---

## Task 4: Python import-derived external bindings

**Files:**
- Modify: `src/graphite/extract/ast.py:569-638` (`_collect_python_import_maps`), `:665` (call site), `:730-741` (call branches)
- Test: `tests/test_external_calls.py`

**Interfaces:**
- Consumes: `_call_confidence(called, external_names)` from Task 2.
- Produces: `_collect_python_import_maps` returns a 3-tuple `(symbol_map, alias_map, external_names)` where `external_names: frozenset[str]`. The only caller is `_extract_python` at line 665.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_external_calls.py`:

```python
def test_python_plain_import_of_external_module_is_tagged(tmp_path):
    _write(
        tmp_path / "m.py",
        "import pathlib\n"
        "def go():\n"
        "    return pathlib.Path('.')\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "m.py")} == {"EXTERNAL_CALL"}


def test_python_aliased_import_uses_the_alias(tmp_path):
    _write(
        tmp_path / "m.py",
        "import numpy as np\n"
        "def go():\n"
        "    return np.array([])\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "m.py")} == {"EXTERNAL_CALL"}


def test_python_from_import_of_external_symbol_is_tagged(tmp_path):
    _write(
        tmp_path / "m.py",
        "from dataclasses import dataclass\n"
        "def go():\n"
        "    return dataclass()\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "m.py")} == {"EXTERNAL_CALL"}


def test_python_dotted_import_binds_the_root(tmp_path):
    """`import os.path` binds `os`, so `os.path.join()` is external."""
    _write(
        tmp_path / "m.py",
        "import os.path\n"
        "def go():\n"
        "    return os.path.join('a')\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "m.py")} == {"EXTERNAL_CALL"}


def test_python_in_repo_import_stays_local(tmp_path):
    """The discriminating case: same syntax, resolvable module."""
    _write(tmp_path / "dep.py", "def helper():\n    return 1\n")
    _write(
        tmp_path / "m.py",
        "from dep import helper\n"
        "def go():\n"
        "    return helper()\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "m.py")} == {"LOCAL_CALL"}
```

- [ ] **Step 2: Run to verify they fail**

```
python -m pytest tests/test_external_calls.py -q > out.txt 2>&1; echo $LASTEXITCODE
```
Expected: FAIL on the four external cases. `test_python_in_repo_import_stays_local` should already PASS.

- [ ] **Step 3: Collect external names**

In `_collect_python_import_maps`, change the signature and docstring return line to a 3-tuple:

```python
def _collect_python_import_maps(
    root: Any, rel_path: str, source_index: SourceIndex | None
) -> tuple[dict[str, str], dict[str, str], frozenset[str]]:
    """(symbol_map, alias_map, external_names).

    symbol_map: local -> definition node id. alias_map: local -> module file id.
    external_names: local names bound by imports that did NOT resolve in-repo --
    calls through them leave the repo (EXTERNAL_CALL).

    Walked at ALL depths (Python allows function-local imports). For
    `from P import name`, `P.name` is tried as a MODULE first (alias), then
    as a symbol defined in P's file. Unresolvable modules enter neither map,
    but DO enter external_names.
    Last binding wins, matching Python shadowing.
    """
    symbol_map: dict[str, str] = {}
    alias_map: dict[str, str] = {}
    external: set[str] = set()
    if source_index is None:
        return symbol_map, alias_map, frozenset()
```

In the `import_statement` branch, record the bound root when resolution fails:

```python
                if child.type == "dotted_name":
                    module = _text(child)
                    if module and "." not in module:
                        resolved = source_index.resolve_python_module(rel_path, module)
                        if resolved:
                            alias_map[module] = _file_node_id(resolved)
                        else:
                            external.add(module)
                    elif module:
                        # `import os.path` binds only the root name `os`.
                        external.add(module.split(".", 1)[0])
                elif child.type == "aliased_import":
                    module = _text(child.child_by_field_name("name"))
                    local = _text(child.child_by_field_name("alias"))
                    if module and local:
                        resolved = source_index.resolve_python_module(rel_path, module)
                        if resolved:
                            alias_map[local] = _file_node_id(resolved)
                        else:
                            external.add(local)
```

In the `import_from_statement` branch, the existing `continue`/fall-through ends with `if parent: symbol_map[local] = ...`. Add the else:

```python
                    parent = source_index.resolve_python_module(rel_path, base_module, dots)
                    if parent:
                        symbol_map[local] = _make_id(_file_node_id(parent), original)
                    else:
                        external.add(local)
```

Change the final `return symbol_map, alias_map` (line 638) to:

```python
    return symbol_map, alias_map, frozenset(external)
```

- [ ] **Step 4: Use it at the call site**

Line 665 becomes:

```python
    symbol_map, alias_map, external_names = _collect_python_import_maps(root, rel_path, source_index)
```

Line 732 and line 740 pass the set:

```python
                edge = _edge(scope_id, target, "calls", rel_path, _line(node), confidence=_call_confidence(bare, external_names))
```
```python
                    edge = _edge(scope_id, _resolve_call(file_id, dotted), "calls", rel_path, _line(node), confidence=_call_confidence(dotted, external_names))
```

Line 736 (the `alias_map` branch) stays `confidence="LOCAL_CALL"` — reached only when the module resolved in-repo.

- [ ] **Step 5: Run the tests**

```
python -m pytest tests/test_external_calls.py -q > out.txt 2>&1; echo $LASTEXITCODE
```
Expected: exit 0.

- [ ] **Step 6: Run the full suite**

```
python -m pytest tests/ -q > out.txt 2>&1; echo $LASTEXITCODE
```
Expected: exit 0. `tests/test_python_resolver.py` covers the resolved paths; a failure there means the 3-tuple was not unpacked correctly at line 665.

- [ ] **Step 7: Commit**

```bash
git add src/graphite/extract/ast.py tests/test_external_calls.py
git commit -m "feat(extract): tag python calls bound by unresolved imports as EXTERNAL_CALL"
```

---

## Task 5: Retire and replace the TypeScript caveat

**Files:**
- Modify: `src/graphite/answer_contract.py:36-42`
- Test: `tests/test_answer_contract.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ts-external-calls-unclassified` carries `retired_by` and no longer appears in `active_caveats()`. A new entry `ts-destructured-locals-unbound` appears instead.

**Why not amend in place:** the registry's process rule (`answer_contract.py:25-27`) is that a published code's meaning never changes — fixed classes get `retired_by`. The old code covered external-package symbols, runtime globals, **and destructured locals**; this round fixes only the first two, so retiring it without a successor would silently drop a live blindspot.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_answer_contract.py`:

```python
def test_ts_external_calls_caveat_is_retired_with_a_successor():
    from graphite.answer_contract import CAVEAT_REGISTRY, active_caveats

    by_code = {e["code"]: e for e in CAVEAT_REGISTRY}
    assert by_code["ts-external-calls-unclassified"]["retired_by"]
    active = {e["code"] for e in active_caveats()}
    assert "ts-external-calls-unclassified" not in active
    assert "ts-destructured-locals-unbound" in active
    successor = by_code["ts-destructured-locals-unbound"]
    assert successor["relations"] == ("calls",)
    assert successor["languages"] == ("typescript", "javascript")
```

- [ ] **Step 2: Run to verify it fails**

```
python -m pytest tests/test_answer_contract.py -q > out.txt 2>&1; echo $LASTEXITCODE
```
Expected: FAIL with `KeyError: 'retired_by'`.

- [ ] **Step 3: Implement**

Replace the second registry entry (lines 36-42) with:

```python
    {
        "code": "ts-external-calls-unclassified",
        "relations": ("calls",),
        "languages": ("typescript", "javascript"),
        "summary": "calls to external-package symbols, runtime globals, and destructured locals count as unbound",
        "since": "2026-07-26",
        "retired_by": "2026-07-27",
    },
    {
        "code": "ts-destructured-locals-unbound",
        "relations": ("calls",),
        "languages": ("typescript", "javascript"),
        "summary": "calls through destructured local bindings (const { f } = require(...)) count as unbound",
        "since": "2026-07-27",
    },
```

- [ ] **Step 4: Run the tests**

```
python -m pytest tests/test_answer_contract.py -q > out.txt 2>&1; echo $LASTEXITCODE
```
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/graphite/answer_contract.py tests/test_answer_contract.py
git commit -m "feat(contract): retire ts-external-calls-unclassified, add destructured-locals successor"
```

---

## Task 6: Document the schema-3 consumer contract

**Files:**
- Modify: `docs/agent-integration.md:109-149`

**Interfaces:**
- Consumes: the shape produced by Task 1.
- Produces: no code.

- [ ] **Step 1: Update the example block**

Replace the sample health block at `docs/agent-integration.md:116-129` with this, so it matches exactly what `resolution_health` now emits (schema 3, `external` on both relations):

```json
{
  "schema": 3,
  "placeholder_nodes": {"total": 4519, "unknown": 2463, "share": 0.545},
  "by_relation": {
    "calls":   {"total": 6631, "bound": 1730, "ratio": 0.261, "external": 412},
    "imports": {"total": 1920, "bound": 1918, "ratio": 0.999, "external": 127}
  },
  "by_language": {"python": {"calls": {"total": 6631, "bound": 1730, "ratio": 0.261, "external": 412},
                              "imports": {"total": 1920, "bound": 1918, "ratio": 0.999, "external": 127}}},
  "healthy": false,
  "threshold": 0.8
}
```

- [ ] **Step 2: Replace the schema-2 paragraph**

Replace the bullet at `:133-138` (`**schema 2**: imports cells carry an `external` count…`) with:

```markdown
- **schema 3**: BOTH `calls` and `imports` cells carry an `external` count of
  edges that leave the repo. For `imports` these are stdlib/pip/node_modules
  modules (`confidence="EXTERNAL_IMPORT"`); for `calls` they are calls to
  external-package symbols and never-imported runtime globals
  (`confidence="EXTERNAL_CALL"`). Ratios count only should-bind-in-repo edges
  and exclude externals. An edge counts as external only when it ALSO failed to
  bind — externality never removes a bound edge from the ratio.
  Ratios are **not comparable across schemas**: schema 1 includes all externals,
  schema 2 excludes only import externals, schema 3 excludes both. Branch on
  `schema` when reading ratios. Consumers reading only `healthy` need no change.
```

- [ ] **Step 3: Correct the "discriminating signal" guidance**

The passage at `:146-149` tells consumers the `calls` ratio is the discriminating health signal *because* `imports` is saturated by construction. Schema 3 partially undoes that. Replace that sentence with:

```markdown
  Because imports is structurally near-saturated on a healthy graph, it stops
  being the signal that tells healthy repos apart from unhealthy ones. Under
  schema 3 the `calls` ratio also rises once externals are excluded, so a high
  `calls` ratio no longer implies deep binding coverage on its own — read it
  alongside the `external` count and `placeholder_nodes.share`, and prefer the
  answer-scoped `answer.grade` over any aggregate ratio for a specific question.
```

- [ ] **Step 4: Verify no stale schema-2 claims remain**

This repo is developed on Windows/PowerShell — `grep` is not available. Use:

```powershell
Select-String -Path docs/agent-integration.md -Pattern 'schema 2|"schema": 2'
```

Expected: no hits other than the cross-schema comparison sentence added in Step 2.

**No `DOC_VERSION` bump is needed for this task.** `DOC_VERSION` (`init.py:17`) pairs with the *managed instruction templates* that `graphite init` writes into consumer repos, pinned by `test_template_change_requires_doc_version_bump`. `docs/agent-integration.md` is a published contract document, not one of those templates, so it changes independently. If the full suite reports that pinning test failing, a template *was* touched — stop and reconsider rather than bumping to make it green.

- [ ] **Step 5: Commit**

```bash
git add docs/agent-integration.md
git commit -m "docs(agent-integration): publish health schema 3 and correct the discriminating-signal guidance"
```

---

## Task 7: Pure-relabel invariant and the masking falsifier

**Files:**
- Test: `tests/test_external_calls.py`

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: no source change. This task is the guard on the round's headline claim.

**Why this task exists:** the round claims classification, not deletion, and claims the ratio still reflects real binding failure. Both are easy to break in ways every other test would miss — a change that dropped external calls instead of tagging them would make every Task 2-4 test pass, and a `_call_confidence` that returned `EXTERNAL_CALL` unconditionally would make the ratio 1.00 everywhere.

- [ ] **Step 1: Write the invariant and falsifier tests**

Append to `tests/test_external_calls.py`:

```python
from graphite.graph import build_graph
from graphite.health import resolution_health


def _mixed_fixture(tmp_path: Path) -> None:
    """Six calls, one of each kind, counted by hand from the source below.

    src/dep.ts   -- in-repo definition
    src/t.ts     -- dep() local, z.object() external-import,
                    crypto.randomUUID() external-global, missing() in-repo MISS
    src/t.test.ts -- it(), expect()  external-globals

    All six survive `should_keep_call_target`: `crypto` is not in
    `_BUILTIN_OBJECTS` and `randomUUID`/`object` are not in
    `_NOISY_MEMBER_CALLS` (both checked in src/graphite/resolve.py:15-27).
    """
    _write(tmp_path / "src" / "dep.ts", "export function dep() { return 1; }\n")
    _write(
        tmp_path / "src" / "t.ts",
        "import { dep } from './dep';\n"
        "import { z } from 'zod';\n"
        "export function go() {\n"
        "  dep();\n"
        "  z.object({});\n"
        "  crypto.randomUUID();\n"
        "  missing();\n"
        "  return 1;\n"
        "}\n",
    )
    _write(
        tmp_path / "src" / "t.test.ts",
        # No chained assertion: `expect(1).toBe(1)` emits a SECOND calls edge
        # for `toBe` (the member call on the result), which would make the
        # counts below wrong. Keep the fixture to unchained calls.
        "it('works', () => { expect(1); });\n",
    )


def test_every_call_still_produces_an_edge(tmp_path):
    """Pure-relabel invariant: classification must not drop edges.

    The fixture contains exactly six calls that survive the drop-list and the
    phantom filter. If a future change 'improves' the ratio by deleting noisy
    calls instead of tagging them, this count falls and the test fails.
    """
    _mixed_fixture(tmp_path)
    result = _extract(tmp_path)
    total = len(_calls(result, "src/t.ts")) + len(_calls(result, "src/t.test.ts"))
    assert total == 6


def test_classification_splits_as_expected(tmp_path):
    _mixed_fixture(tmp_path)
    result = _extract(tmp_path)
    edges = _calls(result, "src/t.ts") + _calls(result, "src/t.test.ts")
    external = [e for e in edges if e["confidence"] == "EXTERNAL_CALL"]
    local = [e for e in edges if e["confidence"] == "LOCAL_CALL"]
    # z.object, crypto.randomUUID, expect, it
    assert len(external) == 4
    # dep() and missing() -- the in-repo hit and the in-repo MISS
    assert len(local) == 2


def test_genuine_in_repo_miss_still_counts_unbound(tmp_path):
    """The falsifier. A real binding failure must survive classification.

    `missing()` is called but never defined, so it stays LOCAL_CALL, stays
    unbound, and must still drag the calls ratio below 1.0. A version of this
    feature that tags everything external would report 1.0 here and pass every
    other test in this file.
    """
    _mixed_fixture(tmp_path)
    result = _extract(tmp_path)
    g = build_graph(result.nodes, result.edges)
    cell = resolution_health(g)["by_relation"]["calls"]
    assert cell["external"] == 4
    assert cell["ratio"] is not None
    assert cell["ratio"] < 1.0, "the unresolved missing() call must still count against the ratio"
```

- [ ] **Step 2: Run the tests**

```
python -m pytest tests/test_external_calls.py -q > out.txt 2>&1; echo $LASTEXITCODE
```

**If the counts in `test_every_call_still_produces_an_edge` or `test_classification_splits_as_expected` are off, do NOT simply change the numbers to match.** First determine *why*: `should_keep_call_target` drops member calls whose root is a builtin object or whose property is a noisy member name, and `.toBe(1)` is a chained member call that may or may not survive that filter. Read the actual edges before adjusting:

```python
for e in _calls(result, "src/t.ts") + _calls(result, "src/t.test.ts"):
    print(e["target"], e["confidence"])
```

Then correct the expected counts **and** the fixture docstring together, so the comment stays true. A count that was silently edited to match output tests nothing.

- [ ] **Step 3: Verify `build_graph`'s signature before relying on it**

The falsifier test calls `build_graph(result.nodes, result.edges)`. That call shape is **unverified** — confirm the real signature and adapt if it differs:

```powershell
Select-String -Path src/graphite/graph.py -Pattern 'def build_graph' -Context 0,8
```

`tests/test_python_resolver.py` already imports `build_graph`; copy its call shape from there if this one does not match.

- [ ] **Step 4: Run the full suite**

```
python -m pytest tests/ -q > out.txt 2>&1; echo $LASTEXITCODE
```
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add tests/test_external_calls.py
git commit -m "test: pure-relabel invariant and the in-repo-miss falsifier"
```

---

## After the plan

Live acceptance (spec §10) runs **pre-merge**, with the falsifier stated before each run. It is not part of the task loop — the controller runs it after Task 7:

- **A1** — pawscout `calls` 0.667 → ≥ 0.89 with `graph.json` edge count unchanged. Fails if the ratio rises *and* the edge count drops.
- **A2** — graphite's own `calls` 0.896 → ≥ 0.92, `python` cell reports non-zero `external`. Should be beaten, not merely met (spec §10).
- **A3** — open-design and pivot-parlor (0.562) move materially. Recorded, not required to cross 0.8.
- **A4** — a graph with a real in-repo binding gap still reports `healthy: false`.
- **A5** — `schema` reads 3 in `stats`, `impact`, `context`, relation-verb JSON, `check --json`, `graph.json` `analysis.resolution_health`, and `.graphite_analysis.json`.
- **A6** — full suite green, exit code read directly.

Rollout (spec §12) then: merge, re-init the five managed consumers, **restart the daemon** (extraction is a daemon-executed surface and this round changes it), and correct the machine `CLAUDE.md` doctrine — the "TypeScript `calls` is systemically under-bound" block and the "calls ratio is the discriminating signal" line — using the **measured** post-rollout numbers, not this plan's projections.

Rollout gates on a **marker survey**, not `graphite init`'s exit code: `init` cannot upgrade a doc it classifies as *legacy unversioned* and still exits 0 (issue #13).
