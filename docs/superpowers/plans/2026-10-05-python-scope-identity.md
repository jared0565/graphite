# Python Scope Identity (phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every Python method and nested definition gets its own graph node, and every bare-name call binds to what CPython would resolve the name to. `callers`, `calls` and `impact` then stop conflating same-named definitions (#70) and stop binding calls made through local names (#71).

**Architecture:** A new module, `graphite/extract/pyscope.py`, builds Python's scope tree in one tree-sitter pass and resolves bare names. It follows CPython's lookup rules: innermost binding scope first, enclosing class scopes skipped from inside a function, `global` and `nonlocal` honoured, imports bound per scope. `_extract_python` reads that table:
- module-scope definitions keep `_make_id(file, name)`;
- every other definition gets `_scoped_id(file, qualname)`;
- each bare call's edge follows what the name denotes in the call's scope.

An independent oracle, `scripts/pyscopeoracle.py`, grades the extractor on graphite and Django. It is built on CPython's `symtable` and `ast`, and it runs FIRST, against today's engine, as the control.

**Tech Stack:** Python 3.11+ (dev venv on 3.14), tree-sitter through graphite's `_LOADER`, CPython `symtable` and `ast`, pytest, networkx.

**Spec:** `docs/superpowers/specs/2026-10-04-python-scope-identity-design.md`. This plan covers phase 1 only; phase 2 (value references) is out of scope.

## Spec refinements: read these before reviewing

You approved the spec as written. These are the places where this plan reads it more narrowly, or makes explicit something it left implicit. The owning task's tests pin each one.

1. **The oracle reads the extraction stage, not the built graph.** Spec §5 says "against the BUILT graph's edges". But the built graph keeps one edge per (source, target, relation), so it cannot say which call site an edge came from, and per-site truth needs exactly that. `dump` runs graphite's own `_resolve_method_dispatch` over the same pre-merge edges, so member calls are still compared as they reach the graph. (Task 4)
2. **Confidence follows the binder.** A name bound in the file is external only if its binder is an import that did not resolve in-repo (§4.2). A name bound nowhere in the file falls back to Python's builtins scope, so `_EXTERNAL_GLOBALS` still classifies it. This has two observable effects, each pre-registered as its own compare category:
   - **(a)** A local value named like a global, as in `for it in items: it.process()`, changes from EXTERNAL_CALL to LOCAL_CALL. That makes the member call eligible for name dispatch, because the evidence gate from #56 refuses only EXTERNAL_CALL. These are counted as `confidence-EXTERNAL_CALL->LOCAL_CALL` and `confidence-flip-then-dispatched`.
   - **(b)** A module-level `def test()` called at module scope changes from EXTERNAL_CALL to LOCAL_CALL, and is bound. (Task 6)
3. **An annotation with no value (`x: int`) binds only in a function.** CPython makes it local there. At module or class scope it binds nothing. (Tasks 3, 4)
4. **Two unresolved imports of one name stay external**, as in `try: from ujson import loads` / `except ImportError: from json import loads`. The spec's table would call two binders "local-value", but both leave the repo whichever one runs. (Task 3)
5. **A comprehension's first iterable is evaluated in the enclosing scope**, as in CPython. So `xs = [y for y in m()]` in a class body binds `K.m`, while the comprehension's element still cannot see the class. The validated prototype did not model this; this plan does. (Tasks 3, 4)
6. **`nonlocal` is handled by moving binders, with no lookup branch.** The prototype's lookup-side branch was measured unfalsifiable: once the binders sit in the owning function, the ordinary walk reaches that function first. (Task 3)
7. **The `qualname` match tier matches dotted tokens only.** A module-level qualname equals its name, so an undotted tier would relabel every module-level `name` match as `qualname`. (Task 7)
8. **`path-suffix` keeps its existing cap of four alternates.** The spec's "stays capped at 3" is the name tier's existing cap. `alternates_total` appears wherever `alternates` does. (Task 7)
9. **Two published schemas gain an optional field.** That is an additive change, a minor release under `docs/compatibility.md`:
   - search-result rows gain `qualname`;
   - query-result `resolution[]` items gain `alternates_total`.

   graphite's subset validator treats both objects as closed, so the fields must be declared. (Task 7)
10. **The oracle's exclusions are named categories, not silence.**
    - `truth-rebound`: a name that some function declares `global` or `nonlocal` and then binds.
    - `truth-ambiguous`: two or more def, class or import binders, if/else redefinitions included.
    - `unbound-but-def(reexport)`: the residual spec §7 already names.

    All three are reported and none is scored. (Task 4)
11. **Calls in a def's defaults keep today's caller.** They are attributed to the def itself, as now, even though they now resolve in the enclosing scope. (Task 6)
12. **Six existing negative assertions would pass vacuously once method ids change.** Each checks that some edge is absent; with a new id that edge can never exist, so the check passes whatever the code does. They are rewritten to look ids up by (file, qualname), which raises when the definition is missing. Each is then proven by disabling the gate it guards (Task 8). One more guard in `test_call_graph.py`, `src == "mod"`, was already vacuous because the file id is `mod_py`; it is fixed in passing. (Task 6)

## Global Constraints

- **Python only.** TypeScript, JavaScript, Go and Rust extraction, ids and ranking are untouched (spec decision 3).
- **Module-scope definitions keep their id byte-for-byte**: `_make_id(file_id, name)`. This includes a def inside a module-level `if` or `try`.
- **Every other definition** gets `_scoped_id(file_id, qualname)`:
  - it is ALWAYS discriminated, and never goes through `_make_id`;
  - its marker is blake2s over `"py-scope\x00" + file_id + "\x00" + qualname`, with `digest_size=_ID_DISCRIMINATOR_LEN // 2`.
- **A local-value call keeps an edge**, marked `LOCAL_CALL` and unbound. Its target is today's placeholder, `_resolve_call(file_id, n)`. If a module-scope def or class named `n` exists, the target is instead `_scoped_id(file_id, "<local>." + n)`.
- **Names in `_LANGUAGE_BUILTIN_GLOBALS` are skipped, as today.**
- **Member calls (`obj.m()`) keep their shape and still go through `_resolve_method_dispatch`.** Only the root's confidence reads the scope walk.
- **No `cache_version` bump** (§4.6: the extraction cache already partitions on engine identity). Checked 2026-10-05: `engine_identity` hashes EVERY `.py`/`.mjs` under the package root, walked recursively (`_collect_engine_files`), not a hand-kept list. So the new `extract/pyscope.py` joins the fingerprint by construction: 113 files against a cap of 512. A later fix to `pyscope.py` alone therefore still invalidates the cache and still triggers the daemon's #18 rebuild.
- **The oracle imports only names that exist in graphite 1.1.1**:
  - from `extract/ast.py`: `_python_call_target`, `_edge`, `_extract_python`, `_file_node_id`, `_resolve_method_dispatch`, `_LOADER`;
  - `Config`, `collect_files` and `SourceIndex`.
- **`_extract_python` calls `_python_call_target` and `_edge` as module globals** (never cached in locals). It also creates each call's edge BEFORE walking that call's children. The oracle's site pairing depends on both.
- **Tests run in the dev venv, from the repo root.**
  - Command: `$PY -m pytest ...`, with `PY=../.venvs/graphite-dev/Scripts/python.exe`.
  - The machine `pytest` imports the 1.1.1 wheel, so it would pass on code it never loaded.
  - Redirect output to a file and read the exit code from `$?` directly, never through a pipe.
- **Before any push, run mypy:** `python -m mypy <changed files under src/graphite>` with the machine python. pytest does not run mypy; the pre-push gate does.
- **Gates:**
  - run `aramid check --staged` before every commit;
  - never pass `--no-verify`;
  - suppress a WARN only with `aramid override <id> --reason "..."`.
- **Every `git commit` and every `git push` needs the maintainer's explicit go, each time.**
  - A subagent stops at "staged, `aramid check --staged` clean" and reports back; the controller asks.
  - Never commit while a push's gate is running.
  - Never edit source, or change the Python environment, while a gate is running. Task 8 mutates source files.
- **Paste the plan's code verbatim; do not reformat it.** Task 8's mutation anchors must match the code from Tasks 3, 6 and 7 byte for byte. A reflowed line makes `mutants.py` report `BAD` for that mutant: loud, but it costs a round. Do not run a formatter over these files.
- **Graph first.** Cross-file questions go to `python -m graphite query`, `context`, `impact` or `search` before any grep. The strict hook falsely denies literal greps whose text contains a symbol name (#72). When that happens, fall back to the Grep tool on one file, and say so.
- **Paths:**
  - Scratch files (corpora, the mutation script, measurement output) go in the session scratchpad: `SCRATCH=<your session scratchpad directory>`.
  - Release evidence goes to `EVIDENCE=/f/Projects/.graphite-releases/phase1-scope-identity`, which RELEASING.md's store already holds.
- **Stay inside this repository.** The two corpora are scratch exports (a `git archive` and an sdist), not repositories.
- **No channel message about this work** (spec §6).

## Review Focus

1. **Decorated methods and properties**, such as `@property`, `@staticmethod` or a decorated nested helper. Expect a scope-qualified id, `qualname` set, `is_method` on class members, and calls inside them resolving normally. Pinned in Task 6 by `test_decorated_methods_get_scoped_ids`.
2. **A recursive nested helper whose name recurs in another function.** Example: `def a(): def visit(n): visit(...)` beside `def b(): def visit(n): ...`, the shape of `extract/ast.py`'s own nested `visit`. Each call must bind to its own `visit`. Pinned in Tasks 3 and 6.
3. **A method's default argument naming a class-level helper**, as in `def m(self, x=factory())`. It is evaluated in the class body, so it binds `K.factory`, and is attributed to `m` as today. Pinned in Tasks 3 and 6.
4. **A class-body comprehension iterating a class-level name**, as in `xs = [y for y in m()]`. The first iterable sees the class scope; the element does not. Pinned in Tasks 3 and 4.
5. **A bare-name query over many same-named definitions**, such as `callers fake_build` over six nested defs:
   - the shallowest module-level definition wins, deterministically;
   - `alternates` is capped at three, and `alternates_total` gives the true count;
   - `callers t0.fake_build` selects exactly one definition.

   Pinned in Task 7.

## File structure

| File | Change | Responsibility |
|---|---|---|
| `src/graphite/extract/pyscope.py` | create | Python scope tree and bare-name resolution (pure; computes no ids) |
| `src/graphite/extract/ast.py` | modify | adds `_location_order`, `_scoped_id`, `_python_import_bindings`, `_python_definition_name`, `_python_def_id`, `_python_name_confidence`, `_python_bare_call`; rewires `_extract_python`; deletes `_collect_python_import_maps` |
| `src/graphite/query.py` | modify | `NodeMatch`, the `qualname` tier, `_scope_rank`, `alternates_total`, `qualname` in `_node_view` |
| `src/graphite/context.py` | modify | reads `NodeMatch` fields; emits `alternates_total` |
| `src/graphite/answer_contract.py` | modify | retires `python-nested-name-shared-id` and `python-bare-call-ignores-scope` |
| `scripts/pyscopeoracle.py` | create | the oracle: `sites`, `dump`, `compare` |
| `docs/schemas/query-result.v1.schema.json`, `docs/schemas/search-result.v1.schema.json` | modify | declare `alternates_total` and `qualname` |
| `docs/agent-integration.md`, `docs/knowledge-base.md`, `CHANGELOG.md` | modify | the documented surface |
| `tests/test_python_scope_identity.py` | create | merge location, `_scoped_id`, extraction, query |
| `tests/test_pyscope.py` | create | scope rules |
| `tests/test_pyscopeoracle.py` | create | the oracle's known answers, plus the control |
| `tests/test_python_import_bindings.py` | create | per-statement import bindings |
| `tests/test_dispatch_evidence.py`, `tests/test_method_dispatch_scope.py`, `tests/test_python_resolver.py`, `tests/test_call_graph.py` | modify | ids looked up, not spelled |
| `tests/test_published_schemas.py`, `tests/test_answer_contract.py` | modify | a schema graph that can see the new fields; the retired registry entries |

Task order:
1. merge location
2. scoped id
3. pyscope
4. the oracle, plus its CONTROL run on today's engine
5. import bindings (a refactor; output must not change)
6. rewire `_extract_python`
7. query surface
8. mutation proofs
9. measurements
10. registry, docs and CHANGELOG
11. node-id holder audit
12. final gate

Every shell block below assumes Git Bash at the repo root, with:

```bash
PY=../.venvs/graphite-dev/Scripts/python.exe
SCRATCH=<your session scratchpad directory>
EVIDENCE=/f/Projects/.graphite-releases/phase1-scope-identity
```

---

### Task 1: `_merge` keeps the numerically first location

**Files:**
- Modify: `src/graphite/extract/ast.py`: add `_location_order` directly above `def _merge`, and change the last key of `_merge`'s edge sort.
- Create: `tests/test_python_scope_identity.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_location_order(location: str | None) -> tuple[int, str]`.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_python_scope_identity.py`:

```python
"""Python scope identity (#70, #71): one node per definition, bare names bound the
way CPython resolves them.

Spec: docs/superpowers/specs/2026-10-04-python-scope-identity-design.md.
"""
from __future__ import annotations

from graphite.extract.ast import ExtractionResult, _edge, _merge


def test_merge_keeps_the_numerically_first_location():
    """As strings `L12` sorts before `L9`, so the surviving edge named a later site (§4.5)."""
    late = _edge("m_py_f", "m_py_g", "calls", "m.py", 12)
    early = _edge("m_py_f", "m_py_g", "calls", "m.py", 9)
    (edge,) = _merge([ExtractionResult(edges=[late, early])]).edges
    assert edge["source_location"] == "L9"
    assert edge["weight"] == 2.0


def test_merge_sorts_a_missing_location_after_every_line():
    unplaced = _edge("m_py_f", "m_py_g", "calls", "m.py")
    placed = _edge("m_py_f", "m_py_g", "calls", "m.py", 3)
    (edge,) = _merge([ExtractionResult(edges=[unplaced, placed])]).edges
    assert edge["source_location"] == "L3"
```

- [ ] **Step 2: Run the tests and watch them fail.**

```bash
$PY -m pytest tests/test_python_scope_identity.py -q > "$SCRATCH/t1.txt" 2>&1; echo "EXIT=$?"; tail -5 "$SCRATCH/t1.txt"
```

Expected: `EXIT=1`, with 2 failures: `assert 'L12' == 'L9'` and `KeyError: 'source_location'`.

- [ ] **Step 3: Implement.** In `src/graphite/extract/ast.py`, add directly above `def _merge(`:

```python
def _location_order(location: str | None) -> tuple[int, str]:
    """Sort key for an edge's `source_location`: the line NUMBER first (§4.5).

    As a string `L12` sorts before `L9`, so the edge `_merge` kept for a
    (source, target, relation) triple named an arbitrary site rather than the
    first one. A missing or non-`L<digits>` location sorts after every line.
    """
    if location and location[0] == "L" and location[1:].isdigit():
        return int(location[1:]), location
    return sys.maxsize, location or ""
```

In `_merge`'s `all_edges.sort(key=lambda e: (...))`, replace the last key element

```python
            e.get("source_location", ""),
```

with

```python
            _location_order(e.get("source_location")),
```

(`sys` is already imported at the top of `ast.py`.)

- [ ] **Step 4: Run the tests and the suites that read merged edges.**

```bash
$PY -m pytest tests/test_python_scope_identity.py tests/test_call_graph.py tests/test_python_resolver.py tests/test_method_dispatch_scope.py tests/test_dispatch_evidence.py tests/test_go_rust.py -q > "$SCRATCH/t1b.txt" 2>&1; echo "EXIT=$?"; tail -3 "$SCRATCH/t1b.txt"
```

Expected: `EXIT=0`.

- [ ] **Step 5: Commit, with the maintainer's go.**

```bash
git add src/graphite/extract/ast.py tests/test_python_scope_identity.py
aramid check --staged
git commit -m "fix(merge): keep the numerically first source_location of a merged edge"
```

---

### Task 2: `_scoped_id`

**Files:**
- Modify: `src/graphite/extract/ast.py`: add `_scoped_id` directly after `_make_id`.
- Test: `tests/test_python_scope_identity.py`

**Interfaces:**
- Consumes: `_make_id`, `_MAX_ID_LEN`, `_ID_DISCRIMINATOR_LEN` (existing).
- Produces: `_scoped_id(file_id: str, qualname: str) -> str`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_python_scope_identity.py`, change the import line to

```python
from graphite.extract.ast import _MAX_ID_LEN, ExtractionResult, _edge, _make_id, _merge, _scoped_id
```

and append:

```python
def test_a_scoped_id_always_carries_a_discriminator():
    assert _scoped_id("m_py", "Worker.run") == "m_py_worker_run_7c6b1a"


def test_a_nested_definition_never_shares_a_module_definitions_id():
    """`_make_id` folds `.` into `_` as lossless: the trap `_scoped_id` exists to avoid."""
    assert _make_id("m_py", "outer.inner") == _make_id("m_py", "outer_inner") == "m_py_outer_inner"
    assert _scoped_id("m_py", "outer.inner") == "m_py_outer_inner_b0dec9"


def test_scoped_ids_differ_by_file_and_by_qualname():
    assert _scoped_id("a_py", "K.run") != _scoped_id("b_py", "K.run")
    assert _scoped_id("m_py", "test_one.fake_build") == "m_py_test_one_fake_build_4e414e"
    assert _scoped_id("m_py", "test_two.fake_build") == "m_py_test_two_fake_build_1dc280"


def test_a_scoped_id_is_capped_at_the_id_length_limit():
    longest = _scoped_id("m_py", "a" * 300)
    assert len(longest) == _MAX_ID_LEN
    assert longest != _scoped_id("m_py", "a" * 299)


def test_the_local_phantom_is_not_a_definitions_id():
    """`<` is no identifier character, so no definition's qualname hashes to this marker."""
    assert _scoped_id("m_py", "<local>.generate") == "m_py_local_generate_1c9caf"
    assert _scoped_id("m_py", "<local>.generate") != _scoped_id("m_py", "local.generate")
```

(The literal ids above were computed on 2026-10-05 from exactly the implementation in Step 3.)

- [ ] **Step 2: Run the tests and watch them fail.**

```bash
$PY -m pytest tests/test_python_scope_identity.py -q > "$SCRATCH/t2.txt" 2>&1; echo "EXIT=$?"; tail -5 "$SCRATCH/t2.txt"
```

Expected: `EXIT=2`, with `ImportError: cannot import name '_scoped_id'`.

- [ ] **Step 3: Implement.** Add directly after `_make_id` in `src/graphite/extract/ast.py`:

```python
def _scoped_id(file_id: str, qualname: str) -> str:
    """Node id for a Python definition that is NOT at module scope (#70).

    `qualname` is the enclosing definitions' names and its own, joined by `.`
    (`Worker.run`, `test_one.fake_build`). Unlike `_make_id`, the hex marker is
    ALWAYS appended, and it is hashed over a namespace tag no `_make_id` input
    contains. `_make_id`'s ambiguity test would be the wrong tool here: it
    treats `.` -> `_` as lossless, so -- measured -- `_make_id(f, "outer.inner")`
    and a module-level `_make_id(f, "outer_inner")` are one id, and
    `Worker.run` came out distinct only because of its capital letter.
    """
    marker = hashlib.blake2s(
        f"py-scope\x00{file_id}\x00{qualname}".encode("utf-8"),
        digest_size=_ID_DISCRIMINATOR_LEN // 2,
    ).hexdigest()
    readable = unicodedata.normalize("NFKC", f"{file_id}_{qualname}")
    readable = re.sub(r"[^\w]+", "_", readable, flags=re.UNICODE)
    readable = re.sub(r"_+", "_", readable).strip("_").casefold()
    return f"{readable[: _MAX_ID_LEN - len(marker) - 1].rstrip('_')}_{marker}"
```

- [ ] **Step 4: Run the tests.**

```bash
$PY -m pytest tests/test_python_scope_identity.py -q > "$SCRATCH/t2b.txt" 2>&1; echo "EXIT=$?"; tail -3 "$SCRATCH/t2b.txt"
```

Expected: `EXIT=0`, 7 passed.

- [ ] **Step 5: Commit, with the maintainer's go.**

```bash
git add src/graphite/extract/ast.py tests/test_python_scope_identity.py
aramid check --staged
git commit -m "feat(extract): _scoped_id -- always-discriminated ids for non-module Python definitions"
```

---
### Task 3: `pyscope` — Python's scope tree and bare-name resolution

**Files:**
- Create: `src/graphite/extract/pyscope.py`
- Create: `tests/test_pyscope.py`

**Interfaces:**
- Consumes: nothing from graphite. It is pure over a tree-sitter tree, and the caller injects import resolution and naming.
- Produces, as consumed by Task 6:
  - `collect_scopes(root, import_bindings: Callable[[Any], list[tuple[str, str, str | None]]], name_of: Callable[[Any], str | None]) -> ScopeTable`
  - `ScopeTable.definitions: dict[int, tuple[str, bool]]`, mapping a def/class node's `.id` to `(qualname, module_level)`
  - `ScopeTable.scope_of_call(call_node) -> PyScope`
  - `ScopeTable.module_defines(name: str) -> bool`
  - `resolve(scope: PyScope, name: str) -> Resolution`
  - `Resolution(kind, qualname, module_level, target)`, where `kind` is one of `"def" | "symbol" | "alias" | "external" | "value" | "none"`

This code was validated on 2026-10-05: 55 tests passed, and four spot-checked mutants were each killed by their intended tests.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_pyscope.py`:

```python
"""Python lexical scopes (#70, #71): what a bare name denotes at a call site.

Each test parses real Python with graphite's own tree-sitter loader, builds the
scope table, and resolves every bare call to one name. Imports are bound by a
fake binder so these tests pin the SCOPE rules alone; import resolution has its
own tests (`test_python_import_bindings.py`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.extract import pyscope as S
from graphite.extract.ast import _LOADER

AST_PY = Path(__file__).parents[1] / "src" / "graphite" / "extract" / "ast.py"


def _fake_imports(statement):
    """`from ext... import n` -> external; other `from` -> symbol; `import ext...` -> external; else alias."""
    out = []
    text = statement.text.decode()
    if statement.type == "import_from_statement":
        module = text.split()[1]
        for part in text.split(" import ", 1)[1].replace("(", "").replace(")", "").split(","):
            bits = part.split(" as ")
            original, local = bits[0].strip(), bits[-1].strip()
            out.append((local, "external", None) if module.startswith("ext") else (local, "symbol", f"{module}:{original}"))
    else:
        for part in text[len("import "):].split(","):
            bits = part.split(" as ")
            original, local = bits[0].strip(), bits[-1].strip().split(".")[0]
            out.append((local, "external", None) if original.startswith("ext") else (local, "alias", f"file:{original}"))
    return out


def _name_of(node):
    return node.child_by_field_name("name").text.decode()


def _table(source: str):
    tree = _LOADER.parser("python").parse(source.encode())
    return tree, S.collect_scopes(tree.root_node, _fake_imports, _name_of)


def _calls(source: str, name: str) -> list:
    tree, table = _table(source)
    found = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "call":
            func = node.child_by_field_name("function")
            if func is not None and func.type == "identifier" and func.text.decode() == name:
                found.append((node.start_byte, S.resolve(table.scope_of_call(node), name)))
        stack.extend(node.children)
    return [res for _start, res in sorted(found, key=lambda pair: pair[0])]


def _one(source: str, name: str) -> S.Resolution:
    (res,) = _calls(source, name)
    return res


def _def(qualname: str, module_level: bool = False) -> S.Resolution:
    return S.Resolution("def", qualname=qualname, module_level=module_level)


VALUE = S.Resolution("value")
NONE = S.Resolution("none")

#: One body per binder kind §4.2 lists. Each binds `fn` locally before calling it.
BINDER_CASES = [
    ("param", "def use(fn):\n    return fn()\n"),
    ("posonly", "def use(a, /, fn):\n    return fn()\n"),
    ("kwonly", "def use(*, fn):\n    return fn()\n"),
    ("default", "def use(fn=None):\n    return fn()\n"),
    ("typed-default", "def use(fn: int = 1):\n    return fn()\n"),
    ("typed", "def use(fn: int):\n    return fn()\n"),
    ("star", "def use(*fn):\n    return fn()\n"),
    ("starstar", "def use(**fn):\n    return fn()\n"),
    ("lambda", "use = lambda fn: fn()\n"),
    ("assign", "def use():\n    fn = make()\n    return fn()\n"),
    ("annotated", "def use():\n    fn: int = make()\n    return fn()\n"),
    ("annotation-only", "def use():\n    fn: int\n    return fn()\n"),
    ("augmented", "def use():\n    fn += 1\n    return fn()\n"),
    ("walrus", "def use():\n    if (fn := make()):\n        return fn()\n"),
    ("unpack", "def use():\n    a, (b, *fn) = t\n    return fn()\n"),
    ("for", "def use(xs):\n    for fn in xs:\n        fn()\n"),
    ("async-for", "async def use(xs):\n    async for fn in xs:\n        fn()\n"),
    ("with", "def use():\n    with ctx() as fn:\n        fn()\n"),
    ("async-with", "async def use():\n    async with ctx() as (a, fn):\n        fn()\n"),
    ("except", "def use():\n    try:\n        pass\n    except E as fn:\n        fn()\n"),
    ("del", "def use():\n    del fn\n    return fn()\n"),
    ("match-star", "def use(t):\n    match t:\n        case [*fn]:\n            fn()\n"),
    ("match-mapping-rest", "def use(t):\n    match t:\n        case {'k': 1, **fn}:\n            fn()\n"),
    ("match-keyword", "def use(t):\n    match t:\n        case P(x=fn):\n            fn()\n"),
    ("match-as", "def use(t):\n    match t:\n        case P() as fn:\n            fn()\n"),
    ("match-capture", "def use(t):\n    match t:\n        case fn:\n            fn()\n"),
]


def test_a_module_level_call_binds_the_module_def():
    assert _one("def f(): pass\nf()\n", "f") == _def("f", module_level=True)


def test_a_nested_def_is_found_before_the_module_one():
    src = "def f(): pass\ndef outer():\n    def f(): pass\n    return f()\n"
    assert _one(src, "f") == _def("outer.f")


def test_a_method_body_does_not_see_its_class_scope():
    src = "def run(): pass\nclass K:\n    def run(self): pass\n    def go(self):\n        return run()\n"
    assert _one(src, "run") == _def("run", module_level=True)


def test_a_method_body_with_no_module_def_resolves_to_nothing():
    src = "class K:\n    def run(self): pass\n    def go(self):\n        return run()\n"
    assert _one(src, "run") == NONE


def test_a_class_body_statement_sees_its_class_scope():
    assert _one("class K:\n    def m(): return 1\n    x = m()\n", "m") == _def("K.m")


@pytest.mark.parametrize("body", [body for _id, body in BINDER_CASES], ids=[i for i, _b in BINDER_CASES])
def test_every_local_binder_makes_a_bare_call_a_local_value(body):
    assert _one("def fn(): pass\n" + body, "fn") == VALUE


def test_a_comprehension_target_binds_only_inside_the_comprehension():
    src = "def g(): pass\ndef f(xs):\n    [g() for g in xs]\n    return g()\n"
    assert _calls(src, "g") == [VALUE, _def("g", module_level=True)]


def test_a_walrus_in_a_comprehension_binds_in_the_function():
    src = "def y(): pass\ndef f(xs):\n    [v for v in xs if (y := v)]\n    return y()\n"
    assert _one(src, "y") == VALUE


def test_a_comprehension_in_a_class_body_does_not_see_the_class_scope():
    assert _one("class K:\n    def m(): pass\n    xs = [m() for _ in range(3)]\n", "m") == NONE


def test_the_first_iterable_of_a_class_body_comprehension_sees_the_class_scope():
    """CPython evaluates the first `for` iterable in the enclosing scope."""
    assert _one("class K:\n    def m(): return [1]\n    xs = [y for y in m()]\n", "m") == _def("K.m")


def test_global_redirects_to_the_module_binding():
    src = (
        "def helper(): pass\n"
        "def outer():\n"
        "    def helper(): pass\n"
        "    def f():\n"
        "        global helper\n"
        "        return helper()\n"
    )
    assert _one(src, "helper") == _def("helper", module_level=True)


def test_an_assignment_under_global_makes_the_module_binding_ambiguous():
    src = "def handler(): pass\ndef setup():\n    global handler\n    handler = make()\nhandler()\n"
    assert _one(src, "handler") == VALUE


def test_nonlocal_rebinding_makes_the_enclosing_binding_ambiguous():
    src = (
        "def outer():\n"
        "    def helper(): pass\n"
        "    def inner():\n"
        "        nonlocal helper\n"
        "        helper = other\n"
        "    inner()\n"
        "    return helper()\n"
    )
    assert _one(src, "helper") == VALUE


def test_a_function_local_import_binds_only_in_that_function():
    src = "def tool(): pass\ndef a():\n    from ext_pkg import tool\n    tool()\ntool()\n"
    assert _calls(src, "tool") == [S.Resolution("external"), _def("tool", module_level=True)]


def test_a_function_local_module_import_binds_a_module_alias_there():
    src = "def fn(): pass\ndef use():\n    import mod as fn\n    return fn()\n"
    assert _one(src, "fn") == S.Resolution("alias", target="file:mod")


def test_an_in_repo_symbol_import_resolves_to_its_target():
    assert _one("from repo import f\nf()\n", "f") == S.Resolution("symbol", target="repo:f")


def test_two_unresolved_imports_of_one_name_stay_external():
    src = "try:\n    from extjson import loads\nexcept ImportError:\n    from extsimple import loads\nloads()\n"
    assert _one(src, "loads") == S.Resolution("external")


def test_a_def_and_an_import_of_one_name_are_ambiguous():
    assert _one("from repo import f\ndef f(): pass\nf()\n", "f") == VALUE


def test_a_same_scope_redefinition_is_one_binding():
    src = "if X:\n    def f(): pass\nelse:\n    def f(): pass\nf()\n"
    assert _one(src, "f") == _def("f", module_level=True)


def test_a_default_value_is_evaluated_in_the_enclosing_scope():
    src = "def make(): pass\ndef f(make, x=make()):\n    return make()\n"
    assert _calls(src, "make") == [_def("make", module_level=True), VALUE]


def test_a_methods_default_is_evaluated_in_the_class_body():
    src = "def factory(): pass\nclass K:\n    def factory(): return 1\n    def m(self, x=factory()):\n        return x\n"
    assert _one(src, "factory") == _def("K.factory")


def test_a_decorator_is_evaluated_in_the_enclosing_scope():
    assert _one("def deco(): pass\ndef outer(deco):\n    @deco()\n    def inner(): pass\n", "deco") == VALUE
    src = "def deco(): pass\nclass K:\n    def deco(self): pass\n    @deco()\n    def m(self): pass\n"
    assert _one(src, "deco") == _def("K.deco")


def test_a_module_level_annotation_alone_does_not_bind():
    """`handler: Callable` at module scope declares, it does not bind; the def does."""
    assert _one("handler: object\ndef handler(): pass\nhandler()\n", "handler") == _def("handler", module_level=True)


def test_a_def_bound_at_module_scope_through_global_keeps_its_scoped_identity():
    src = "def setup():\n    global h\n    def h(): pass\nh()\n"
    assert _one(src, "h") == _def("setup.h")


def test_a_name_bound_nowhere_resolves_to_nothing():
    assert _one("f()\n", "f") == NONE


def test_qualnames_follow_nesting():
    src = "class Outer:\n    def method(self):\n        def helper(): pass\n        return helper()\n"
    assert _one(src, "helper") == _def("Outer.method.helper")


def test_a_recursive_nested_helper_binds_to_itself_not_its_namesake():
    src = (
        "def a(tree):\n    def visit(n):\n        return visit(n)\n    return visit(tree)\n"
        "def b(tree):\n    def visit(n):\n        return visit(n)\n    return visit(tree)\n"
    )
    assert _calls(src, "visit") == [_def("a.visit")] * 2 + [_def("b.visit")] * 2


def test_definitions_record_qualname_and_module_level():
    _tree, table = _table("def f(): pass\nclass K:\n    @property\n    def size(self): pass\n")
    assert sorted(table.definitions.values()) == [("K", True), ("K.size", False), ("f", True)]


def test_module_defines_counts_module_level_definitions_only():
    _tree, table = _table("def f(): pass\nx = 1\ndef g():\n    def h(): pass\n")
    assert table.module_defines("f")
    assert not table.module_defines("x")
    assert not table.module_defines("h")


def test_call_and_definition_node_ids_are_unique_in_a_large_file():
    """`ScopeTable` keys on tree-sitter `Node.id`; prove it is unique per node."""
    tree = _LOADER.parser("python").parse(AST_PY.read_bytes())
    keyed = {"call": [], "function_definition": [], "class_definition": []}
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in keyed:
            keyed[node.type].append(node.id)
        stack.extend(node.children)
    for kind, ids in keyed.items():
        assert ids, kind
        assert len(ids) == len(set(ids)), kind
```

- [ ] **Step 2: Run the tests and watch them fail.**

```bash
$PY -m pytest tests/test_pyscope.py -q > "$SCRATCH/t3.txt" 2>&1; echo "EXIT=$?"; tail -5 "$SCRATCH/t3.txt"
```

Expected: `EXIT=2`, with `ModuleNotFoundError: No module named 'graphite.extract.pyscope'`.

- [ ] **Step 3: Implement.** Create `src/graphite/extract/pyscope.py`:

```python
"""Python lexical scopes for bare-name call binding (#70, #71).

One pass over a tree-sitter Python tree builds every scope Python creates --
module, function, lambda, class, comprehension -- and records each name each
scope binds. `resolve` then answers what a bare name denotes at a call site the
way CPython does: the innermost scope that binds it, enclosing CLASS scopes
skipped from inside a function or comprehension, `global` and `nonlocal`
honoured.

Node ids are not computed here. A definition is reported by `qualname` and by
whether it sits at module scope; `extract/ast.py` turns that into an id. The
collector records the scope each call node is EVALUATED in, so the extractor
and the collector cannot disagree about where a call sits.

Spec: docs/superpowers/specs/2026-10-04-python-scope-identity-design.md, §4.2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

#: (local name, kind, target) for each name one import statement binds. kind is
#: "symbol", "alias", "external" or "unknown"; see `ast._python_import_bindings`.
ImportBindings = Callable[[Any], list[tuple[str, str, str | None]]]
#: A def/class node's name segment exactly as its graph node records it, or None.
NameOf = Callable[[Any], str | None]

_COMPREHENSIONS = frozenset({
    "list_comprehension", "set_comprehension", "dictionary_comprehension", "generator_expression",
})
#: Node types whose identifiers are all binding targets (`a, (b, *c) = ...`).
_TARGET_CONTAINERS = frozenset({
    "pattern_list", "tuple_pattern", "list_pattern", "tuple", "list",
    "expression_list", "parenthesized_expression", "list_splat_pattern", "as_pattern_target",
})
_SPLATS = frozenset({"list_splat_pattern", "dictionary_splat_pattern", "splat_pattern"})


@dataclass(frozen=True)
class Binder:
    """One thing that binds a name in a scope."""

    kind: str  # "def" | "class" | "import" | "value"
    qualname: str | None = None
    module_level: bool = False
    import_kind: str | None = None
    target: str | None = None


_VALUE = Binder("value")


@dataclass(eq=False)
class PyScope:
    kind: str  # "module" | "function" | "lambda" | "class" | "comprehension"
    parent: PyScope | None
    qualname: str
    binders: dict[str, list[Binder]] = field(default_factory=dict)
    globals: set[str] = field(default_factory=set)
    nonlocals: set[str] = field(default_factory=set)

    def bind(self, name: str, binder: Binder) -> None:
        self.binders.setdefault(name, []).append(binder)

    @property
    def prefix(self) -> str:
        return f"{self.qualname}." if self.qualname else ""


@dataclass(frozen=True)
class Resolution:
    """What a bare name denotes at one call site."""

    kind: str  # "def" | "symbol" | "alias" | "external" | "value" | "none"
    qualname: str | None = None
    module_level: bool = False
    target: str | None = None


@dataclass
class ScopeTable:
    module: PyScope
    call_scope: dict[int, PyScope]
    definitions: dict[int, tuple[str, bool]]

    def scope_of_call(self, call_node: Any) -> PyScope:
        return self.call_scope.get(call_node.id, self.module)

    def module_defines(self, name: str) -> bool:
        """A def or class at module scope binds `name`, so `_make_id(file, name)` is its id."""
        return any(
            b.kind in ("def", "class") and b.module_level for b in self.module.binders.get(name, ())
        )


def _text(node: Any) -> str:
    return node.text.decode("utf-8", errors="ignore") if node is not None and node.text else ""


def _target_names(node: Any) -> Iterator[str]:
    """Every identifier a binding target binds: `a`, `a, (b, *c)`, `[a, b]`."""
    if node is None:
        return
    if node.type == "identifier":
        yield _text(node)
    elif node.type in _TARGET_CONTAINERS:
        for child in node.named_children:
            yield from _target_names(child)


def _parameter_names(params: Any) -> Iterator[str]:
    for child in params.named_children:
        if child.type == "identifier":
            yield _text(child)
        elif child.type in ("default_parameter", "typed_default_parameter"):
            yield from _target_names(child.child_by_field_name("name"))
        elif child.type == "typed_parameter":
            for part in child.named_children:
                if part.type == "identifier":
                    yield _text(part)
                    break
                if part.type in _SPLATS:
                    yield from (_text(c) for c in part.named_children if c.type == "identifier")
                    break
        elif child.type in _SPLATS:
            yield from (_text(c) for c in child.named_children if c.type == "identifier")


def _parameter_expressions(params: Any) -> Iterator[Any]:
    """Defaults and annotations: evaluated where the def or lambda itself is."""
    for child in params.named_children:
        for name in ("value", "type"):
            part = child.child_by_field_name(name)
            if part is not None:
                yield part


def _pattern_captures(node: Any) -> Iterator[str]:
    """Names a `case` pattern captures. `_` captures nothing; a dotted name is a value."""
    kind = node.type
    if kind == "dotted_name":
        parts = node.named_children
        if len(parts) == 1 and parts[0].type == "identifier" and _text(parts[0]) != "_":
            yield _text(parts[0])
        return
    if kind in _SPLATS:
        for child in node.named_children:
            if child.type == "identifier" and _text(child) != "_":
                yield _text(child)
        return
    if kind == "as_pattern":
        # In a case pattern the alias is a bare identifier child with no field name.
        for child in node.named_children:
            if child.type == "identifier":
                if _text(child) != "_":
                    yield _text(child)
            elif child.type == "as_pattern_target":
                yield from _target_names(child)
            else:
                yield from _pattern_captures(child)
        return
    if kind in ("keyword_pattern", "class_pattern"):
        # `P(x=fn)`: the keyword and the class are not captures.
        for child in node.named_children[1:]:
            yield from _pattern_captures(child)
        return
    if kind == "dict_pattern":
        for index, child in enumerate(node.children):
            if node.field_name_for_child(index) != "key" and child.is_named:
                yield from _pattern_captures(child)
        return
    for child in node.named_children:
        yield from _pattern_captures(child)


def collect_scopes(root: Any, import_bindings: ImportBindings, name_of: NameOf) -> ScopeTable:
    """Every scope in one Python file, what each binds, and where each call is evaluated."""
    module = PyScope("module", None, "")
    call_scope: dict[int, PyScope] = {}
    definitions: dict[int, tuple[str, bool]] = {}
    scopes: list[PyScope] = [module]

    def open_scope(kind: str, parent: PyScope, qualname: str) -> PyScope:
        inner = PyScope(kind, parent, qualname)
        scopes.append(inner)
        return inner

    def bind_parameters(params: Any, inner: PyScope, outer: PyScope) -> None:
        for name in _parameter_names(params):
            inner.bind(name, _VALUE)
        for expression in _parameter_expressions(params):
            visit(expression, outer)

    def visit_definition(node: Any, scope: PyScope) -> None:
        raw = _text(node.child_by_field_name("name"))
        segment = name_of(node)
        if not raw or segment is None:
            for child in node.children:
                visit(child, scope)
            return
        is_function = node.type == "function_definition"
        qualname = f"{scope.prefix}{segment}"
        module_level = scope.kind == "module"
        definitions[node.id] = (qualname, module_level)
        scope.bind(raw, Binder("def" if is_function else "class", qualname=qualname, module_level=module_level))
        inner = open_scope("function" if is_function else "class", scope, qualname)
        for index, child in enumerate(node.children):
            field_name = node.field_name_for_child(index)
            if field_name == "body":
                visit(child, inner)
            elif field_name == "parameters":
                bind_parameters(child, inner, scope)
            elif field_name != "name":
                # bases, the return annotation, type parameters: evaluated in the
                # enclosing scope (decorators sit outside this node already)
                visit(child, scope)

    def visit_lambda(node: Any, scope: PyScope) -> None:
        inner = open_scope("lambda", scope, scope.qualname)
        for index, child in enumerate(node.children):
            field_name = node.field_name_for_child(index)
            if field_name == "parameters":
                bind_parameters(child, inner, scope)
            elif field_name == "body":
                visit(child, inner)

    def visit_comprehension(node: Any, scope: PyScope) -> None:
        inner = open_scope("comprehension", scope, scope.qualname)
        first = True
        for child in node.children:
            if child.type != "for_in_clause":
                visit(child, inner)
                continue
            for name in _target_names(child.child_by_field_name("left")):
                inner.bind(name, _VALUE)
            for index, part in enumerate(child.children):
                field_name = child.field_name_for_child(index)
                if field_name == "left":
                    continue
                # The FIRST iterable is evaluated in the enclosing scope, so a
                # class-body comprehension can iterate a class-level name.
                visit(part, scope if first and field_name == "right" else inner)
            first = False

    def visit(node: Any, scope: PyScope) -> None:
        kind = node.type
        if kind in ("function_definition", "class_definition"):
            visit_definition(node, scope)
            return
        if kind == "lambda":
            visit_lambda(node, scope)
            return
        if kind in _COMPREHENSIONS:
            visit_comprehension(node, scope)
            return
        if kind == "global_statement":
            scope.globals.update(_text(c) for c in node.named_children if c.type == "identifier")
            return
        if kind == "nonlocal_statement":
            scope.nonlocals.update(_text(c) for c in node.named_children if c.type == "identifier")
            return
        if kind in ("import_statement", "import_from_statement"):
            for local, import_kind, target in import_bindings(node):
                scope.bind(local, Binder("import", import_kind=import_kind, target=target))
            return
        if kind == "call":
            call_scope[node.id] = scope
        elif kind == "assignment":
            # An annotation with no value (`x: int`) binds only in a function.
            if node.child_by_field_name("right") is not None or scope.kind in ("function", "lambda"):
                for name in _target_names(node.child_by_field_name("left")):
                    scope.bind(name, _VALUE)
        elif kind == "augmented_assignment":
            for name in _target_names(node.child_by_field_name("left")):
                scope.bind(name, _VALUE)
        elif kind == "named_expression":
            # PEP 572: a walrus binds in the nearest scope that is not a comprehension.
            home = scope
            while home.kind == "comprehension" and home.parent is not None:
                home = home.parent
            for name in _target_names(node.child_by_field_name("name")):
                home.bind(name, _VALUE)
        elif kind == "for_statement":
            for name in _target_names(node.child_by_field_name("left")):
                scope.bind(name, _VALUE)
        elif kind == "as_pattern" and node.parent is not None and node.parent.type != "case_pattern":
            # `with ... as x` and `except E as x`. A case pattern's `as` is a capture.
            for name in _target_names(node.child_by_field_name("alias")):
                scope.bind(name, _VALUE)
        elif kind == "delete_statement":
            for child in node.named_children:
                for name in _target_names(child):
                    scope.bind(name, _VALUE)
        elif kind == "case_clause":
            for child in node.named_children:
                if child.type == "case_pattern":
                    for name in _pattern_captures(child):
                        scope.bind(name, _VALUE)
        for child in node.children:
            visit(child, scope)

    visit(root, module)
    _relocate_declarations(scopes, module)
    return ScopeTable(module, call_scope, definitions)


def _relocate_declarations(scopes: list[PyScope], module: PyScope) -> None:
    """Move the binders of `global` and `nonlocal` names to the scope they bind in.

    Innermost first, so a chain of `nonlocal` declarations ends at its owner.
    `nonlocal` needs nothing else: once its binders sit in the owning function,
    the ordinary lookup walk reaches that function first.
    """
    for scope in reversed(scopes):
        for name in scope.globals:
            moved = scope.binders.pop(name, [])
            if moved:
                module.binders.setdefault(name, []).extend(moved)
        for name in scope.nonlocals:
            moved = scope.binders.pop(name, [])
            home = _nonlocal_home(scope, name)
            if moved and home is not None:
                home.binders.setdefault(name, []).extend(moved)


def _nonlocal_home(scope: PyScope, name: str) -> PyScope | None:
    current = scope.parent
    while current is not None and current.kind != "module":
        if current.kind in ("function", "lambda") and (name in current.binders or name in current.nonlocals):
            return current
        current = current.parent
    return None


def _module_of(scope: PyScope) -> PyScope:
    while scope.parent is not None:
        scope = scope.parent
    return scope


def resolving_scope(scope: PyScope, name: str) -> PyScope | None:
    """The scope whose binding of `name` a lookup from `scope` reaches, or None."""
    if name in scope.globals:
        module = _module_of(scope)
        return module if name in module.binders else None
    current: PyScope | None = scope
    starting = True
    while current is not None:
        # A class scope is visible only to statements directly in its body.
        if (starting or current.kind != "class") and name in current.binders:
            return current
        starting = False
        current = current.parent
    return None


def resolve(scope: PyScope, name: str) -> Resolution:
    """What a bare `name` denotes when called from `scope`."""
    home = resolving_scope(scope, name)
    return Resolution("none") if home is None else _outcome(home.binders[name])


def _outcome(binders: list[Binder]) -> Resolution:
    """§4.2's outcome table, for the binders of one name in its resolving scope."""
    distinct: dict[tuple[str | None, ...], Binder] = {}
    for binder in binders:
        if binder.kind in ("def", "class"):
            key: tuple[str | None, ...] = ("def", binder.qualname)
        elif binder.kind == "import":
            key = ("import", binder.import_kind, binder.target)
        else:
            key = ("value",)
        distinct.setdefault(key, binder)
    if len(distinct) == 1:
        (binder,) = distinct.values()
        if binder.kind in ("def", "class"):
            return Resolution("def", qualname=binder.qualname, module_level=binder.module_level)
        if binder.kind == "import" and binder.import_kind is not None and binder.import_kind != "unknown":
            return Resolution(binder.import_kind, target=binder.target)
        return Resolution("value")
    # Two unresolved imports of one name (`try: import ujson as json` /
    # `except ImportError: import json`) leave the repo whichever one runs.
    if all(b.kind == "import" and b.import_kind == "external" for b in binders):
        return Resolution("external")
    return Resolution("value")
```

- [ ] **Step 4: Run the tests, then type-check and lint.**

```bash
$PY -m pytest tests/test_pyscope.py -q > "$SCRATCH/t3b.txt" 2>&1; echo "EXIT=$?"; tail -3 "$SCRATCH/t3b.txt"
python -m mypy src/graphite/extract/pyscope.py > "$SCRATCH/t3m.txt" 2>&1; echo "MYPY=$?"; tail -3 "$SCRATCH/t3m.txt"
python -m ruff check src/graphite/extract/pyscope.py tests/test_pyscope.py
```

Expected: `EXIT=0` with 55 passed; `MYPY=0`; `All checks passed!`.

- [ ] **Step 5: Commit, with the maintainer's go.**

```bash
git add src/graphite/extract/pyscope.py tests/test_pyscope.py
aramid check --staged
git commit -m "feat(extract): pyscope -- Python lexical scopes for bare-name call binding (#70, #71)"
```

---
### Task 4: the oracle, and its CONTROL run on today's engine

The oracle is built and run before anything changes extraction. At this point the dev venv still runs today's extractor. `git diff v1.1.1 HEAD -- src/graphite/extract/ src/graphite/resolve.py src/graphite/graph.py src/graphite/health.py src/graphite/ingest.py src/graphite/config.py` was empty at `f9e7014` (checked 2026-10-05). The last two paths are there because the oracle's `load` uses `collect_files` and `Config`. Apart from Tasks 1 to 3, the diff is still empty, and none of those three tasks changes extraction output:
- `_merge` runs after extraction;
- `_scoped_id` is not yet called;
- `extract/pyscope.py` exists but nothing imports it yet.

So the oracle must find the planted #70/#71 defects and the known real sites NOW. An oracle written only after the fix could only ever read zero, which looks the same whether the engine is right or the oracle is blind.

**Files:**
- Create: `scripts/pyscopeoracle.py`
- Create: `tests/test_pyscopeoracle.py`

**Interfaces:**
- Consumes: from graphite 1.1.1 names only (see Global Constraints).
- Produces:
  - the CLI modes `sites`, `dump` and `compare`;
  - module functions used by tests: `load(root, only=None) -> Corpus`, `extract(corpus)`, `TruthModel(corpus).sites(rel)`, `classify(truth, bound)`, `measure_sites(corpus)`, `dump(corpus)`, `compare(old, new)`.

This code was validated on 2026-10-05 against today's engine: 20 passed. On the fixture below it reported exactly the three planted wrong bindings, with zero symtable disagreements.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_pyscopeoracle.py`:

```python
"""Known-answer tests for `scripts/pyscopeoracle.py` (#70, #71).

The oracle's figures gate the phase 1 release (spec §5: zero wrong-target
bare-name calls on graphite and Django) and go into its release notes, so the
instrument and its known answers live here together. Its truth side is checked
against hand-derived CPython answers; its pairing side against today's engine,
which must show the planted defects (the CONTROL: an oracle that reads 0 on an
engine known to be wrong sees nothing).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ORACLE_PATH = Path(__file__).parents[1] / "scripts" / "pyscopeoracle.py"


def _load_oracle():
    """Import the oracle from its path; `scripts/` is deliberately not a package."""
    spec = importlib.util.spec_from_file_location("_pyscopeoracle", ORACLE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def oracle():
    return _load_oracle()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


#: The #70 / #71 shapes in one file. Line numbers are load-bearing.
SCOPE_FIXTURE = (
    "def test_one():\n"  # 1
    "    def fake_build():\n"  # 2
    "        return 1\n"  # 3
    "    return fake_build()\n"  # 4
    "\n"  # 5
    "\n"  # 6
    "def test_two():\n"  # 7
    "    def fake_build():\n"  # 8
    "        return 2\n"  # 9
    "    return fake_build()\n"  # 10
    "\n"  # 11
    "\n"  # 12
    "def generate():\n"  # 13
    "    return 0\n"  # 14
    "\n"  # 15
    "\n"  # 16
    "def route_c(generate):\n"  # 17
    "    return generate()\n"  # 18
    "\n"  # 19
    "\n"  # 20
    "class K:\n"  # 21
    "    def run(self):\n"  # 22
    "        return 1\n"  # 23
    "\n"  # 24
    "    def go(self):\n"  # 25
    "        return run()\n"  # 26
)


def _truths(oracle, tmp_path: Path, files: dict[str, str]) -> dict:
    for rel, text in files.items():
        _write(tmp_path / rel, text)
    model = oracle.TruthModel(oracle.load(tmp_path))
    return {rel: dict(model.sites(rel)) for rel in files}


def test_cpython_truth_for_the_scope_fixture(oracle, tmp_path):
    sites = _truths(oracle, tmp_path, {"m.py": SCOPE_FIXTURE})["m.py"]
    assert sites[(4, "fake_build")] == [("def", "m.py", 2)]
    assert sites[(10, "fake_build")] == [("def", "m.py", 8)]
    assert sites[(18, "generate")] == [("value",)]
    assert sites[(26, "run")] == [("none",)]


def test_cpython_truth_for_comprehensions_and_class_bodies(oracle, tmp_path):
    source = (
        "def g(): pass\n"  # 1
        "def f(xs):\n"  # 2
        "    [g() for g in xs]\n"  # 3
        "    return g()\n"  # 4
        "class K:\n"  # 5
        "    def m(): return [1]\n"  # 6
        "    a = m()\n"  # 7
        "    b = [m() for _ in range(3)]\n"  # 8
        "    c = [y for y in m()]\n"  # 9
    )
    sites = _truths(oracle, tmp_path, {"m.py": source})["m.py"]
    assert sites[(3, "g")] == [("value",)]
    assert sites[(4, "g")] == [("def", "m.py", 1)]
    assert sites[(7, "m")] == [("def", "m.py", 6)]
    assert sites[(8, "m")] == [("none",)]
    assert sites[(9, "m")] == [("def", "m.py", 6)]


def test_a_rebound_global_is_excluded_not_scored(oracle, tmp_path):
    source = "def handler(): pass\ndef setup():\n    global handler\n    handler = make()\nhandler()\n"
    sites = _truths(oracle, tmp_path, {"m.py": source})["m.py"]
    assert sites[(5, "handler")] == [("rebound",)]


def test_truth_follows_one_import_hop_and_marks_a_reexport(oracle, tmp_path):
    files = {
        "pkg/__init__.py": "from pkg.impl import f\n",
        "pkg/impl.py": "def f():\n    return 1\n",
        "direct.py": "from pkg.impl import f\nf()\n",
        "via_init.py": "from pkg import f\nf()\n",
        "ext.py": "from json import loads\nloads()\n",
    }
    truths = _truths(oracle, tmp_path, files)
    assert truths["direct.py"][(2, "f")] == [("def", "pkg/impl.py", 1)]
    assert truths["via_init.py"][(2, "f")] == [("def", "pkg/impl.py", 1, "reexport")]
    assert truths["ext.py"][(2, "loads")] == [("external",)]


@pytest.mark.parametrize(
    ("truth", "bound", "category"),
    [
        (("def", "m.py", 2), "m.py:2", "correct"),
        (("def", "m.py", 8), "m.py:2", "WRONG-def->other-def"),
        (("def", "m.py", 8), None, "unbound-but-def"),
        (("def", "p.py", 1, "reexport"), None, "unbound-but-def(reexport)"),
        (("value",), None, "correct-unbound"),
        (("value",), "m.py:13", "WRONG-value-bound"),
        (("none",), "m.py:22", "WRONG-none-bound"),
        (("external",), "m.py:3", "WRONG-external-bound"),
        (("ambiguous",), "m.py:3", "truth-ambiguous"),
        (("rebound",), None, "truth-rebound"),
    ],
)
def test_classify(oracle, truth, bound, category):
    assert oracle.classify(truth, bound) == category


def test_todays_engine_mis_binds_the_three_planted_sites(oracle, tmp_path):
    """CONTROL. Today's engine binds #70's second call, and both #71 shapes, wrongly."""
    _write(tmp_path / "m.py", SCOPE_FIXTURE)
    report = oracle.measure_sites(oracle.load(tmp_path))
    assert report["stats"] == {
        "sites": 4,
        "correct": 1,
        "WRONG-def->other-def": 1,
        "WRONG-value-bound": 1,
        "WRONG-none-bound": 1,
    }
    assert report["wrong"] == 3
    assert report["selfcheck"]["call_nodes"] == report["selfcheck"]["distinct_call_ids"] == 4
    assert report["selfcheck"]["symtable_disagreements"] == 0


def test_a_report_names_the_oracle_that_wrote_it(oracle, tmp_path):
    """Two reports differ for an engine reason only if they came from the same oracle."""
    import hashlib
    import json

    _write(tmp_path / "corpus" / "m.py", "def f(): pass\nf()\n")
    out = tmp_path / "sites.json"
    assert oracle.main(["sites", str(tmp_path / "corpus"), "--out", str(out)]) == 0
    engine = json.loads(out.read_text(encoding="utf-8"))["engine"]
    assert engine["oracle_sha256"] == hashlib.sha256(ORACLE_PATH.read_bytes()).hexdigest()


def test_the_oracle_refuses_an_engine_it_cannot_pair(oracle, tmp_path, monkeypatch):
    import graphite.extract.ast as A

    monkeypatch.delattr(A, "_python_call_target")
    _write(tmp_path / "m.py", "def f(): pass\nf()\n")
    with pytest.raises(SystemExit, match="_python_call_target"):
        oracle.extract(oracle.load(tmp_path))


def test_extraction_restores_the_engine(oracle, tmp_path):
    import graphite.extract.ast as A

    before = (A._python_call_target, A._edge)
    _write(tmp_path / "m.py", "def f(): pass\nf()\n")
    oracle.extract(oracle.load(tmp_path))
    assert (A._python_call_target, A._edge) == before


def test_dump_records_what_dispatch_did(oracle, tmp_path):
    _write(
        tmp_path / "svc.py",
        "class Svc:\n    def helper(self):\n        return 1\n    def run(self):\n        return self.helper()\n",
    )
    report = oracle.dump(oracle.load(tmp_path))
    (record,) = [r for r in report["records"].values() if r["kind"] == "member"]
    assert record["dispatch"] == "repointed"
    assert record["targets"] == ["svc.py:2"]
    assert record["caller"] == "svc.py:4"


def test_compare_sorts_changes_into_pre_registered_categories(oracle):
    def rec(**changes):
        base = {
            "kind": "bare", "name": "f", "caller": "m.py:1", "targets": ["m.py:5"],
            "confidence": "LOCAL_CALL", "dispatch": "n/a", "member_candidates": None,
        }
        return {**base, **changes}

    member = {"kind": "member", "name": None}
    old = {"records": {
        "m.py:2:0": rec(),
        "m.py:3:0": rec(),
        "m.py:4:0": rec(),
        "m.py:6:0": rec(**member, targets=["unbound"], confidence="EXTERNAL_CALL", dispatch="kept"),
    }}
    new = {"records": {
        "m.py:2:0": rec(),
        "m.py:3:0": rec(targets=["unbound"]),
        "m.py:4:0": rec(caller="m.py:9"),
        "m.py:6:0": rec(**member, targets=["k.py:3"], confidence="LOCAL_CALL", dispatch="repointed"),
        "m.py:7:0": rec(),
    }}
    assert oracle.compare(old, new)["stats"] == {
        "only-old": 0,
        "only-new": 1,
        "same": 1,
        "bare-target-changed": 1,
        "caller-reattributed": 1,
        "dispatch-changed": 1,
        "confidence-EXTERNAL_CALL->LOCAL_CALL": 1,
        "confidence-flip-then-dispatched": 1,
    }
```

- [ ] **Step 2: Run the tests and watch them fail.**

```bash
$PY -m pytest tests/test_pyscopeoracle.py -q > "$SCRATCH/t4.txt" 2>&1; echo "EXIT=$?"; tail -5 "$SCRATCH/t4.txt"
```

Expected: `EXIT=1`, every test erroring in the `oracle` fixture with `FileNotFoundError` naming `scripts/pyscopeoracle.py`.

- [ ] **Step 3: Implement.** Create `scripts/pyscopeoracle.py`:

```python
"""An independent oracle for Python bare-name call binding (#70, #71).

Written to GRADE graphite's extractor, not to replace it. Truth comes from
CPython: `symtable` says how each function classifies a name (declared global,
nonlocal, local), and the `ast` of the resolving scope says which binder it
is. graphite's side is the REAL extractor: `_extract_python` runs on every
file with `_python_call_target` and `_edge` wrapped, so each call site is
paired with the exact edge graphite emitted for it.

It reads the EXTRACTION stage, not the built graph. The built graph keeps one
edge per (source, target, relation), so it cannot say which call site an edge
came from, and per-site truth needs exactly that. `dump` then runs graphite's
own dispatch post-pass (`_resolve_method_dispatch`) over the same edges, so
member calls are compared as they reach the graph.

Modes
-----
  sites ROOT [--only PREFIX] [--out FILE]   score every bare-name call site
  dump ROOT [--only PREFIX] --out FILE      every call edge as a denotation record
  compare OLD NEW [--out FILE]              diff two dumps in pre-registered categories

It imports only names that exist in graphite 1.1.1 as well as later, so one
script grades both engines: run it under the interpreter whose graphite you
want to grade. Every report records which graphite ran (`engine`) and
the sha256 of this script, so a changed oracle is never read as a changed engine.

What it CANNOT see -- published so it does not grade its own blind spot
----------------------------------------------------------------------
* A name some function declares `global` (or `nonlocal`) and assigns:
  reported as `truth-rebound`, not scored.
* Two or more def/class/import binders of one name in the resolving scope,
  an if/else redefinition included: `truth-ambiguous`, not scored.
* A symbol import that reaches its definition only through a re-export
  (`pkg/__init__.py` importing it): `unbound-but-def(reexport)`, the known
  residual (spec §7), not counted as wrong.
* Star imports bind nothing it can name; PEP 695 type-parameter scopes are not
  modelled; two calls to one name on one line are paired in source order.
* Where CPython's `symtable` and this oracle's own binder walk disagree about
  whether a function binds a name, the site is still scored and the
  disagreement is counted in `selfcheck.symtable_disagreements`. A non-zero
  count is an oracle defect until each example is read and explained.
"""
from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import json
import symtable
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
#: Every graphite name this oracle reads. All exist in 1.1.1 and later.
ENGINE_NAMES = (
    "_python_call_target", "_edge", "_extract_python", "_file_node_id",
    "_resolve_method_dispatch", "_LOADER",
)
MAX_HOPS = 8

Truth = tuple[Any, ...]


# --- corpus -------------------------------------------------------------------


@dataclass
class Corpus:
    root: Path
    files: dict[str, bytes]  # rel path -> source, for the files extracted and scored
    index: Any  # graphite SourceIndex over EVERY Python file under root


def load(root: Path, only: str | None = None) -> Corpus:
    """Every Python file graphite would extract under `root`.

    The SourceIndex covers all of them, as a real build's does; `only` (a
    rel-path prefix such as `django/`) limits which are extracted and scored.
    """
    from graphite.config import Config
    from graphite.ingest import collect_files
    from graphite.resolve import SourceIndex

    with tempfile.TemporaryDirectory() as cache:
        cfg = Config(workers=1, cache_dir=Path(cache), typescript_resolver="disabled")
        entries = [e for e in collect_files(root, cfg) if e.language == "python"]
        index = SourceIndex.from_entries(entries, cfg)
    files = {
        e.rel_path: Path(e.abs_path).read_bytes()
        for e in entries
        if only is None or e.rel_path.startswith(only)
    }
    return Corpus(root=root, files=files, index=index)


# --- graphite's side ------------------------------------------------------------


@dataclass
class Extraction:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)  # every relation
    calls: list[dict[str, Any]] = field(default_factory=list)  # one record per call edge
    call_nodes: int = 0
    distinct_call_ids: int = 0


def _ts_calls(root: Any) -> list[Any]:
    found, stack = [], [root]
    while stack:
        node = stack.pop()
        if node.type == "call":
            found.append(node)
        stack.extend(node.children)
    return found


def extract(corpus: Corpus) -> Extraction:
    """Run graphite's REAL Python extractor, pairing each call edge with its site.

    `_python_call_target` runs once per call node and the call's edge is made
    before that call's children are walked, so the edge that follows a site
    event belongs to that site. Both are wrapped as module attributes and
    restored in `finally`; an engine missing either is refused, not guessed.
    """
    import graphite.extract.ast as A

    missing = [name for name in ENGINE_NAMES if not hasattr(A, name)]
    if missing:
        raise SystemExit(f"pyscopeoracle: graphite.extract.ast lacks {missing}; cannot pair sites")
    parser = A._LOADER.parser("python")
    out = Extraction()
    events: list[tuple[Any, ...]] = []
    real_target, real_edge = A._python_call_target, A._edge

    def target(func: Any) -> Any:
        found = real_target(func)
        events.append(("site", found, func.start_point[0] + 1))
        return found

    def edge(*args: Any, **kwargs: Any) -> Any:
        made = real_edge(*args, **kwargs)
        if made.get("relation") == "calls":
            events.append(("edge", made))
        return made

    A._python_call_target, A._edge = target, edge
    try:
        for rel, source in corpus.files.items():
            events.clear()
            tree = parser.parse(source)
            ids = [node.id for node in _ts_calls(tree.root_node)]
            out.call_nodes += len(ids)
            out.distinct_call_ids += len(set(ids))
            result = A._extract_python(A._file_node_id(rel), rel, source, tree, corpus.index)
            out.nodes.extend(result.nodes)
            out.edges.extend(result.edges)
            pending: tuple[Any, ...] | None = None
            for event in events:
                if event[0] == "site":
                    pending = event
                    continue
                made = event[1]
                bare = pending[1][0] if pending is not None else None
                made["_oracle_seq"] = len(out.calls)
                out.calls.append({
                    "file": rel,
                    "line": int(str(made.get("source_location", "L0"))[1:]),
                    "site_line": pending[2] if pending is not None else 0,
                    "name": bare,
                    "kind": "bare" if bare else ("member" if "_member" in made else "alias"),
                    "edge": made,
                })
                pending = None
    finally:
        A._python_call_target, A._edge = real_target, real_edge
    return out


def denotations(nodes: list[dict[str, Any]]) -> dict[str, str]:
    """id -> `file:line` of the definition `_merge` keeps for it, or `file:file`.

    Mirrors `_merge`: nodes sorted stably by (id, source_file), first one kept.
    """
    kept: dict[str, str] = {}
    for n in sorted(nodes, key=lambda n: (n.get("id", ""), n.get("source_file", ""))):
        if n["id"] in kept:
            continue
        if n.get("kind") in ("function", "class"):
            kept[n["id"]] = f"{n['source_file']}:{str(n.get('source_location', 'L0'))[1:]}"
        elif n.get("kind") == "file":
            kept[n["id"]] = f"{n['source_file']}:file"
    return kept


def _bound(denote: dict[str, str], target: str) -> str | None:
    """The definition an edge target reaches, or None when it reaches none."""
    found = denote.get(target)
    return None if found is None or found.endswith(":file") else found


# --- CPython's side ---------------------------------------------------------------


@dataclass
class Frame:
    kind: str  # "module" | "function" | "class" | "comp"
    node: Any
    table: Any = None  # symtable for module / function / class frames
    targets: frozenset[str] = frozenset()  # comprehension targets


def _store_names(target: Any) -> list[str]:
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]


def _evaluated_outside(node: Any) -> list[Any]:
    """Parts of a def / lambda / class evaluated in the ENCLOSING scope."""
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *(k.value for k in node.keywords)]
    args = node.args
    parts: list[Any] = [*getattr(node, "decorator_list", [])]
    parts.extend(args.defaults)
    parts.extend(d for d in args.kw_defaults if d is not None)
    for arg in (*args.posonlyargs, *args.args, args.vararg, *args.kwonlyargs, args.kwarg):
        if arg is not None and arg.annotation is not None:
            parts.append(arg.annotation)
    if getattr(node, "returns", None) is not None:
        parts.append(node.returns)
    return parts


def _scope_binders(scope: Any) -> tuple[dict[str, list[Truth]], list[Any], set[str]]:
    """(binders by name, import statements, comprehension targets) of ONE scope.

    A binder is ("def", line), ("class", line) or ("value",); imports are
    returned unexpanded. Comprehension targets bind in the comprehension, not
    here, so they are reported apart; a walrus inside one still binds here.
    """
    binders: dict[str, list[Truth]] = collections.defaultdict(list)
    imports: list[Any] = []
    comp_targets: set[str] = set()
    in_function = isinstance(scope, (*FUNCS, ast.Lambda))
    if in_function:
        args = scope.args
        for arg in (*args.posonlyargs, *args.args, args.vararg, *args.kwonlyargs, args.kwarg):
            if arg is not None:
                binders[arg.arg].append(("value",))
        todo: list[Any] = [scope.body] if isinstance(scope, ast.Lambda) else list(scope.body)
    else:
        todo = list(scope.body)
    while todo:
        node = todo.pop()
        if isinstance(node, FUNCS):
            binders[node.name].append(("def", node.lineno))
            todo.extend(_evaluated_outside(node))
            continue
        if isinstance(node, ast.ClassDef):
            binders[node.name].append(("class", node.lineno))
            todo.extend(_evaluated_outside(node))
            continue
        if isinstance(node, ast.Lambda):
            todo.extend(_evaluated_outside(node))
            continue
        if isinstance(node, COMPS):
            for generator in node.generators:
                comp_targets.update(_store_names(generator.target))
                todo.append(generator.iter)
                todo.extend(generator.ifs)
            todo.extend([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt])
            continue
        if isinstance(node, ast.AnnAssign) and node.value is None and not in_function:
            todo.append(node.annotation)  # `x: int` at module or class scope binds nothing
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            binders[node.id].append(("value",))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            binders[node.name].append(("value",))
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            binders[node.name].append(("value",))
        elif isinstance(node, ast.MatchMapping) and node.rest:
            binders[node.rest].append(("value",))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        todo.extend(ast.iter_child_nodes(node))
    return binders, imports, comp_targets


def _import_bindings(statement: Any, rel: str, index: Any) -> list[tuple[str, str, Any]]:
    """(local, kind, target) per name, by graphite's own module resolution.

    Module resolution is not what phase 1 changes, so the oracle reuses it; what
    it checks independently is WHERE each binding lives and what it denotes.
    """
    out: list[tuple[str, str, Any]] = []
    if isinstance(statement, ast.Import):
        for alias in statement.names:
            if alias.asname:
                found = index.resolve_python_module(rel, alias.name)
                out.append((alias.asname, "alias" if found else "external", found))
            else:
                root = alias.name.split(".", 1)[0]
                found = index.resolve_python_module(rel, alias.name if "." not in alias.name else root)
                out.append((root, "alias" if found else "external", found))
        return out
    base, dots = statement.module or "", statement.level
    for alias in statement.names:
        if alias.name == "*":
            continue
        local = alias.asname or alias.name
        sub = f"{base}.{alias.name}" if base else alias.name
        if index.resolve_python_module(rel, sub, dots):
            out.append((local, "alias", None))
            continue
        parent = index.resolve_python_module(rel, base, dots)
        out.append((local, "symbol", (parent, alias.name)) if parent else (local, "external", None))
    return out


def _lookup(table: Any, name: str) -> Any:
    if table is None:
        return None
    try:
        return table.lookup(name)
    except KeyError:
        return None


def _rebinds(table: Any, name: str, how: str) -> bool:
    """Does a function below `table` declare `name` global/nonlocal AND bind it?"""
    for child in table.get_children():
        symbol = _lookup(child, name)
        if symbol is not None and (symbol.is_assigned() or symbol.is_imported()):
            if how == "global" and symbol.is_declared_global():
                return True
            if how == "nonlocal" and symbol.is_nonlocal():
                return True
        if _rebinds(child, name, how):
            return True
    return False


class TruthModel:
    """CPython's answer, from `symtable` and `ast`, for every file of one corpus."""

    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus
        self.trees: dict[str, Any] = {}
        self.tables: dict[str, Any] = {}
        self.cache: dict[tuple[str, int], tuple[dict[str, list[Truth]], set[str]]] = {}
        self.disagreements: list[str] = []
        self.parse_errors = 0

    def tree(self, rel: str) -> Any:
        if rel not in self.trees:
            source = self.corpus.files.get(rel)
            if source is None:
                path = self.corpus.root / rel
                source = path.read_bytes() if path.is_file() else None
            try:
                self.trees[rel] = ast.parse(source) if source is not None else None
                self.tables[rel] = symtable.symtable(source.decode("utf-8"), rel, "exec") if source is not None else None
            except (SyntaxError, ValueError, UnicodeDecodeError):
                self.trees[rel], self.tables[rel] = None, None
                self.parse_errors += 1
        return self.trees[rel]

    def binders(self, rel: str, scope: Any) -> tuple[dict[str, list[Truth]], set[str]]:
        key = (rel, id(scope))
        if key not in self.cache:
            found, imports, comp_targets = _scope_binders(scope)
            for statement in imports:
                for local, kind, target in _import_bindings(statement, rel, self.corpus.index):
                    found[local].append(("import", kind, target))
            self.cache[key] = (found, comp_targets)
        return self.cache[key]

    def in_scope(self, rel: str, scope: Any, name: str, depth: int = 0) -> Truth | None:
        """What `name` denotes if `scope` binds it; None when it binds nothing there."""
        found = self.binders(rel, scope)[0].get(name)
        if not found:
            return None
        if any(b[0] == "value" for b in found):
            return ("value",)
        if len(found) > 1:
            return ("ambiguous",)
        binder = found[0]
        if binder[0] in ("def", "class"):
            return ("def", rel, binder[1])
        _, kind, target = binder
        if kind == "external":
            return ("external",)
        if kind == "alias":
            return ("value",)  # a module object: calling it reaches no definition
        target_file, original = target
        if depth >= MAX_HOPS:
            return ("none",)
        resolved = self.in_module(target_file, original, depth + 1)
        if resolved[0] == "def" and depth >= 1:
            return ("def", resolved[1], resolved[2], "reexport")
        return resolved

    def in_module(self, rel: str, name: str, depth: int = 0) -> Truth:
        tree = self.tree(rel)
        if tree is None:
            return ("none",)
        if _rebinds(self.tables[rel], name, "global"):
            return ("rebound",)
        return self.in_scope(rel, tree, name, depth) or ("none",)

    def in_function(self, rel: str, name: str, frame: Frame) -> Truth | None:
        """The function frame's own answer, or None to keep looking outward."""
        symbol = _lookup(frame.table, name)
        found = self.in_scope(rel, frame.node, name)
        if symbol is not None:
            local = symbol.is_local() and not symbol.is_declared_global()
            comp_only = name in self.binders(rel, frame.node)[1]
            if local and found is None and not comp_only:
                self.disagreements.append(f"{rel}:{frame.node.lineno}:{name}: symtable local, no binder")
            elif not local and found is not None and not symbol.is_nonlocal():
                self.disagreements.append(f"{rel}:{frame.node.lineno}:{name}: binder, symtable not local")
        if found is not None and frame.table is not None and _rebinds(frame.table, name, "nonlocal"):
            return ("rebound",)
        return found

    def outward(self, rel: str, name: str, frames: list[Frame]) -> Truth:
        """Look `name` up from just outside `frames[-1]`'s body, skipping classes."""
        for frame in reversed(frames):
            if frame.kind == "module":
                return self.in_module(rel, name)
            if frame.kind == "class":
                continue
            if frame.kind == "comp":
                if name in frame.targets:
                    return ("value",)
                continue
            symbol = _lookup(frame.table, name)
            if symbol is not None and symbol.is_declared_global():
                return self.in_module(rel, name)
            found = self.in_function(rel, name, frame)
            if found is not None:
                return found
        return self.in_module(rel, name)

    def resolve(self, rel: str, name: str, frames: list[Frame]) -> Truth:
        """What a bare `name` called inside `frames[-1]` denotes."""
        i = len(frames) - 1
        crossed = False
        while frames[i].kind == "comp":
            if name in frames[i].targets:
                return ("value",)
            crossed = True
            i -= 1
        frame = frames[i]
        if frame.kind == "module":
            return self.in_module(rel, name)
        if frame.kind == "class":
            if crossed:  # a comprehension in a class body never sees the class scope
                return self.outward(rel, name, frames[:i])
            found = self.in_scope(rel, frame.node, name)
            if found is not None:
                return found
            symbol = _lookup(frame.table, name)
            if symbol is not None and symbol.is_free():
                return self.outward(rel, name, frames[:i])
            return self.in_module(rel, name)
        symbol = _lookup(frame.table, name)
        if symbol is not None and symbol.is_declared_global():
            return self.in_module(rel, name)
        if symbol is not None and symbol.is_nonlocal():
            if symbol.is_assigned() or symbol.is_imported():
                return ("rebound",)
            return self.outward(rel, name, frames[:i])
        found = self.in_function(rel, name, frame)
        return found if found is not None else self.outward(rel, name, frames[:i])

    def sites(self, rel: str) -> dict[tuple[int, str], list[Truth]]:
        """(line, name) -> truth for every bare-name call, in source order."""
        found: dict[tuple[int, str], list[Truth]] = collections.defaultdict(list)
        tree = self.tree(rel)
        if tree is None:
            return found
        tables: dict[tuple[str, int], Any] = {}

        def index(table: Any) -> None:
            for child in table.get_children():
                tables.setdefault((child.get_name(), child.get_lineno()), child)
                index(child)

        index(self.tables[rel])

        def visit(node: Any, frames: list[Frame]) -> None:
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                found[(node.func.lineno, node.func.id)].append(self.resolve(rel, node.func.id, frames))
            if isinstance(node, (*FUNCS, ast.Lambda)):
                for part in _evaluated_outside(node):
                    visit(part, frames)
                key = ("lambda", node.lineno) if isinstance(node, ast.Lambda) else (node.name, node.lineno)
                inner = [*frames, Frame("function", node, tables.get(key))]
                for statement in [node.body] if isinstance(node, ast.Lambda) else node.body:
                    visit(statement, inner)
                return
            if isinstance(node, ast.ClassDef):
                for part in _evaluated_outside(node):
                    visit(part, frames)
                inner = [*frames, Frame("class", node, tables.get((node.name, node.lineno)))]
                for statement in node.body:
                    visit(statement, inner)
                return
            if isinstance(node, COMPS):
                first = node.generators[0]
                visit(first.iter, frames)  # the first iterable is evaluated outside
                targets = frozenset(n for g in node.generators for n in _store_names(g.target))
                inner = [*frames, Frame("comp", node, None, targets)]
                for generator in node.generators:
                    if generator is not first:
                        visit(generator.iter, inner)
                    for condition in generator.ifs:
                        visit(condition, inner)
                for part in [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]:
                    visit(part, inner)
                return
            for child in ast.iter_child_nodes(node):
                visit(child, frames)

        visit(tree, [Frame("module", tree, self.tables[rel])])
        return found


# --- scoring --------------------------------------------------------------------


def classify(truth: Truth, bound: str | None) -> str:
    """One scored site; `bound` is `file:line` of the definition the edge reaches."""
    kind = truth[0]
    if kind == "def":
        if bound is None:
            return "unbound-but-def(reexport)" if len(truth) > 3 else "unbound-but-def"
        return "correct" if bound == f"{truth[1]}:{truth[2]}" else "WRONG-def->other-def"
    if kind in ("value", "external", "none"):
        return "correct-unbound" if bound is None else f"WRONG-{kind}-bound"
    return f"truth-{kind}"


def measure_sites(corpus: Corpus) -> dict[str, Any]:
    """Score every bare-name call site that has a non-external edge."""
    run = extract(corpus)
    denote = denotations(run.nodes)
    model = TruthModel(corpus)
    stats: collections.Counter[str] = collections.Counter()
    examples: dict[str, list[str]] = collections.defaultdict(list)
    by_file: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for call in run.calls:
        if call["kind"] == "bare":
            by_file[call["file"]].append(call)
    for rel in sorted(by_file):
        truths = model.sites(rel)
        taken: collections.Counter[tuple[int, str]] = collections.Counter()
        lines = corpus.files[rel].decode("utf-8", errors="replace").splitlines()
        for call in by_file[rel]:
            key = (call["site_line"], call["name"])
            options = truths.get(key, [])
            truth = options[taken[key]] if taken[key] < len(options) else ("unmatched",)
            taken[key] += 1
            edge = call["edge"]
            if edge.get("confidence") == "EXTERNAL_CALL":
                stats["external-edge"] += 1
                continue
            stats["sites"] += 1
            bound = _bound(denote, edge["target"])
            category = classify(truth, bound)
            stats[category] += 1
            # Every WRONG site is kept (they are the gate); other categories are sampled.
            keep = category.startswith("WRONG") or (
                category.startswith(("unbound-but-def", "truth-unmatched")) and len(examples[category]) < 15
            )
            if keep:
                line = call["site_line"]
                text = lines[line - 1].strip()[:100] if 0 < line <= len(lines) else ""
                examples[category].append(f"{rel}:{line}: {text}  [graphite {bound} truth {truth}]")
    return {
        "files": len(corpus.files),
        "wrong": sum(n for category, n in stats.items() if category.startswith("WRONG")),
        "stats": dict(sorted(stats.items())),
        "examples": dict(examples),
        "selfcheck": {
            "call_nodes": run.call_nodes,
            "distinct_call_ids": run.distinct_call_ids,
            "symtable_disagreements": len(model.disagreements),
            "disagreement_examples": model.disagreements[:15],
            "parse_errors": model.parse_errors,
        },
    }


def dump(corpus: Corpus) -> dict[str, Any]:
    """Every call edge as (caller, targets, confidence) denotations, after dispatch."""
    import graphite.extract.ast as A

    run = extract(corpus)
    denote = denotations(run.nodes)
    methods: dict[str, set[str]] = collections.defaultdict(set)
    for n in run.nodes:
        if n.get("is_method") and n.get("name"):
            methods[n["name"].casefold()].add(n["id"])
    dispatched = A._resolve_method_dispatch(run.nodes, [dict(e) for e in run.edges])
    outputs: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    for e in dispatched:
        if "_oracle_seq" in e:
            outputs[e["_oracle_seq"]].append(e)
    records: dict[str, dict[str, Any]] = {}
    ordinal: collections.Counter[tuple[str, int]] = collections.Counter()
    summary: collections.Counter[str] = collections.Counter()
    for seq, call in enumerate(run.calls):
        edge = call["edge"]
        place = (call["file"], call["line"])
        key = f"{call['file']}:{call['line']}:{ordinal[place]}"
        ordinal[place] += 1
        made = outputs.get(seq, [])
        member = edge.get("_member")
        if not member:
            status = "n/a"
        elif not made:
            status = "dropped"
        elif len(made) > 1:
            status = "fanout"
        else:
            status = "kept" if made[0]["target"] == edge["target"] else "repointed"
        candidates = len(methods.get(member.casefold(), ())) if member else None
        summary[f"{call['kind']}:{status}"] += 1
        if status in ("kept", "dropped") and candidates is not None and candidates > 3:
            summary["member:over-cap(ungated)"] += 1
        records[key] = {
            "kind": call["kind"],
            "name": call["name"],
            "caller": denote.get(edge["source"], "unknown"),
            "targets": sorted({_bound(denote, e["target"]) or "unbound" for e in made}),
            "confidence": edge.get("confidence"),
            "dispatch": status,
            "member_candidates": candidates,
        }
    return {"files": len(corpus.files), "dispatch": dict(sorted(summary.items())), "records": records}


def compare(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Pre-registered denotation categories between two dumps (spec §5).

    A record can land in several. Anything outside these names is a finding.
    """
    old_records, new_records = old["records"], new["records"]
    stats: collections.Counter[str] = collections.Counter()
    examples: dict[str, list[str]] = collections.defaultdict(list)
    stats["only-old"] = len(old_records.keys() - new_records.keys())
    stats["only-new"] = len(new_records.keys() - old_records.keys())
    renamed = {"bare": "bare-target-changed", "member": "dispatch-changed", "alias": "alias-target-changed"}
    for key in sorted(old_records.keys() & new_records.keys()):
        a, b = old_records[key], new_records[key]
        changes = []
        if a["caller"] != b["caller"]:
            changes.append("caller-reattributed")
        if a["targets"] != b["targets"]:
            changes.append(renamed[b["kind"]])
        if a["confidence"] != b["confidence"]:
            changes.append(f"confidence-{a['confidence']}->{b['confidence']}")
            if b["kind"] == "member" and a["targets"] != b["targets"]:
                changes.append("confidence-flip-then-dispatched")
        if not changes:
            stats["same"] += 1
        for change in changes:
            stats[change] += 1
            if len(examples[change]) < 15:
                examples[change].append(f"{key}: {a} -> {b}")
    return {"stats": dict(sorted(stats.items())), "examples": dict(examples)}


def _engine() -> dict[str, str]:
    import graphite
    import graphite.extract.ast as A

    return {
        "version": str(getattr(graphite, "__version__", "?")),
        "extractor": str(Path(A.__file__).resolve()),
        # Two reports differ for an ENGINE reason only when this hash matches too.
        "oracle_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grade graphite's Python bare-name call binding.")
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("sites", "dump"):
        p = sub.add_parser(mode)
        p.add_argument("root", type=Path)
        p.add_argument("--only", default=None, help="rel-path prefix to extract and score, e.g. django/")
        p.add_argument("--out", type=Path, default=None)
    p = sub.add_parser("compare")
    p.add_argument("old", type=Path)
    p.add_argument("new", type=Path)
    p.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    if args.mode == "compare":
        report = compare(
            json.loads(args.old.read_text(encoding="utf-8")),
            json.loads(args.new.read_text(encoding="utf-8")),
        )
    else:
        corpus = load(args.root, args.only)
        report = measure_sites(corpus) if args.mode == "sites" else dump(corpus)
        report["engine"] = _engine()
    if args.out is not None:
        args.out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("examples", "records")}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests and lint the script the way the gate does.**

```bash
$PY -m pytest tests/test_pyscopeoracle.py -q > "$SCRATCH/t4b.txt" 2>&1; echo "EXIT=$?"; tail -3 "$SCRATCH/t4b.txt"
python -m ruff check --extend-select S scripts/pyscopeoracle.py tests/test_pyscopeoracle.py
```

Expected: `EXIT=0`, 20 passed, and `All checks passed!`. `test_todays_engine_mis_binds_the_three_planted_sites` passing means today's engine IS wrong in the three planted places: that is the control firing.

- [ ] **Step 5: Build the two corpora (scratch, not repositories).**

```bash
mkdir -p "$SCRATCH/corpus/graphite-f9e7014" "$EVIDENCE"
git archive --format=tar f9e7014 | tar -x -C "$SCRATCH/corpus/graphite-f9e7014"
curl -sSL -o "$SCRATCH/corpus/django-5.2.7.tar.gz" https://files.pythonhosted.org/packages/source/d/django/django-5.2.7.tar.gz
echo "e0f6f12e2551b1716a95a63a1366ca91bbcd7be059862c1b18f989b1da356cdd  $SCRATCH/corpus/django-5.2.7.tar.gz" | sha256sum -c -
tar -xzf "$SCRATCH/corpus/django-5.2.7.tar.gz" -C "$SCRATCH/corpus"
ls "$SCRATCH/corpus/django-5.2.7/django/utils/dateformat.py" "$SCRATCH/corpus/graphite-f9e7014/src/graphite/extract/ast.py"
```

Expected: `sha256sum` prints `OK`, and both files are listed. If the checksum fails, stop and report: a different sdist is a different corpus.

`f9e7014` is HEAD before any phase 1 change. The census harness behind spec §2 ran on `f006763`; the difference between the two is the first thing to explain if graphite's numbers differ from 118.

- [ ] **Step 6: Run the CONTROL on both corpora with today's engine (dev venv).**

```bash
$PY -P scripts/pyscopeoracle.py sites "$SCRATCH/corpus/graphite-f9e7014" --out "$EVIDENCE/sites-graphite-control.json" > "$SCRATCH/c-g.txt" 2>&1; echo "EXIT=$?"
$PY -P scripts/pyscopeoracle.py sites "$SCRATCH/corpus/django-5.2.7" --only django/ --out "$EVIDENCE/sites-django-control.json" > "$SCRATCH/c-d.txt" 2>&1; echo "EXIT=$?"
cat "$SCRATCH/c-g.txt" "$SCRATCH/c-d.txt"
```

Then check each of these. A miss is a stop-and-report, not a re-run:
- `engine.extractor` is `F:\Projects\graphite\src\graphite\extract\ast.py`, the dev tree. Do not read `engine.version`: the editable tree also says 1.1.1.
- `engine.oracle_sha256` is the same in both reports. Copy it into `control-notes.md`. Tasks 5 and 9 compare later reports against these two files, and a comparison is only about the ENGINE when this hash matches.
- `wrong` > 0 on both corpora.
- `selfcheck.call_nodes == selfcheck.distinct_call_ids` on both. `ScopeTable` keys on `Node.id`, so this checks that `Node.id` is unique per call node.
- The known real sites are present. Check with:

```bash
$PY - "$EVIDENCE" <<'EOF'
import json, sys
from pathlib import Path
ev = Path(sys.argv[1])
g = json.loads((ev / "sites-graphite-control.json").read_text(encoding="utf-8"))["examples"]
d = json.loads((ev / "sites-django-control.json").read_text(encoding="utf-8"))["examples"]
def hit(examples, category, path, text):
    return any(path in e and text in e for e in examples.get(category, []))
print("ast.py nested visit ->", hit(g, "WRONG-def->other-def", "src/graphite/extract/ast.py", "visit(child)"))
print("_text parameter     ->", hit(g, "WRONG-value-bound", "src/graphite/extract/ast.py", "_text("))
print("password_changed    ->", any("password_validation.py" in e for c in d if c.startswith("WRONG") for e in d[c]))
print("dateformat format   ->", any("dateformat.py" in e for c in d if c.startswith("WRONG") for e in d[c]))
EOF
```

  Expected: four `True` lines.
- Write `$EVIDENCE/control-notes.md` with:
  - each corpus's `stats`, set beside the census numbers in spec §2 (graphite 118 wrong of 12,082 sites; Django 76 of 6,517);
  - an explanation for every difference. Expected causes: the `f9e7014` vs `f006763` snapshot, and the oracle's `truth-rebound`, `truth-ambiguous` and first-iterable handling that the census harness lacked;
  - every `selfcheck.disagreement_examples` entry, read at its source line and explained.

  A disagreement that cannot be explained is an oracle defect: fix the oracle (TDD, in this task) and repeat this step.

- [ ] **Step 7: Commit, with the maintainer's go.** The evidence directory is outside the repo and is not committed.

```bash
git add scripts/pyscopeoracle.py tests/test_pyscopeoracle.py
aramid check --staged
git commit -m "test(oracle): pyscopeoracle -- symtable-backed grading of Python bare-name binding; control on today's engine"
```

---
### Task 5: per-statement import bindings (a refactor; output must not change)

`_collect_python_import_maps` walks every import at every depth into three file-wide maps. That flattening is exactly the #71 defect: a function-local import binds the name for the whole file. This task splits out the per-statement half, `_python_import_bindings`, and rebuilds the old function on top of it, so that this commit changes NO output. Task 6 then hands the per-statement half to `pyscope`, which decides where each binding lives.

**Files:**
- Modify: `src/graphite/extract/ast.py`: add `_python_import_bindings` directly above `_collect_python_import_maps`, and rebuild `_collect_python_import_maps` on it.
- Create: `tests/test_python_import_bindings.py`

**Interfaces:**
- Consumes: `_python_import_modules`, `_file_node_id`, `_make_id`, `SourceIndex.resolve_python_module` (existing).
- Produces: `_python_import_bindings(node, rel_path: str, source_index: SourceIndex | None) -> list[tuple[str, str, str | None]]`. Each tuple is `(local name, kind, target)`, with kind one of:
  - `"alias"`: a module. The target is its file id, or None for the package root that `import pkg.sub` binds.
  - `"symbol"`: the target is the definition id in the module's file.
  - `"external"`: the import did not resolve in-repo; the target is None.
  - `"unknown"`: there is no source index; the target is None.

  Star imports bind nothing.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_python_import_bindings.py`:

```python
"""Per-statement Python import bindings (#71).

`_python_import_bindings` is the per-statement half of what used to be the
file-wide `_collect_python_import_maps`: it says what each name ONE import
statement binds denotes. Where the binding lives is the scope walk's business
(`extract/pyscope.py`); flattening every import into file-wide maps was #71.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.config import Config
from graphite.extract.ast import _LOADER, _file_node_id, _make_id, _python_import_bindings
from graphite.ingest import collect_files
from graphite.resolve import SourceIndex

REPO = {
    "flatmod.py": "def func():\n    return 1\n",
    "pkg/__init__.py": "",
    "pkg/sub.py": "def f():\n    return 1\n\n\ndef g():\n    return 2\n",
}
FLAT = _file_node_id("flatmod.py")
SUB = _file_node_id("pkg/sub.py")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _bindings(tmp_path: Path, statement: str, rel: str = "m.py", *, indexed: bool = True):
    for path, text in REPO.items():
        _write(tmp_path / path, text)
    _write(tmp_path / rel, statement + "\n")
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite", typescript_resolver="disabled")
    index = SourceIndex.from_entries(collect_files(tmp_path, cfg), cfg) if indexed else None
    tree = _LOADER.parser("python").parse((statement + "\n").encode())
    (node,) = [c for c in tree.root_node.children if c.type in ("import_statement", "import_from_statement")]
    return _python_import_bindings(node, rel, index)


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("import flatmod", [("flatmod", "alias", FLAT)]),
        ("import json", [("json", "external", None)]),
        ("import pkg.sub", [("pkg", "alias", None)]),
        ("import nopkg.sub", [("nopkg", "external", None)]),
        ("import pkg.sub as s", [("s", "alias", SUB)]),
        ("import json as j", [("j", "external", None)]),
        ("import flatmod, json", [("flatmod", "alias", FLAT), ("json", "external", None)]),
        ("from pkg import sub", [("sub", "alias", SUB)]),
        ("from pkg.sub import f, g as h", [("f", "symbol", _make_id(SUB, "f")), ("h", "symbol", _make_id(SUB, "g"))]),
        ("from pkg.sub import (\n    f,\n    g,\n)", [("f", "symbol", _make_id(SUB, "f")), ("g", "symbol", _make_id(SUB, "g"))]),
        ("from json import loads", [("loads", "external", None)]),
        ("from pkg.sub import *", []),
    ],
    ids=[
        "module", "external-module", "dotted-binds-root", "dotted-external-root", "dotted-alias",
        "external-alias", "two-modules", "from-submodule", "from-symbols", "parenthesized",
        "from-external", "star",
    ],
)
def test_each_name_an_import_binds(tmp_path, statement, expected):
    assert _bindings(tmp_path, statement) == expected


def test_relative_imports_bind_through_the_package(tmp_path):
    assert _bindings(tmp_path, "from . import sub", rel="pkg/m.py") == [("sub", "alias", SUB)]
    assert _bindings(tmp_path, "from .sub import f", rel="pkg/m.py") == [("f", "symbol", _make_id(SUB, "f"))]


def test_without_a_source_index_every_binding_is_unknown(tmp_path):
    assert _bindings(tmp_path, "from x import a, b as c", indexed=False) == [
        ("a", "unknown", None),
        ("c", "unknown", None),
    ]
    assert _bindings(tmp_path, "import a.b", indexed=False) == [("a", "unknown", None)]
```

- [ ] **Step 2: Run the tests and watch them fail.**

```bash
$PY -m pytest tests/test_python_import_bindings.py -q > "$SCRATCH/t5.txt" 2>&1; echo "EXIT=$?"; tail -5 "$SCRATCH/t5.txt"
```

Expected: `EXIT=2`, with `ImportError: cannot import name '_python_import_bindings'`.

- [ ] **Step 3: Implement.** In `src/graphite/extract/ast.py`, add directly above `def _collect_python_import_maps(`:

```python
def _python_import_bindings(
    node: Any, rel_path: str, source_index: SourceIndex | None
) -> list[tuple[str, str, str | None]]:
    """(local name, kind, target) for each name ONE import statement binds.

    kind is "alias" (a module: target its file id, or None for the package root
    `import pkg.sub` binds), "symbol" (target the definition id in the module's
    file), "external" (did not resolve in-repo), or "unknown" (no source index
    to ask). For `from P import name`, `P.name` is tried as a MODULE first.
    Star imports bind nothing this can name.

    Where each binding LIVES is not decided here. The scope walk
    (`extract/pyscope.py`) binds it in the scope the statement sits in, so a
    function-local import no longer binds the name for the whole file (#71).
    """
    def _text(n: Any) -> str:
        return n.text.decode("utf-8", errors="ignore") if n is not None and n.text else ""

    out: list[tuple[str, str, str | None]] = []
    if node.type == "import_statement":
        for child in node.children:
            if child.type == "dotted_name":
                module = _text(child)
                if not module:
                    continue
                root = module.split(".", 1)[0]
                if source_index is None:
                    out.append((root, "unknown", None))
                elif "." not in module:
                    resolved = source_index.resolve_python_module(rel_path, module)
                    out.append((module, "alias", _file_node_id(resolved)) if resolved else (module, "external", None))
                elif source_index.resolve_python_module(rel_path, root) is None:
                    # `import pkg.sub` binds only the root name `pkg`. Mark it
                    # external ONLY if that root does not resolve in-repo -- a
                    # false external costs more than a missed one (spec §4.2).
                    out.append((root, "external", None))
                else:
                    out.append((root, "alias", None))
            elif child.type == "aliased_import":
                module = _text(child.child_by_field_name("name"))
                local = _text(child.child_by_field_name("alias"))
                if not module or not local:
                    continue
                if source_index is None:
                    out.append((local, "unknown", None))
                    continue
                resolved = source_index.resolve_python_module(rel_path, module)
                out.append((local, "alias", _file_node_id(resolved)) if resolved else (local, "external", None))
    elif node.type == "import_from_statement":
        modules = _python_import_modules(node)
        if not modules:
            return out
        base_module, dots = modules[0]
        module_field = node.child_by_field_name("module_name")
        for child in node.children:
            if module_field is not None and child.id == module_field.id:
                # The module_name field's own dotted_name is ALSO a plain child.
                # Skipped by identity: sibling-token sniffing missed the first
                # name inside parentheses (`from x import (a, b)`).
                continue
            if child.type == "dotted_name":
                original = local = _text(child)
            elif child.type == "aliased_import":
                original = _text(child.child_by_field_name("name"))
                local = _text(child.child_by_field_name("alias"))
            else:
                continue
            if not original or not local or "." in original:
                continue
            if source_index is None:
                out.append((local, "unknown", None))
                continue
            sub = f"{base_module}.{original}" if base_module else original
            as_module = source_index.resolve_python_module(rel_path, sub, dots)
            if as_module:
                out.append((local, "alias", _file_node_id(as_module)))
                continue
            parent = source_index.resolve_python_module(rel_path, base_module, dots)
            if parent:
                out.append((local, "symbol", _make_id(_file_node_id(parent), original)))
            else:
                out.append((local, "external", None))
    return out
```

Replace the whole body of `_collect_python_import_maps` (from `symbol_map: dict[str, str] = {}` through `return symbol_map, alias_map, frozenset(external)`, keeping its signature and docstring) with:

```python
    symbol_map: dict[str, str] = {}
    alias_map: dict[str, str] = {}
    external: set[str] = set()
    if source_index is None:
        return symbol_map, alias_map, frozenset()

    def visit(node: Any) -> None:
        if node.type in ("import_statement", "import_from_statement"):
            for local, kind, target in _python_import_bindings(node, rel_path, source_index):
                if kind == "alias" and target is not None:
                    alias_map[local] = target
                elif kind == "symbol" and target is not None:
                    symbol_map[local] = target
                elif kind == "external":
                    external.add(local)
        for child in node.children:
            visit(child)

    visit(root)
    return symbol_map, alias_map, frozenset(external)
```

- [ ] **Step 4: Run the new tests, and prove the refactor changed nothing.**

```bash
$PY -m pytest tests/test_python_import_bindings.py tests/test_python_resolver.py tests/test_external_calls.py tests/test_call_graph.py tests/test_method_dispatch_scope.py tests/test_dispatch_evidence.py tests/test_pyscopeoracle.py -q > "$SCRATCH/t5b.txt" 2>&1; echo "EXIT=$?"; tail -3 "$SCRATCH/t5b.txt"
$PY -P scripts/pyscopeoracle.py sites "$SCRATCH/corpus/graphite-f9e7014" --out "$SCRATCH/sites-graphite-task5.json" > /dev/null 2>&1; echo "ORACLE=$?"
$PY - "$EVIDENCE/sites-graphite-control.json" "$SCRATCH/sites-graphite-task5.json" <<'EOF'
import json, sys
a, b = (json.load(open(p, encoding="utf-8")) for p in sys.argv[1:3])
print("same oracle" if a["engine"]["oracle_sha256"] == b["engine"]["oracle_sha256"] else "ORACLE CHANGED")
print("identical" if (a["stats"], a["examples"]) == (b["stats"], b["examples"]) else "DIFFERENT")
EOF
```

Expected: `EXIT=0`, `ORACLE=0`, `same oracle`, `identical`. A refactor that moves a single site's category is not a refactor; if the output reads `DIFFERENT`, stop and find out why. `ORACLE CHANGED` means `scripts/pyscopeoracle.py` was edited after the control ran, so `identical` proves nothing about the engine. In that case, stash this task's `ast.py` change, re-run the control with the current oracle, and compare again.

- [ ] **Step 5: Commit, with the maintainer's go.**

```bash
git add src/graphite/extract/ast.py tests/test_python_import_bindings.py
aramid check --staged
git commit -m "refactor(extract): per-statement Python import bindings; file-wide maps rebuilt on them"
```

---

### Task 6: rewire `_extract_python` onto the scope table

**Files:**
- Modify: `src/graphite/extract/ast.py`:
  - add an import of `pyscope`;
  - add the helpers `_python_definition_name`, `_python_def_id`, `_python_name_confidence` and `_python_bare_call`;
  - replace `_extract_python`;
  - delete `_collect_python_import_maps`;
  - edit `_call_confidence`'s docstring.
- Test: `tests/test_python_scope_identity.py` (append).
- Modify (ids looked up, not spelled): `tests/test_dispatch_evidence.py`, `tests/test_method_dispatch_scope.py`, `tests/test_python_resolver.py`, `tests/test_call_graph.py`.
- Modify (the control flips): `tests/test_pyscopeoracle.py`.

**Interfaces:**
- Consumes:
  - Task 2: `_scoped_id`.
  - Task 3: `collect_scopes`, `resolve`, `Resolution`, `ScopeTable`.
  - Task 5: `_python_import_bindings`.
- Produces:
  - every Python def and class node carries a `qualname` (equal to `name` at module scope);
  - method and nested ids are `_scoped_id(file_id, qualname)`;
  - `_python_def_id(file_id: str, qualname: str, module_level: bool) -> str`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_python_scope_identity.py`, replace the import block (everything from `from __future__ import annotations` through the `graphite.extract.ast` import) with:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.config import Config
from graphite.extract.ast import (
    _LOADER,
    _MAX_ID_LEN,
    ExtractionResult,
    _edge,
    _extract_python,
    _file_node_id,
    _make_id,
    _merge,
    _scoped_id,
    extract_all,
)
from graphite.graph import build_graph
from graphite.health import resolution_health
from graphite.ingest import collect_files
```

and append:

```python
# --- extraction (Task 6) --------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(tmp_path: Path, files: dict[str, str]):
    for rel, text in files.items():
        _write(tmp_path / rel, text)
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite", typescript_resolver="disabled")
    return extract_all(collect_files(tmp_path, cfg), cfg)


def _extract_one(source: str, rel: str = "m.py"):
    """The extraction stage for one file: before `_merge`, before dispatch, no index."""
    tree = _LOADER.parser("python").parse(source.encode())
    return _extract_python(_file_node_id(rel), rel, source.encode(), tree, None)


def _defs(result) -> dict[tuple[str, str], dict]:
    return {(n["source_file"], n["qualname"]): n for n in result.nodes if "qualname" in n}


def _ids(result) -> dict[str, str]:
    return {qualname: n["id"] for (_file, qualname), n in _defs(result).items()}


def _call_edges(result) -> list[dict]:
    return [e for e in result.edges if e["relation"] == "calls"]


def _pairs(result) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in _call_edges(result)}


#: Same cases, same ids, as tests/test_pyscope.py's BINDER_CASES: the mutation
#: run (Task 8) names a failing case by id in BOTH files.
BINDER_CASES = [
    ("param", "def use(fn):\n    return fn()\n"),
    ("posonly", "def use(a, /, fn):\n    return fn()\n"),
    ("kwonly", "def use(*, fn):\n    return fn()\n"),
    ("default", "def use(fn=None):\n    return fn()\n"),
    ("typed-default", "def use(fn: int = 1):\n    return fn()\n"),
    ("typed", "def use(fn: int):\n    return fn()\n"),
    ("star", "def use(*fn):\n    return fn()\n"),
    ("starstar", "def use(**fn):\n    return fn()\n"),
    ("lambda", "use = lambda fn: fn()\n"),
    ("assign", "def use():\n    fn = make()\n    return fn()\n"),
    ("annotated", "def use():\n    fn: int = make()\n    return fn()\n"),
    ("annotation-only", "def use():\n    fn: int\n    return fn()\n"),
    ("augmented", "def use():\n    fn += 1\n    return fn()\n"),
    ("walrus", "def use():\n    if (fn := make()):\n        return fn()\n"),
    ("unpack", "def use():\n    a, (b, *fn) = t\n    return fn()\n"),
    ("for", "def use(xs):\n    for fn in xs:\n        fn()\n"),
    ("async-for", "async def use(xs):\n    async for fn in xs:\n        fn()\n"),
    ("with", "def use():\n    with ctx() as fn:\n        fn()\n"),
    ("async-with", "async def use():\n    async with ctx() as (a, fn):\n        fn()\n"),
    ("except", "def use():\n    try:\n        pass\n    except E as fn:\n        fn()\n"),
    ("del", "def use():\n    del fn\n    return fn()\n"),
    ("match-star", "def use(t):\n    match t:\n        case [*fn]:\n            fn()\n"),
    ("match-mapping-rest", "def use(t):\n    match t:\n        case {'k': 1, **fn}:\n            fn()\n"),
    ("match-keyword", "def use(t):\n    match t:\n        case P(x=fn):\n            fn()\n"),
    ("match-as", "def use(t):\n    match t:\n        case P() as fn:\n            fn()\n"),
    ("match-capture", "def use(t):\n    match t:\n        case fn:\n            fn()\n"),
]

SHARED_NAMES = (
    "class Alpha:\n"
    "    def setup(self):\n"
    "        return 1\n"
    "class Beta:\n"
    "    def setup(self):\n"
    "        return 2\n"
    "def test_one():\n"
    "    def fake_build():\n"
    "        return 1\n"
    "    return fake_build()\n"
    "def test_two():\n"
    "    def fake_build():\n"
    "        return 2\n"
    "    return fake_build()\n"
)


def test_methods_and_nested_definitions_get_their_own_node(tmp_path):
    """#70: two `setup` methods, and two nested `fake_build`s, were ONE node each."""
    result = _extract(tmp_path, {"m.py": SHARED_NAMES})
    defs = _defs(result)
    assert defs[("m.py", "Alpha.setup")]["id"] == _scoped_id("m_py", "Alpha.setup")
    assert defs[("m.py", "Beta.setup")]["id"] == _scoped_id("m_py", "Beta.setup")
    assert defs[("m.py", "Alpha.setup")]["is_method"] is True
    one, two = "m_py_test_one_fake_build_4e414e", "m_py_test_two_fake_build_1dc280"
    assert defs[("m.py", "test_one.fake_build")]["id"] == one
    assert defs[("m.py", "test_two.fake_build")]["id"] == two
    calls = _pairs(result)
    assert ("m_py_test_one", one) in calls
    assert ("m_py_test_two", two) in calls
    assert ("m_py_test_two", one) not in calls


def test_module_scope_ids_are_unchanged_and_every_definition_has_a_qualname(tmp_path):
    source = "def helper():\n    return 1\nclass Box:\n    pass\nif X:\n    def guarded():\n        pass\n"
    defs = _defs(_extract(tmp_path, {"m.py": source}))
    for name in ("helper", "Box", "guarded"):
        assert defs[("m.py", name)]["id"] == _make_id("m_py", name)
        assert defs[("m.py", name)]["name"] == name


def test_a_nested_outer_inner_and_a_module_outer_inner_are_two_nodes(tmp_path):
    source = "def outer():\n    def inner():\n        return 1\n    return inner()\ndef outer_inner():\n    return 2\n"
    ids = _ids(_extract(tmp_path, {"m.py": source}))
    assert ids["outer_inner"] == "m_py_outer_inner"
    assert ids["outer.inner"] != ids["outer_inner"]


@pytest.mark.parametrize("body", [body for _id, body in BINDER_CASES], ids=[i for i, _b in BINDER_CASES])
def test_a_call_through_any_local_binding_is_an_unbound_local_value(tmp_path, body):
    """#71, one binder kind per case. The edge stays (the denominator must see the
    site) and points at a phantom no definition owns -- never at `def fn`."""
    result = _extract(tmp_path, {"m.py": "def fn(): pass\n" + body})
    phantom = _scoped_id("m_py", "<local>.fn")
    through = [e for e in _call_edges(result) if e["target"] in (phantom, "m_py_fn")]
    assert through, "the call through the local binding must keep an edge"
    assert {e["target"] for e in through} == {phantom}
    assert {e["confidence"] for e in through} == {"LOCAL_CALL"}


def test_a_local_value_call_stays_in_the_calls_denominator(tmp_path):
    """Dropping the edge would hide the site from the ratio (round 55): kept, unbound."""
    result = _extract(tmp_path, {"m.py": "def generate(): pass\ndef route_c(generate):\n    return generate()\n"})
    cell = resolution_health(build_graph(result.nodes, result.edges))["by_language"]["python"]["calls"]
    assert (cell["total"], cell["bound"]) == (1, 0)


def test_scope_rules_reach_the_graph(tmp_path):
    source = (
        "def helper(): pass\n"
        "def outer():\n"
        "    def helper(): pass\n"
        "    def f():\n"
        "        global helper\n"
        "        return helper()\n"
        "    return helper()\n"
        "def run(): pass\n"
        "class K:\n"
        "    def run(self): pass\n"
        "    def go(self):\n"
        "        return run()\n"
    )
    result = _extract(tmp_path, {"m.py": source})
    ids, calls = _ids(result), _pairs(result)
    assert (ids["outer.f"], ids["helper"]) in calls  # `global` reaches the module def
    assert (ids["outer"], ids["outer.helper"]) in calls  # the enclosing def wins
    assert (ids["K.go"], ids["run"]) in calls  # a method body skips its class scope
    assert (ids["K.go"], ids["K.run"]) not in calls


def test_a_function_local_import_binds_only_in_its_function(tmp_path):
    files = {
        "pkg/__init__.py": "",
        "pkg/util.py": "def tool():\n    return 1\n",
        "m.py": (
            "def tool():\n    return 2\n"
            "def a():\n    from pkg.util import tool\n    return tool()\n"
            "def b():\n    return tool()\n"
        ),
    }
    calls = _pairs(_extract(tmp_path, files))
    imported = _make_id(_file_node_id("pkg/util.py"), "tool")
    assert ("m_py_a", imported) in calls
    assert ("m_py_b", "m_py_tool") in calls
    assert ("m_py_b", imported) not in calls


def test_a_function_local_module_alias_binds_only_in_its_function(tmp_path):
    """§4.2: `module.attr` resolves `module` through the same scope walk."""
    files = {
        "flatmod.py": "def func():\n    return 1\n",
        "m.py": (
            "def a():\n    import flatmod as fm\n    return fm.func()\n"
            "def b():\n    return fm.func()\n"
        ),
    }
    calls = _pairs(_extract(tmp_path, files))
    assert ("m_py_a", "flatmod_py_func") in calls
    assert ("m_py_b", "flatmod_py_func") not in calls


def test_an_unbound_local_call_keeps_todays_placeholder_unless_it_would_bind():
    no_def = _call_edges(_extract_one("def route(generate):\n    return generate()\n"))
    assert [e["target"] for e in no_def] == ["m_py_generate"]
    with_def = _call_edges(_extract_one("def generate(): pass\ndef route(generate):\n    return generate()\n"))
    assert [e["target"] for e in with_def] == [_scoped_id("m_py", "<local>.generate")]


def test_two_unresolved_imports_of_one_name_stay_external(tmp_path):
    """Regression guard (passes before this task too): refinement 4 must not lose it."""
    source = "try:\n    from ujson import loads\nexcept ImportError:\n    from json import loads\nloads()\n"
    (edge,) = _call_edges(_extract(tmp_path, {"m.py": source}))
    assert edge["confidence"] == "EXTERNAL_CALL"


def test_a_local_value_named_like_a_global_is_a_local_receiver():
    """Refinement 2a: `it` is a test-framework global, but here a loop variable."""
    (edge,) = _call_edges(_extract_one("def f(items):\n    for it in items:\n        it.process()\n"))
    assert edge["confidence"] == "LOCAL_CALL"
    (unbound,) = _call_edges(_extract_one("def f():\n    return it.process()\n"))
    assert unbound["confidence"] == "EXTERNAL_CALL"


def test_a_module_definition_named_like_a_global_is_a_local_call():
    """Refinement 2b."""
    (edge,) = _call_edges(_extract_one("def test():\n    return 1\ntest()\n"))
    assert (edge["target"], edge["confidence"]) == ("m_py_test", "LOCAL_CALL")


def test_decorated_methods_get_scoped_ids(tmp_path):
    source = (
        "class K:\n"
        "    @property\n"
        "    def size(self):\n"
        "        return 1\n"
        "    @staticmethod\n"
        "    def make():\n"
        "        return K()\n"
    )
    result = _extract(tmp_path, {"m.py": source})
    defs = _defs(result)
    for qualname in ("K.size", "K.make"):
        assert defs[("m.py", qualname)]["id"] == _scoped_id("m_py", qualname)
        assert defs[("m.py", qualname)]["is_method"] is True
    assert (defs[("m.py", "K.make")]["id"], defs[("m.py", "K")]["id"]) in _pairs(result)


def test_a_recursive_nested_helper_calls_itself_not_its_namesake(tmp_path):
    """The shape graphite's own `extract/ast.py` had: two nested `visit`s, one bound wrong."""
    source = (
        "def a(tree):\n    def visit(n):\n        return visit(n)\n    return visit(tree)\n"
        "def b(tree):\n    def visit(n):\n        return visit(n)\n    return visit(tree)\n"
    )
    result = _extract(tmp_path, {"m.py": source})
    ids, calls = _ids(result), _pairs(result)
    expected = {
        (ids["a.visit"], ids["a.visit"]), (ids["a"], ids["a.visit"]),
        (ids["b.visit"], ids["b.visit"]), (ids["b"], ids["b.visit"]),
    }
    assert expected <= calls
    assert (ids["b.visit"], ids["a.visit"]) not in calls
    assert (ids["b"], ids["a.visit"]) not in calls


def test_a_methods_default_binds_the_class_level_helper(tmp_path):
    """Resolved in the class body; attributed to `m` as today (refinement 11)."""
    source = (
        "def factory(): pass\n"
        "class K:\n"
        "    def factory(): return 1\n"
        "    def m(self, x=factory()):\n"
        "        return x\n"
    )
    result = _extract(tmp_path, {"m.py": source})
    ids, calls = _ids(result), _pairs(result)
    assert (ids["K.m"], ids["K.factory"]) in calls
    assert (ids["K.m"], ids["factory"]) not in calls
```

- [ ] **Step 2: Run the tests and watch them fail.**

```bash
$PY -m pytest tests/test_python_scope_identity.py -q > "$SCRATCH/t6.txt" 2>&1; echo "EXIT=$?"; grep -c "^FAILED" "$SCRATCH/t6.txt"; tail -3 "$SCRATCH/t6.txt"
```

Expected: `EXIT=1`. Every new test fails except `test_two_unresolved_imports_of_one_name_stay_external`, which is a guard. Most fail with `KeyError` (no `qualname` yet). The binder cases and `test_a_local_value_call_stays_in_the_calls_denominator` fail because today's edge binds to `m_py_fn` / `m_py_generate`.

- [ ] **Step 3: Implement.**

(a) Add the import below the existing `from ..resolve import ...` line at the top of `src/graphite/extract/ast.py`:

```python
from .pyscope import Resolution, ScopeTable, collect_scopes, resolve
```

(b) Add these helpers directly above `def _python_call_target(`:

```python
def _python_definition_name(node: Any) -> str | None:
    """A def/class node's name exactly as its graph node records it."""
    name_node = node.child_by_field_name("name")
    if name_node is None or not name_node.text:
        return None
    return _short_name(name_node.text.decode("utf-8", errors="ignore"))


def _python_def_id(file_id: str, qualname: str, module_level: bool) -> str:
    """Module-scope definitions keep `_make_id(file, name)`; every other is scope-qualified (#70)."""
    return _make_id(file_id, qualname) if module_level else _scoped_id(file_id, qualname)


def _python_name_confidence(name: str, resolution: Resolution) -> str:
    """`EXTERNAL_CALL` iff what `name` denotes provably leaves the repo (spec §4.2).

    A name bound in the file is external only when its binder is an import that
    did not resolve in-repo. A name bound nowhere in the file is Python's
    builtins scope, so `_EXTERNAL_GLOBALS` still classifies it. A local value
    named like a global (`for it in items: it.process()`) is NOT external: the
    binding is local, and a false external hides real code from the ratio and
    from dispatch's evidence gate.
    """
    if resolution.kind == "external":
        return "EXTERNAL_CALL"
    if resolution.kind == "none" and name in _EXTERNAL_GLOBALS:
        return "EXTERNAL_CALL"
    return "LOCAL_CALL"


def _python_bare_call(file_id: str, name: str, resolution: Resolution, table: ScopeTable) -> tuple[str, str]:
    """(target id, confidence) for a bare-name call, from what the name denotes there.

    A local value -- a parameter, an assignment, a module object, two binders --
    keeps an UNBOUND edge rather than none: dropping it would take the site out
    of the `calls` denominator, and a ratio cannot see a missing site. Its
    target is today's placeholder, unless that id is a module-scope definition
    in this file and would bind it; then it is a phantom no definition can own
    (`<` is not an identifier character).
    """
    confidence = _python_name_confidence(name, resolution)
    if resolution.kind == "def" and resolution.qualname is not None:
        return _python_def_id(file_id, resolution.qualname, resolution.module_level), confidence
    if resolution.kind == "symbol" and resolution.target is not None:
        return resolution.target, confidence
    if resolution.kind in ("value", "alias") and table.module_defines(name):
        return _scoped_id(file_id, f"<local>.{name}"), confidence
    return _resolve_call(file_id, name), confidence
```

(c) Replace the whole of `def _extract_python(...)`, from its `def` line through its `return result`, with:

```python
def _extract_python(file_id: str, rel_path: str, _source: bytes, tree: Any, source_index: SourceIndex | None = None) -> ExtractionResult:
    result = ExtractionResult()
    root = tree.root_node

    def _line(node: Any) -> int:
        return (node.start_point[0] + 1) if node.start_point else 1

    result.nodes.append(_node(file_id, "file", Path(rel_path).name, rel_path))

    # One pass builds every Python scope and what each binds (#70, #71). The walk
    # below reads it: each def/class gets its scope-qualified identity, and each
    # bare name resolves in the scope its call is EVALUATED in.
    table = collect_scopes(
        root,
        lambda statement: _python_import_bindings(statement, rel_path, source_index),
        _python_definition_name,
    )

    class_ids: set[str] = set()

    # ``parent_id`` is the nearest named container (for ``contains`` edges);
    # ``scope_id`` is the nearest enclosing function (for ``calls`` attribution).
    def walk(node: Any, parent_id: str | None, scope_id: str) -> None:
        if node.type in ("function_definition", "class_definition"):
            located = table.definitions.get(node.id)
            name = _python_definition_name(node)
            if located is None or name is None:
                walk_children(node, parent_id, scope_id)
                return
            qualname, module_level = located
            did = _python_def_id(file_id, qualname, module_level)
            if node.type == "function_definition":
                extra: dict[str, Any] = {"qualname": qualname}
                if parent_id in class_ids:
                    extra["is_method"] = True
                result.nodes.append(_node(did, "function", name, rel_path, _line(node), extra))
                if parent_id:
                    result.edges.append(_edge(parent_id, did, "contains", rel_path, _line(node)))
                walk_children(node, did, did)
                return
            class_ids.add(did)
            result.nodes.append(_node(did, "class", name, rel_path, _line(node), {"qualname": qualname}))
            if parent_id:
                result.edges.append(_edge(parent_id, did, "contains", rel_path, _line(node)))
            # Inheritance
            for base in node.children:
                if base.type == "argument_list":
                    for arg in base.children:
                        if arg.type.endswith("identifier") and arg.text:
                            base_name = arg.text.decode("utf-8", errors="ignore")
                            result.edges.append(_edge(did, _make_id(base_name), "inherits", rel_path, _line(arg)))
            walk_children(node, did, scope_id)
        elif node.type in ("import_statement", "import_from_statement"):
            for module, dots in _python_import_modules(node):
                resolved = (
                    source_index.resolve_python_module(rel_path, module, dots)
                    if source_index is not None
                    else None
                )
                if resolved:
                    result.edges.append(_edge(
                        file_id, _file_node_id(resolved), "imports", rel_path,
                        _line(node), confidence="EXACT_IMPORT",
                    ))
                    # `import pkg.sub` also imports `pkg` (#55). Deliberately NOT
                    # applied to `from pkg.sub import x`: that executes the package
                    # too, but binds only `x`, so there is no `pkg.` call to
                    # attribute -- and `test_from_import_symbol_only_edge_unchanged`
                    # pins one edge per module for that spelling.
                    for ancestor in (
                        _python_ancestor_packages(module, dots, rel_path, source_index)
                        if node.type == "import_statement" else ()
                    ):
                        result.edges.append(_edge(
                            file_id, _file_node_id(ancestor), "imports", rel_path,
                            _line(node), confidence="EXACT_IMPORT",
                        ))
                else:
                    result.edges.append(_edge(
                        file_id, _make_id(module) if module else _make_id("package"),
                        "imports", rel_path, _line(node), confidence="EXTERNAL_IMPORT",
                    ))
            for sub in _python_from_import_submodules(node, rel_path, source_index):
                result.edges.append(_edge(
                    file_id, _file_node_id(sub), "imports", rel_path,
                    _line(node), confidence="EXACT_IMPORT",
                ))
            walk_children(node, parent_id, scope_id)
        elif node.type == "call":
            # `_python_call_target` and `_edge` stay module-global lookups, and the
            # edge is made BEFORE the call's children are walked:
            # scripts/pyscopeoracle.py pairs each site with its edge on exactly that.
            func = node.child_by_field_name("function")
            bare, obj_name, attr = _python_call_target(func) if func is not None else (None, None, None)
            edge = None
            scope = table.scope_of_call(node)
            if bare and bare not in _LANGUAGE_BUILTIN_GLOBALS:
                resolution = resolve(scope, bare)
                target, confidence = _python_bare_call(file_id, bare, resolution, table)
                edge = _edge(scope_id, target, "calls", rel_path, _line(node), confidence=confidence)
            elif attr:
                dotted = f"{obj_name}.{attr}" if obj_name else attr
                recovered_root = obj_name or _python_attribute_root(func)
                root_binding = resolve(scope, recovered_root) if recovered_root else None
                if obj_name and root_binding is not None and root_binding.kind == "alias" and root_binding.target:
                    edge = _edge(
                        scope_id, _make_id(root_binding.target, attr), "calls", rel_path, _line(node),
                        confidence="LOCAL_CALL",
                    )
                elif should_keep_call_target(dotted):
                    # Unresolved member call: file-scoped phantom now, re-pointed
                    # (or dropped) by the method-dispatch post-pass via _member.
                    # Confidence is read off the receiver's ROOT as the scope walk
                    # resolved it (a depth->=2 chain like `os.path.join` tests `os`,
                    # not `join`). When no receiver root can be recovered,
                    # `dotted` is the bare attribute name and says nothing about
                    # where the call goes, so it is never classified external (#14).
                    edge = _edge(
                        scope_id, _resolve_call(file_id, dotted), "calls", rel_path, _line(node),
                        confidence=(
                            _python_name_confidence(recovered_root, root_binding)
                            if recovered_root is not None and root_binding is not None
                            else "LOCAL_CALL"
                        ),
                    )
                    edge["_member"] = attr
            if edge is not None:
                result.edges.append(edge)
            walk_children(node, parent_id, scope_id)
        else:
            walk_children(node, parent_id, scope_id)

    def walk_children(node: Any, parent_id: str | None, scope_id: str) -> None:
        for child in node.children:
            walk(child, parent_id, scope_id)

    walk(root, file_id, file_id)
    return result
```

(d) Delete `_collect_python_import_maps` entirely. First confirm through the graph that nothing else calls it:

```bash
python -m graphite query "callers _collect_python_import_maps" > "$SCRATCH/t6c.json" 2>&1; $PY -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); print(d.get('answer',{}).get('grade'), [c['name'] for c in d.get('callers',[])])" "$SCRATCH/t6c.json"
```

Expected: `decision_grade ['_extract_python']`. Two comments in `tests/test_python_resolver.py` name it: those in `test_aliased_dotted_import_binds_cross_module` and `test_plain_import_binds_cross_module`. Change `_collect_python_import_maps's import_statement branch` to `_python_import_bindings's import_statement branch` in both.

(e) In `_call_confidence`'s docstring, replace the paragraph that begins `This precedence check is threaded for BOTH TypeScript and Python (#14` and ends `` rather than colliding with `_EXTERNAL_GLOBALS`. `` with:

```
    This precedence check is threaded for TypeScript (#14 mechanism B). Python
    no longer comes here: `_python_name_confidence` reads the binder the scope
    walk found for the name, which is the same rule made per scope -- an in-repo
    binding is never external, an unresolved import is (#71).
```

- [ ] **Step 4: Run the new tests.**

```bash
$PY -m pytest tests/test_python_scope_identity.py tests/test_pyscope.py tests/test_python_import_bindings.py -q > "$SCRATCH/t6b.txt" 2>&1; echo "EXIT=$?"; tail -3 "$SCRATCH/t6b.txt"
```

Expected: `EXIT=0`.

- [ ] **Step 5: Update the tests that spell method ids, and those that would go vacuous.**

```bash
$PY -m pytest tests/test_dispatch_evidence.py tests/test_method_dispatch_scope.py tests/test_python_resolver.py tests/test_call_graph.py -q > "$SCRATCH/t6d.txt" 2>&1; echo "EXIT=$?"; grep "^FAILED" "$SCRATCH/t6d.txt"
```

Expected: `EXIT=1`. The FAILED lines are only POSITIVE assertions on a method id:
- `test_dispatch_evidence.py`: `test_a_local_receiver_still_dispatches_to_the_same_method_name`, `test_an_in_repo_binding_still_beats_an_external_name_collision`;
- `test_method_dispatch_scope.py`: `test_an_imported_classs_method_still_binds_across_files`, `test_a_self_call_still_binds_in_the_same_file`, `test_a_dotted_import_reaches_the_package_it_binds`, both `test_both_import_spellings_reach_their_target` cases;
- `test_python_resolver.py`: `test_self_call_binds_to_own_class_method`, `test_python_methods_tagged_top_level_functions_not`.

The six NEGATIVE assertions on method ids now pass for the wrong reason (refinement 12). The edits below fix both kinds.

**`tests/test_dispatch_evidence.py`.** Replace the helpers `_edges` and `_calls` with:

```python
def _result(tmp_path: Path):
    cfg = Config(
        workers=1,
        cache_dir=tmp_path / ".cache" / "graphite",
        typescript_resolver="disabled",
    )
    return extract_all(collect_files(tmp_path, cfg), cfg)


def _call_edges(result) -> list[dict]:
    return [e for e in result.edges if e["relation"] == "calls"]


def _pairs(result) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in _call_edges(result)}


def _edges(tmp_path: Path) -> list[dict]:
    return _call_edges(_result(tmp_path))


def _calls(tmp_path: Path) -> set[tuple[str, str]]:
    return _pairs(_result(tmp_path))


def _def_id(result, source_file: str, qualname: str) -> str:
    """A definition's id, LOOKED UP rather than spelled (#70).

    A spelled id in a negative assertion goes vacuous when ids change: the edge
    is absent because the id no longer exists, and the guard passes whatever
    dispatch does. A lookup raises instead. Non-Python nodes carry no
    `qualname`, so their `name` stands in.
    """
    for n in result.nodes:
        if n.get("source_file") == source_file and n.get("qualname", n.get("name")) == qualname:
            return n["id"]
    raise AssertionError(f"no definition {qualname!r} in {source_file}")
```

Then make these replacements, in order of appearance. Each `old` line is unique in the file:

- `    assert ("app_py_go", "cache_py_read") not in _calls(tmp_path)` becomes:

```python
    result = _result(tmp_path)
    go, read = _def_id(result, "app.py", "go"), _def_id(result, "cache.py", "Cache.read")
    assert (go, read) not in _pairs(result)
```

- The block from `    external = [` through `    assert all(e["target"] != "cache_py_read" for e in external)` becomes:

```python
    result = _result(tmp_path)
    go, read = _def_id(result, "app.py", "go"), _def_id(result, "cache.py", "Cache.read")
    external = [
        e for e in _call_edges(result)
        if e["source"] == go and e.get("confidence") == "EXTERNAL_CALL"
    ]
    assert external, "the os.read call lost its EXTERNAL_CALL edge entirely"
    assert all(e["target"] != read for e in external)
```

- `    assert ("app_py_go", "cache_py_read") in _calls(tmp_path)` becomes:

```python
    result = _result(tmp_path)
    assert (_def_id(result, "app.py", "go"), _def_id(result, "cache.py", "Cache.read")) in _pairs(result)
```

- `    assert ("app_py_go", "helpers_py_format") in _calls(tmp_path)` becomes:

```python
    result = _result(tmp_path)
    assert (_def_id(result, "app.py", "go"), _def_id(result, "helpers.py", "Formatter.format")) in _pairs(result)
```

- `    assert ("web_app_js_run", "api_worker_py_handle") not in _calls(tmp_path)` becomes:

```python
    result = _result(tmp_path)
    run, handle = _def_id(result, "web/app.js", "run"), _def_id(result, "api/worker.py", "Worker.handle")
    assert (run, handle) not in _pairs(result)
```

**`tests/test_method_dispatch_scope.py`.** Replace the helper `_calls` with:

```python
def _result(tmp_path: Path):
    cfg = Config(
        workers=1,
        cache_dir=tmp_path / ".cache" / "graphite",
        typescript_resolver="disabled",
    )
    return extract_all(collect_files(tmp_path, cfg), cfg)


def _pairs(result) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}


def _calls(tmp_path: Path) -> set[tuple[str, str]]:
    return _pairs(_result(tmp_path))


def _def_id(result, source_file: str, qualname: str) -> str:
    """A definition's id, LOOKED UP rather than spelled (#70).

    A spelled id in a negative assertion goes vacuous when ids change: the edge
    is absent because the id no longer exists, and the guard passes whatever
    dispatch does. A lookup raises instead. Non-Python nodes carry no
    `qualname`, so their `name` stands in.
    """
    for n in result.nodes:
        if n.get("source_file") == source_file and n.get("qualname", n.get("name")) == qualname:
            return n["id"]
    raise AssertionError(f"no definition {qualname!r} in {source_file}")
```

Then:

- `    assert ("src_prod_py_where", "tests_test_double_py_resolve") not in _calls(tmp_path)` appears TWICE. Replace both occurrences (Edit with `replace_all`) with:

```python
    result = _result(tmp_path)
    where = _def_id(result, "src/prod.py", "where")
    double = _def_id(result, "tests/test_double.py", "FakePath.resolve")
    assert (where, double) not in _pairs(result)
```

- `    assert ("src_prod_py_save", "tests_test_double_py_write") not in _calls(tmp_path)` becomes:

```python
    result = _result(tmp_path)
    save = _def_id(result, "src/prod.py", "save")
    double = _def_id(result, "tests/test_double.py", "FakePath.write")
    assert (save, double) not in _pairs(result)
```

- `    assert ("src_pipeline_py_run", "src_ledger_py_record_run") in _calls(tmp_path)` becomes:

```python
    result = _result(tmp_path)
    run = _def_id(result, "src/pipeline.py", "run")
    assert (run, _def_id(result, "src/ledger.py", "Ledger.record_run")) in _pairs(result)
```

- `    assert ("svc_py_run", "svc_py_helper") in _calls(tmp_path)` becomes:

```python
    result = _result(tmp_path)
    assert (_def_id(result, "svc.py", "Svc.run"), _def_id(result, "svc.py", "Svc.helper")) in _pairs(result)
```

- `    assert ("m_py_go", "pkg_init_py_56bf3f_build") in _calls(tmp_path)` becomes:

```python
    result = _result(tmp_path)
    assert (_def_id(result, "m.py", "go"), _def_id(result, "pkg/__init__.py", "Widget.build")) in _pairs(result)
```

- `    assert ("src_pkg_pipeline_py_run", "src_pkg_ledger_py_record_run") in _calls(tmp_path)` becomes:

```python
    result = _result(tmp_path)
    run = _def_id(result, "src/pkg/pipeline.py", "run")
    assert (run, _def_id(result, "src/pkg/ledger.py", "Ledger.record_run")) in _pairs(result)
```

**`tests/test_python_resolver.py`.** Add `from graphite.extract.ast import _scoped_id` below the file's existing imports, then:

- `    assert "svc_py_run" in [c["id"] for c in out.get("callers", [])]` becomes `    assert _scoped_id("svc_py", "Svc.run") in [c["id"] for c in out.get("callers", [])]`.
- `    assert by_id["src_pkg_ledger_py_record_run"].get("is_method") is True` becomes `    assert by_id[_scoped_id("src_pkg_ledger_py", "Ledger.record_run")].get("is_method") is True`.

**`tests/test_call_graph.py`** (the already-vacuous guard). In `test_python_calls_are_function_scoped`, replace

```python
    assert not any(src == "mod" for src, tgt in calls if tgt == "mod_py_helper")
```

with

```python
    # The file node's id is `mod_py` (#58 kept the extension); this guard read
    # `src == "mod"` and could not fail. Pin the id it compares against.
    assert {n["id"] for n in result.nodes if n["kind"] == "file"} == {"mod_py"}
    assert not any(src == "mod_py" for src, tgt in calls if tgt == "mod_py_helper")
```

Re-run:

```bash
$PY -m pytest tests/test_dispatch_evidence.py tests/test_method_dispatch_scope.py tests/test_python_resolver.py tests/test_call_graph.py -q > "$SCRATCH/t6e.txt" 2>&1; echo "EXIT=$?"; tail -3 "$SCRATCH/t6e.txt"
```

Expected: `EXIT=0`. Task 8 proves that each rewritten negative can fail.

- [ ] **Step 6: Flip the oracle control.** It is now the after-state, deliberately and in this same commit. In `tests/test_pyscopeoracle.py`, replace the whole function `test_todays_engine_mis_binds_the_three_planted_sites` with:

```python
def test_the_engine_binds_every_planted_site_correctly(oracle, tmp_path):
    """Was the CONTROL (Task 4: today's engine bound three of these wrongly).
    Flipped in the rewire commit, deliberately: the same fixture now reads zero."""
    _write(tmp_path / "m.py", SCOPE_FIXTURE)
    report = oracle.measure_sites(oracle.load(tmp_path))
    assert report["stats"] == {"sites": 4, "correct": 2, "correct-unbound": 2}
    assert report["wrong"] == 0
    assert report["selfcheck"]["call_nodes"] == report["selfcheck"]["distinct_call_ids"] == 4
    assert report["selfcheck"]["symtable_disagreements"] == 0
```

- [ ] **Step 7: Run the full suite, type-check and lint.**

```bash
$PY -m pytest -q -p no:cacheprovider > "$SCRATCH/t6-full.txt" 2>&1; echo "EXIT=$?"; tail -5 "$SCRATCH/t6-full.txt"
python -m mypy src/graphite/extract/ast.py src/graphite/extract/pyscope.py > "$SCRATCH/t6m.txt" 2>&1; echo "MYPY=$?"; tail -3 "$SCRATCH/t6m.txt"
python -m ruff check src/graphite/extract tests
```

Expected: `EXIT=0`, `MYPY=0`, `All checks passed!`. The full suite takes 13–15 minutes, so run it in the background and wait for it to finish.

Any failure outside the files above: read it.
- If it asserts a method or nested-definition id, update it by lookup the same way and name it in the commit message.
- If it is anything else, stop and report before going on.

- [ ] **Step 8: Commit, with the maintainer's go.**

```bash
git add src/graphite/extract/ast.py tests/test_python_scope_identity.py tests/test_pyscopeoracle.py tests/test_dispatch_evidence.py tests/test_method_dispatch_scope.py tests/test_python_resolver.py tests/test_call_graph.py
aramid check --staged
git commit -m "feat(extract): Python definitions get scope-qualified ids; bare names bind by Python's scoping rules (#70, #71)"
```

---
### Task 7: query surface — `qualname` matching, ranking, `alternates_total`, rows

**Files:**
- Modify: `src/graphite/query.py` (`_find_node_detail`, `_match_meta`, `_node_view`; add `NodeMatch` and `_scope_rank`)
- Modify: `src/graphite/context.py` (`build_context`'s matched-input loop)
- Modify: `docs/schemas/query-result.v1.schema.json`, `docs/schemas/search-result.v1.schema.json`
- Modify: `docs/agent-integration.md`
- Test: `tests/test_python_scope_identity.py` (append), `tests/test_published_schemas.py`

**Interfaces:**
- Consumes: from Task 6, the node attributes `qualname` and `is_method`.
- Produces: `NodeMatch(node: str, match_type: str, alternates: list[str], alternates_total: int)`, a `NamedTuple` returned by `_find_node_detail`. Index `[0]` is still the node id, so `detail[0]` callers keep working.

The graph says `_find_node_detail` has seven callers (`decision_grade`). Six use only `detail[0]` or pass the whole match to `_match_meta`. `context.build_context` unpacks it as a 3-tuple, and that line must change.

- [ ] **Step 1: Write the failing query tests.** Add these imports to `tests/test_python_scope_identity.py`:

```python
from graphite.context import build_context
from graphite.query import query, search_graph
```

and append:

```python
# --- query surface (Task 7) -----------------------------------------------------


def _qualname_graph():
    """Same-named definitions whose ids sort the WRONG way round on purpose: by id
    the nested one comes first, so id order alone cannot pass the ranking test."""
    nodes = [
        {"id": "m_py", "kind": "file", "name": "m.py", "source_file": "m.py"},
        {"id": "m_py_caller", "kind": "function", "name": "caller", "qualname": "caller", "source_file": "m.py"},
        {"id": "m_py_a_outer_run_1", "kind": "function", "name": "run", "qualname": "outer.run", "source_file": "m.py"},
        {"id": "m_py_b_worker_run_2", "kind": "function", "name": "run", "qualname": "Worker.run",
         "is_method": True, "source_file": "m.py"},
        {"id": "m_py_run", "kind": "function", "name": "run", "qualname": "run", "source_file": "m.py"},
        {"id": "pkg_n_py_worker_run_3", "kind": "function", "name": "run", "qualname": "Worker.run",
         "is_method": True, "source_file": "pkg/n.py"},
    ]
    edges = [{"source": "m_py_caller", "target": "m_py_b_worker_run_2", "relation": "calls"}]
    return build_graph(nodes, edges)


def test_a_bare_name_means_the_module_level_definition():
    (resolution,) = query(_qualname_graph(), "callers run")["resolution"]
    assert (resolution["type"], resolution["node"]) == ("name", "m_py_run")
    # equal path depth: method before nested definition; the deeper file last
    assert resolution["alternates"] == ["m_py_b_worker_run_2", "m_py_a_outer_run_1", "pkg_n_py_worker_run_3"]
    assert resolution["alternates_total"] == 3


def test_a_dotted_qualname_selects_that_definition():
    out = query(_qualname_graph(), "callers Worker.run")
    (resolution,) = out["resolution"]
    assert (resolution["type"], resolution["node"]) == ("qualname", "m_py_b_worker_run_2")
    assert resolution["alternates"] == ["pkg_n_py_worker_run_3"]
    assert resolution["alternates_total"] == 1
    assert [c["id"] for c in out["callers"]] == ["m_py_caller"]


def test_alternates_are_capped_and_alternates_total_is_not():
    """Review Focus 5: `test_daemon.py` alone will have six nested `fake_build`s."""
    nodes = [{"id": "m_py", "kind": "file", "name": "m.py", "source_file": "m.py"}] + [
        {"id": f"m_py_t{i}_fake_build", "kind": "function", "name": "fake_build",
         "qualname": f"t{i}.fake_build", "source_file": "m.py"}
        for i in range(6)
    ]
    g = build_graph(nodes, [])
    (resolution,) = query(g, "callers fake_build")["resolution"]
    assert len(resolution["alternates"]) == 3
    assert resolution["alternates_total"] == 5
    (exact,) = query(g, "callers t0.fake_build")["resolution"]
    assert (exact["type"], exact["node"]) == ("qualname", "m_py_t0_fake_build")
    assert "alternates" not in exact


def test_rows_carry_a_qualname_when_it_differs_from_the_name():
    g = _qualname_graph()
    (callee,) = query(g, "calls caller")["calls"]
    assert callee["qualname"] == "Worker.run"
    rows = {r["id"]: r for r in search_graph(g, "run")["results"]}
    assert rows["m_py_b_worker_run_2"]["qualname"] == "Worker.run"
    assert "qualname" not in rows["m_py_run"]


def test_context_reports_alternates_total():
    (entry,) = build_context(_qualname_graph(), ["run"])["matched"]
    assert entry["alternates_total"] == 3
```

- [ ] **Step 2: Give the schema conformance test a graph that can see the new fields.** In `tests/test_published_schemas.py`'s `_graph()`, add to `nodes`:

```python
        {"id": "src_svc_py", "kind": "file", "name": "svc.py", "source_file": "src/svc.py"},
        {"id": "src_svc_py_run", "kind": "function", "name": "run", "qualname": "run",
         "source_file": "src/svc.py"},
        {"id": "src_svc_py_worker_run_7c6b1a", "kind": "function", "name": "run",
         "qualname": "Worker.run", "is_method": True, "source_file": "src/svc.py"},
```

and to `edges`:

```python
        {"source": "src_app_main", "target": "src_svc_py_worker_run_7c6b1a", "relation": "calls"},
```

In `test_query_outputs_match_published_result_schema`, add `"callers run",` and `"callers worker.run",` to `samples`. In `test_search_outputs_match_published_schema`, change the loop's tuple to `("helper", "src/app.ts", "pairing accept", "zzqx", "   ", "run")`.

- [ ] **Step 3: Run both and read the results carefully.**

```bash
$PY -m pytest tests/test_python_scope_identity.py tests/test_published_schemas.py -q > "$SCRATCH/t7.txt" 2>&1; echo "EXIT=$?"; grep "^FAILED" "$SCRATCH/t7.txt"
```

Expected: `EXIT=1`. The five new query tests fail (`KeyError: 'alternates_total'` / `'qualname'`, or a wrong node). `test_published_schemas.py` still PASSES at this point, because nothing emits the new fields yet. It must turn red in Step 5; that is what proves it can see them.

- [ ] **Step 4: Implement.**

(a) In `src/graphite/query.py`, add `NamedTuple` to the `typing` import, and add above `_path_depth`:

```python
class NodeMatch(NamedTuple):
    """How a query token resolved to a node: `_find_node_detail`'s answer.

    `alternates` lists a few other nodes that matched equally well;
    `alternates_total` is how many there were in all, so a capped list cannot
    read as a complete one.
    """

    node: str
    match_type: str
    alternates: list[str]
    alternates_total: int


def _scope_rank(g: nx.DiGraph, node_id: str) -> int:
    """0 module scope (or no qualname: other languages, files), 1 method, 2 nested (#70).

    A bare name then means the module-level definition when one exists, and the
    choice among same-named definitions stops depending on id order.
    """
    data = g.nodes[node_id]
    if "." not in str(data.get("qualname", "")):
        return 0
    return 1 if data.get("is_method") else 2
```

(b) Replace `_find_node_detail` (its signature through its final `return None`) with:

```python
def _find_node_detail(g: nx.DiGraph, token: str) -> NodeMatch | None:
    """Match a node and report HOW it matched.

    Match types, in precedence order: "exact-id", "qualname" (a dotted Python
    qualified name such as `Worker.run`), "name", "path-suffix", "fuzzy".
    `alternates` lists other nodes that matched equally well, so a
    silently-wrong pick is visible to the caller; ties break deterministically
    instead of by insertion order.
    """
    token = token.strip().lower().strip("`")
    if token in g:
        return NodeMatch(token, "exact-id", [], 0)

    # A DOTTED token can name a nested definition exactly. An undotted one is
    # left to the name tier: a module-level qualname equals its name, and
    # matching it here would relabel every such match as `qualname`.
    if "." in token:
        qualname_hits = sorted(
            (n for n in g.nodes() if "." in (q := str(g.nodes[n].get("qualname", ""))) and q.lower() == token),
            key=lambda n: (_path_depth(g, n), n),
        )
        if qualname_hits:
            return NodeMatch(qualname_hits[0], "qualname", qualname_hits[1:4], len(qualname_hits) - 1)

    # Multiple files can share a basename (README.md at root and under
    # hooks/, policy/, etc.) -- prefer the shallowest path, since a bare
    # basename query with no path segments almost always means the
    # repo-root file, not whichever id happened to sort first alphabetically
    # (found via operation-firewall dogfooding, 2026-07-31: `README.md`
    # matched `hooks/README.md` over the root file purely because
    # "hooks_readme" < "readme" as strings). At equal depth a module-level
    # definition beats a method, which beats a nested definition (#70).
    name_hits = sorted(
        (n for n in g.nodes() if g.nodes[n].get("name", "").lower() == token),
        key=lambda n: (_path_depth(g, n), _scope_rank(g, n), n),
    )
    if name_hits:
        return NodeMatch(name_hits[0], "name", name_hits[1:4], len(name_hits) - 1)

    normalized = token.replace("\\", "/")
    path_hits = sorted(
        n
        for n in g.nodes()
        if (sf := g.nodes[n].get("source_file", "")) and sf.lower().replace("\\", "/").endswith(normalized)
    )
    if path_hits:
        # Prefer the file node itself over symbols defined in the file.
        file_hits = [n for n in path_hits if g.nodes[n].get("kind") == "file"]
        chosen = file_hits[0] if file_hits else path_hits[0]
        others = [n for n in path_hits if n != chosen]
        return NodeMatch(chosen, "path-suffix", others[:4], len(others))

    fuzzy_hits = sorted((n for n in g.nodes() if token in n), key=lambda n: (len(n), n))
    if fuzzy_hits:
        return NodeMatch(fuzzy_hits[0], "fuzzy", fuzzy_hits[1:4], len(fuzzy_hits) - 1)
    return None
```

(c) Replace `_match_meta` with:

```python
def _match_meta(token: str, detail: NodeMatch) -> dict[str, Any]:
    """Query-response metadata describing how an input token was matched."""
    meta: dict[str, Any] = {"input": token, "node": detail.node, "type": detail.match_type}
    if detail.alternates:
        meta["alternates"] = detail.alternates
        meta["alternates_total"] = detail.alternates_total
    return meta
```

(d) Replace `_node_view` with:

```python
def _node_view(g: nx.DiGraph, n: str) -> dict[str, Any]:
    """Compact node descriptor for call-graph results.

    `qualname` appears only where it says more than `name`: a Python method or
    nested definition (`Worker.run`), never a module-level one.
    """
    data = g.nodes[n]
    view = {
        "id": n,
        "name": data.get("name", n),
        "kind": data.get("kind", "unknown"),
        "source_file": data.get("source_file", ""),
    }
    qualname = data.get("qualname")
    if qualname and qualname != view["name"]:
        view["qualname"] = qualname
    return view
```

(e) In `src/graphite/context.py`, replace

```python
        detail = _find_node_detail(g, item)
        if detail:
            node, match_type, alternates = detail
            start_nodes.append(node)
            entry: dict[str, Any] = {"input": item, "node": _node_summary(g, node), "match_type": match_type}
            if alternates:
                entry["alternates"] = alternates
            matched.append(entry)
```

with

```python
        detail = _find_node_detail(g, item)
        if detail:
            start_nodes.append(detail.node)
            entry: dict[str, Any] = {
                "input": item,
                "node": _node_summary(g, detail.node),
                "match_type": detail.match_type,
            }
            if detail.alternates:
                entry["alternates"] = detail.alternates
                entry["alternates_total"] = detail.alternates_total
            matched.append(entry)
```

- [ ] **Step 5: Run again. The schema tests must now FAIL.**

```bash
$PY -m pytest tests/test_python_scope_identity.py tests/test_published_schemas.py -q > "$SCRATCH/t7b.txt" 2>&1; echo "EXIT=$?"; grep "^FAILED" "$SCRATCH/t7b.txt"
```

Expected: `EXIT=1`, with exactly `test_query_outputs_match_published_result_schema` and `test_search_outputs_match_published_schema` failing. `resolution[]` items and search rows are closed objects, and the output now carries `alternates_total` and `qualname`. If those two pass here, the conformance graph cannot see the new fields: fix Step 2 before going on.

- [ ] **Step 6: Declare the fields.**

In `docs/schemas/query-result.v1.schema.json`, `properties.resolution.items.properties`, after `"alternates": {"type": "array", "items": {"type": "string"}}`, add:

```json
,
          "alternates_total": {"type": "integer", "description": "How many other nodes matched as well as `node`. `alternates` lists at most three (four for `path-suffix`), so compare the two before treating the list as complete."}
```

In `docs/schemas/search-result.v1.schema.json`, `properties.results.items.properties`, after `"score": {"type": "number"}`, add:

```json
,
          "qualname": {"type": "string", "description": "A Python method's or nested definition's qualified name (`Worker.run`); present only when it differs from `name`."}
```

Keep each file valid JSON: put the comma after the preceding property, not on a line of its own, and indent to match.

- [ ] **Step 7: Document the surface.** In `docs/agent-integration.md`, replace

```
paths, and concept tokens. `match_type` explains each hit (`exact-id`, `name`,
`path-suffix`, `id-substring`, `name-substring`, `tokens`); results are bounded
(`--limit`, default 20, max 100) with `truncated`/`total_matches` reported.
```

with

```
paths, and concept tokens. `match_type` explains each hit (`exact-id`, `name`,
`path-suffix`, `id-substring`, `name-substring`, `tokens`); results are bounded
(`--limit`, default 20, max 100) with `truncated`/`total_matches` reported.
A row for a Python method or nested definition carries `qualname`
(`Worker.run`, `test_one.fake_build`) when it differs from `name`.
```

and replace

```
- On success, `resolution` lists how every input resolved
  (`role`/`input`/`node`/`type`, plus `alternates` when a token was
  ambiguous). Check it before trusting results — a `fuzzy` resolution with
  alternates may mean the wrong node was picked.
```

with

```
- On success, `resolution` lists how every input resolved
  (`role`/`input`/`node`/`type`, plus `alternates` and `alternates_total`
  when a token was ambiguous). `type` is `exact-id`, `qualname` (a dotted
  Python qualified name such as `Worker.run`), `name`, `path-suffix` or
  `fuzzy`. Among same-named definitions at equal path depth a bare name picks
  the module-level one, then a method, then a nested definition; name a nested
  one by its qualname. `alternates` lists at most three (four for
  `path-suffix`); `alternates_total` says how many there were. Check it before
  trusting results — a `fuzzy` resolution with alternates may mean the wrong
  node was picked.
```

- [ ] **Step 8: Run, type-check, lint.**

```bash
$PY -m pytest tests/test_python_scope_identity.py tests/test_published_schemas.py tests/test_call_graph.py tests/test_go_rust.py tests/test_context.py tests/test_context_builder.py -q > "$SCRATCH/t7c.txt" 2>&1; echo "EXIT=$?"; tail -3 "$SCRATCH/t7c.txt"
python -m mypy src/graphite/query.py src/graphite/context.py > "$SCRATCH/t7m.txt" 2>&1; echo "MYPY=$?"; tail -3 "$SCRATCH/t7m.txt"
python -m ruff check src/graphite/query.py src/graphite/context.py tests
```

Expected: `EXIT=0`, `MYPY=0`, `All checks passed!`.

- [ ] **Step 9: Commit, with the maintainer's go.**

```bash
git add src/graphite/query.py src/graphite/context.py docs/schemas/query-result.v1.schema.json docs/schemas/search-result.v1.schema.json docs/agent-integration.md tests/test_python_scope_identity.py tests/test_published_schemas.py
aramid check --staged
git commit -m "feat(query): match a dotted qualname, rank module-level definitions first, report alternates_total (#70)"
```

---

### Task 8: mutation proofs

Every guard this phase relies on must be shown to fail when its code is broken, and to fail for ITS reason. Each mutant names the tests that must appear as FAILED. Only pytest exit 1 counts as a kill: exit 2 or more is an error, and a mutant that does not import proves nothing. The script restores every file byte-for-byte in `finally`. Do not run it while any gate (pre-commit or pre-push) is running, since it edits source the dev venv imports.

**Files:**
- Create (scratch, not committed): `$SCRATCH/mutants.py`

- [ ] **Step 1: Write the script.** Create `$SCRATCH/mutants.py`:

```python
"""Phase 1 mutation proofs (#70, #71). Each mutant must make its named tests FAIL.

Only pytest exit 1 is a kill. Files are restored byte-for-byte in `finally`.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(r"F:/Projects/graphite")
PY = r"F:/Projects/.venvs/graphite-dev/Scripts/python.exe"
SCOPE = "src/graphite/extract/pyscope.py"
AST = "src/graphite/extract/ast.py"
QUERY = "src/graphite/query.py"
UNIT = "tests/test_pyscope.py"
IDENT = "tests/test_python_scope_identity.py"
EVIDENCE_TESTS = "tests/test_dispatch_evidence.py"
SCOPE_TESTS = "tests/test_method_dispatch_scope.py"
BINDER = "test_every_local_binder_makes_a_bare_call_a_local_value"
THROUGH = "test_a_call_through_any_local_binding_is_an_unbound_local_value"
LOCAL_IMPORT = ["test_a_function_local_import_binds_only_in_that_function",
                "test_a_function_local_import_binds_only_in_its_function",
                "test_a_function_local_module_alias_binds_only_in_its_function"]

# (name, file, old, new, tests to run, tests that must FAIL)
MUTANTS = [
    ("parameters", SCOPE,
     "        for name in _parameter_names(params):\n            inner.bind(name, _VALUE)\n",
     "        for name in _parameter_names(params):\n            pass\n",
     [UNIT, IDENT], [f"{BINDER}[param]", f"{BINDER}[lambda]", f"{THROUGH}[param]", f"{THROUGH}[lambda]"]),
    ("assignment", SCOPE, 'elif kind == "assignment":', 'elif kind == "assignment-mutant":',
     [UNIT, IDENT], [f"{BINDER}[assign]", f"{BINDER}[annotation-only]", f"{BINDER}[unpack]", f"{THROUGH}[assign]"]),
    ("augmented assignment", SCOPE, 'elif kind == "augmented_assignment":',
     'elif kind == "augmented_assignment-mutant":', [UNIT, IDENT], [f"{BINDER}[augmented]", f"{THROUGH}[augmented]"]),
    ("walrus", SCOPE, 'elif kind == "named_expression":', 'elif kind == "named_expression-mutant":',
     [UNIT, IDENT], [f"{BINDER}[walrus]", "test_a_walrus_in_a_comprehension_binds_in_the_function", f"{THROUGH}[walrus]"]),
    ("for", SCOPE, 'elif kind == "for_statement":', 'elif kind == "for_statement-mutant":',
     [UNIT, IDENT], [f"{BINDER}[for]", f"{BINDER}[async-for]", f"{THROUGH}[for]"]),
    ("with / except", SCOPE, 'elif kind == "as_pattern" and', 'elif kind == "as_pattern-mutant" and',
     [UNIT, IDENT], [f"{BINDER}[with]", f"{BINDER}[async-with]", f"{BINDER}[except]", f"{THROUGH}[except]"]),
    ("del", SCOPE, 'elif kind == "delete_statement":', 'elif kind == "delete_statement-mutant":',
     [UNIT, IDENT], [f"{BINDER}[del]", f"{THROUGH}[del]"]),
    ("match captures", SCOPE, 'elif kind == "case_clause":', 'elif kind == "case_clause-mutant":',
     [UNIT, IDENT], [f"{BINDER}[match-capture]", f"{BINDER}[match-as]", f"{BINDER}[match-star]",
                     f"{THROUGH}[match-capture]"]),
    ("imports bind nothing", SCOPE,
     'if kind in ("import_statement", "import_from_statement"):\n            for local, import_kind',
     'if kind in ("import_statement-mutant",):\n            for local, import_kind',
     [UNIT, IDENT], LOCAL_IMPORT),
    ("imports file-wide", SCOPE,
     'scope.bind(local, Binder("import", import_kind=import_kind, target=target))',
     'module.bind(local, Binder("import", import_kind=import_kind, target=target))',
     [UNIT, IDENT], LOCAL_IMPORT),
    ("comprehension targets", SCOPE,
     '            for name in _target_names(child.child_by_field_name("left")):\n                inner.bind(name, _VALUE)\n',
     '            for name in _target_names(child.child_by_field_name("left")):\n                pass\n',
     [UNIT], ["test_a_comprehension_target_binds_only_inside_the_comprehension"]),
    ("first iterable evaluated inside", SCOPE,
     'visit(part, scope if first and field_name == "right" else inner)', 'visit(part, inner)',
     [UNIT], ["test_the_first_iterable_of_a_class_body_comprehension_sees_the_class_scope"]),
    ("definition names bind nothing", SCOPE,
     '        scope.bind(raw, Binder("def" if is_function else "class", qualname=qualname, module_level=module_level))\n',
     '        pass\n',
     [UNIT, IDENT], ["test_a_module_level_call_binds_the_module_def", "test_methods_and_nested_definitions_get_their_own_node"]),
    ("class scope visible from method bodies", SCOPE,
     'if (starting or current.kind != "class") and name in current.binders:', 'if name in current.binders:',
     [UNIT, IDENT], ["test_a_method_body_does_not_see_its_class_scope", "test_scope_rules_reach_the_graph"]),
    ("global ignored at lookup", SCOPE,
     "    if name in scope.globals:\n        module = _module_of(scope)",
     "    if False:\n        module = _module_of(scope)",
     [UNIT, IDENT], ["test_global_redirects_to_the_module_binding", "test_scope_rules_reach_the_graph"]),
    ("global binders not relocated", SCOPE,
     "        for name in scope.globals:\n            moved", "        for name in ():\n            moved",
     [UNIT], ["test_an_assignment_under_global_makes_the_module_binding_ambiguous"]),
    ("nonlocal binders not relocated", SCOPE,
     "        for name in scope.nonlocals:\n            moved", "        for name in ():\n            moved",
     [UNIT], ["test_nonlocal_rebinding_makes_the_enclosing_binding_ambiguous"]),
    ("local-value calls dropped", AST,
     '                edge = _edge(scope_id, target, "calls", rel_path, _line(node), confidence=confidence)\n',
     '                edge = None if resolution.kind in ("value", "alias") else _edge('
     'scope_id, target, "calls", rel_path, _line(node), confidence=confidence)\n',
     [IDENT], [f"{THROUGH}[param]", "test_a_local_value_call_stays_in_the_calls_denominator"]),
    ("discriminator made conditional", AST,
     "    return f\"{readable[: _MAX_ID_LEN - len(marker) - 1].rstrip('_')}_{marker}\"\n",
     "    return _make_id(file_id, qualname)\n",
     [IDENT], ["test_a_nested_outer_inner_and_a_module_outer_inner_are_two_nodes"]),
    ("value-bound root classified against the globals", AST,
     'if resolution.kind == "none" and name in _EXTERNAL_GLOBALS:',
     'if resolution.kind in ("none", "value") and name in _EXTERNAL_GLOBALS:',
     [IDENT], ["test_a_local_value_named_like_a_global_is_a_local_receiver"]),
    ("evidence gate off", AST,
     '        if e.get("confidence") == "EXTERNAL_CALL":\n            candidates: set[str] | None = None',
     '        if False:\n            candidates: set[str] | None = None',
     [EVIDENCE_TESTS], ["test_a_stdlib_call_is_not_re_pointed_to_an_imported_definition",
                        "test_the_external_edge_is_kept_rather_than_dropped"]),
    ("family gate off", AST,
     'caller_family = _interop_family(e.get("source_file"))', "caller_family = None",
     [EVIDENCE_TESTS], ["test_a_javascript_call_does_not_dispatch_to_a_python_method"]),
    ("reachability gate off", AST,
     '        if candidates and _dispatch_is_gated(e.get("source_file")):', "        if False:",
     [SCOPE_TESTS], ["test_a_stdlib_call_does_not_bind_to_an_unimported_double",
                     "test_a_local_receiver_does_not_bind_to_an_unimported_double",
                     "test_a_file_handle_write_does_not_bind_to_an_unimported_double"]),
    ("qualname tier removed", QUERY, "        qualname_hits = sorted(", "        qualname_hits = [] and sorted(",
     [IDENT], ["test_a_dotted_qualname_selects_that_definition"]),
    ("scope rank removed", QUERY,
     "key=lambda n: (_path_depth(g, n), _scope_rank(g, n), n),", "key=lambda n: (_path_depth(g, n), n),",
     [IDENT], ["test_a_bare_name_means_the_module_level_definition"]),
]


def run(tests: list[str]) -> tuple[int, str]:
    done = subprocess.run(
        [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return done.returncode, done.stdout + done.stderr


def main() -> int:
    problems: list[str] = []
    controls: dict[tuple[str, ...], int] = {}
    for name, rel, old, new, tests, must_fail in MUTANTS:
        path = REPO / rel
        original = path.read_bytes()
        found = original.count(old.encode("utf-8"))
        if found != 1:
            problems.append(f"{name}: anchor found {found} times in {rel}")
            print(f"BAD      {name}: anchor found {found} times")
            continue
        key = tuple(tests)
        if key not in controls:
            controls[key] = run(tests)[0]
        if controls[key] != 0:
            problems.append(f"{name}: control run exit {controls[key]} before mutating")
            print(f"BAD      {name}: control exit {controls[key]}")
            continue
        try:
            path.write_bytes(original.replace(old.encode("utf-8"), new.encode("utf-8")))
            code, text = run(tests)
        finally:
            path.write_bytes(original)
        failed = [line for line in text.splitlines() if line.startswith("FAILED ")]
        missing = [t for t in must_fail if not any(re.search(rf"::{re.escape(t)}( |$)", line) for line in failed)]
        killed = code == 1 and not missing
        print(f"{'KILLED' if killed else 'SURVIVED':8} {name}: exit {code}, {len(failed)} failed"
              + (f"; NOT failing: {missing}" if missing else ""))
        if not killed:
            problems.append(name)
    print("ALL KILLED" if not problems else f"PROBLEMS: {problems}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it (about 30–45 minutes), in the background.**

```bash
$PY "$SCRATCH/mutants.py" > "$SCRATCH/mutants.txt" 2>&1; echo "EXIT=$?" >> "$SCRATCH/mutants.txt"
```

Then check:

```bash
tail -30 "$SCRATCH/mutants.txt"; git status --short
```

Expected: 25 `KILLED` lines, `ALL KILLED`, `EXIT=0`, and an empty `git status --short`. An empty status proves every file was restored.

- `BAD`: an anchor that is not unique, or a control that is not green. Fix the anchor in the script, never the source, and re-run that mutant.
- `SURVIVED`: a guard that cannot fail. Strengthen its test (TDD: the strengthened test must fail under the mutant), then commit with the maintainer's go and re-run.

- [ ] **Step 3: Keep the evidence.**

```bash
cp "$SCRATCH/mutants.py" "$SCRATCH/mutants.txt" "$EVIDENCE/"
```

---
### Task 9: measurements — before (the 1.1.1 wheel) and after (this engine)

Spec §5 makes these release-gating, and RELEASING.md requires the version number to be MEASURED. Nothing is committed in this task; everything lands in `$EVIDENCE`. "Before" is the deployed 1.1.1 wheel under the machine python, which runs exactly today's extractor (the diff in Task 4). "After" is the dev venv.

**Files:**
- Create (scratch): `$SCRATCH/graph_stats.py`, `$SCRATCH/id_survival.py`, `$SCRATCH/impact_sample.py`
- Output: `$EVIDENCE/*.json` and `$EVIDENCE/notes.md`

- [ ] **Step 1: Prove which graphite each interpreter runs.**

```bash
WHEEL=/c/Python314/python.exe
$WHEEL -P -c "import graphite, graphite.extract.ast as A; print(graphite.__version__, A.__file__)"
$PY -P -c "import graphite, graphite.extract.ast as A; print(graphite.__version__, A.__file__)"
```

Expected: the first line ends in a `site-packages` path, NOT `F:\Projects\graphite\src`. The second line is the dev tree. If the wheel line points at the tree, stop: the "before" arm would measure the new engine.

- [ ] **Step 2: Site scores and denotation dumps, before and after, on both corpora.**

```bash
G="$SCRATCH/corpus/graphite-f9e7014"; D="$SCRATCH/corpus/django-5.2.7"
for arm in before after; do
  if [ $arm = before ]; then RUN=$WHEEL; else RUN=$PY; fi
  $RUN -P scripts/pyscopeoracle.py sites "$G" --out "$EVIDENCE/sites-graphite-$arm.json" > /dev/null 2>&1; echo "sites graphite $arm EXIT=$?"
  $RUN -P scripts/pyscopeoracle.py sites "$D" --only django/ --out "$EVIDENCE/sites-django-$arm.json" > /dev/null 2>&1; echo "sites django $arm EXIT=$?"
  $RUN -P scripts/pyscopeoracle.py dump "$G" --out "$EVIDENCE/dump-graphite-$arm.json" > /dev/null 2>&1; echo "dump graphite $arm EXIT=$?"
  $RUN -P scripts/pyscopeoracle.py dump "$D" --only django/ --out "$EVIDENCE/dump-django-$arm.json" > /dev/null 2>&1; echo "dump django $arm EXIT=$?"
done
for c in graphite django; do
  $PY -P scripts/pyscopeoracle.py compare "$EVIDENCE/dump-$c-before.json" "$EVIDENCE/dump-$c-after.json" --out "$EVIDENCE/compare-$c.json"
done
```

Expected: every `EXIT=0`. Each report's `engine.extractor` names the interpreter's graphite: site-packages for `before`, the dev tree for `after`.

- [ ] **Step 3: Check the pre-registered acceptance (spec §5).** Write each check's result into `$EVIDENCE/notes.md`:

```bash
$PY - "$EVIDENCE" <<'EOF'
import json, sys
from pathlib import Path
ev = Path(sys.argv[1])
load = lambda name: json.loads((ev / name).read_text(encoding="utf-8"))
for c in ("graphite", "django"):
    before, after, control = load(f"sites-{c}-before.json"), load(f"sites-{c}-after.json"), load(f"sites-{c}-control.json")
    print(c, "same oracle (control, before, after):", len({r["engine"]["oracle_sha256"] for r in (control, before, after)}) == 1)
    print(c, "wheel == Task 4 control:", (before["stats"], before["examples"]) == (control["stats"], control["examples"]))
    print(c, "wrong before -> after:", before["wrong"], "->", after["wrong"])
    print(c, "unbound-but-def after:", after["stats"].get("unbound-but-def", 0))
    print(c, "selfcheck after:", after["selfcheck"])
    print(c, "compare:", load(f"compare-{c}.json")["stats"])
EOF
```

Required:

1. **`same oracle: True` and `wheel == Task 4 control: True` on both.** The wheel and the pre-change dev tree run the same extractor, so with the same oracle any difference means the "before" arm is not what it claims. If `same oracle` is False, the oracle changed after Task 4. Then the control comparison is void: say so in `notes.md`. The wheel arm, run by the current oracle, is the only valid "before".
2. **`wrong ... -> 0` on both.** If any `WRONG-*` site remains, read it at its source line. Record it in `notes.md`, quoting the line, as either an engine defect (stop: fix with a failing test first) or an oracle truth-model limit (named against the oracle docstring's CANNOT-see list). An unexplained WRONG site blocks release.
3. **`unbound-but-def` (not the reexport variant) after: every site explained the same way.** These are recall losses against CPython.
4. **`compare`: `only-old == only-new == 0`.** Both engines must emit the same call sites. For every other category, put the count in `notes.md` beside spec §2's prediction:
   - `bare-target-changed` against 118 (graphite) and 92 (Django);
   - `caller-reattributed` against 1,345 and 6,614;
   - `confidence-EXTERNAL_CALL->LOCAL_CALL` and `confidence-flip-then-dispatched` (refinement 2), and `dispatch-changed` (§4.3), all pre-registered with no predicted figure.

   Explain each gap, and read at least five `examples` of every category. `alias-target-changed`, `confidence-LOCAL_CALL->EXTERNAL_CALL` or any category not named here is a finding: stop and explain it before Task 10.
5. **Dispatch (§4.3):** from each dump's `dispatch` block, record `member:repointed`, `member:fanout`, `member:kept`, `member:dropped` and `member:over-cap(ungated)` before and after.

- [ ] **Step 4: Build both graphs with each engine.** This writes `graph-out/` inside the scratch corpora only.

```bash
for c in graphite-f9e7014 django-5.2.7; do
  $WHEEL -P -m graphite build "$SCRATCH/corpus/$c" > "$SCRATCH/build-$c-before.txt" 2>&1; echo "$c before EXIT=$?"
  cp "$SCRATCH/corpus/$c/graph-out/graph.json" "$EVIDENCE/graph-$c-before.json"
  $PY -P -m graphite build "$SCRATCH/corpus/$c" > "$SCRATCH/build-$c-after.txt" 2>&1; echo "$c after EXIT=$?"
  cp "$SCRATCH/corpus/$c/graph-out/graph.json" "$EVIDENCE/graph-$c-after.json"
done
```

Expected: four `EXIT=0`. The extraction cache partitions on engine identity, so the second build re-extracts rather than reusing the first build's output.

- [ ] **Step 5: Node, edge and health figures.** Create `$SCRATCH/graph_stats.py`:

```python
"""Node, edge and Python-calls health figures for one graph.json (phase 1, Task 9)."""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

graph, python = sys.argv[1], sys.argv[2]
data = json.loads(Path(graph).read_text(encoding="utf-8"))
stats = json.loads(subprocess.run(
    [python, "-P", "-m", "graphite", "query", "stats", "--graph-json", graph],
    capture_output=True, text=True, encoding="utf-8", check=True,
).stdout)
health = stats["resolution_health"]
print(json.dumps({
    "engine": data.get("metadata", {}).get("engine"),
    "nodes": len(data["nodes"]),
    "edges": len(data["edges"]),
    "nodes_by_kind": dict(Counter(n.get("kind") for n in data["nodes"])),
    "edges_by_relation": dict(Counter(e.get("relation") for e in data["edges"])),
    "nodes_with_qualname": sum(1 for n in data["nodes"] if "qualname" in n),
    "python_calls": health["by_language"].get("python", {}).get("calls"),
    "placeholder_nodes": health["placeholder_nodes"],
}, indent=1, sort_keys=True))
```

Create `$SCRATCH/id_survival.py` (spec §2's table, judged by what each id DENOTES):

```python
"""Python def/class ids between two graph.json files, judged by denotation (spec §2)."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def defs(path: str) -> dict[str, tuple[str, str, str]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        n["id"]: (n["source_file"], str(n.get("source_location")), n["kind"])
        for n in data["nodes"]
        if n.get("kind") in ("function", "class") and str(n.get("source_file", "")).endswith(".py")
    }


old, new = defs(sys.argv[1]), defs(sys.argv[2])
print(json.dumps({
    "def/class ids before": len(old),
    "same id, same definition": sum(1 for i, d in old.items() if new.get(i) == d),
    "id string removed": sum(1 for i in old if i not in new),
    "same string, different definition": sum(1 for i, d in old.items() if i in new and new[i] != d),
    "new ids": sum(1 for i in new if i not in old),
}, indent=1))
```

Run:

```bash
for c in graphite-f9e7014 django-5.2.7; do
  for arm in before after; do
    $PY "$SCRATCH/graph_stats.py" "$EVIDENCE/graph-$c-$arm.json" "$PY" > "$EVIDENCE/stats-$c-$arm.json"; echo "$c $arm EXIT=$?"
  done
  $PY "$SCRATCH/id_survival.py" "$EVIDENCE/graph-$c-before.json" "$EVIDENCE/graph-$c-after.json" > "$EVIDENCE/ids-$c.json"; echo "$c ids EXIT=$?"
done
```

Record in `notes.md`:
- the `python_calls` cell (total, bound, ratio) before and after, with the direction and its causes;
- `placeholder_nodes.share` before and after;
- the id-survival table beside spec §2's (graphite 4,473 / 915 / **0** / 1,298; Django 3,082 / 6,068 / **5** / 8,111).

Spec §4.2 deliberately pre-claims no direction for the calls ratio. Any "same string, different definition" count above spec §2's figure must be read row by row.

- [ ] **Step 6: Impact sizes for a fixed file sample.** This is spec §6, aramid's "graphite blast radius". Create `$SCRATCH/impact_sample.py`:

```python
"""`graphite impact` result sizes before and after, over a fixed file sample (spec §6)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

python, root, before, after, prefix, step = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], int(sys.argv[6])
files = sorted(p.relative_to(root).as_posix() for p in (root / prefix).rglob("*.py"))[::step]


def size(graph: str, rel: str) -> int | None:
    done = subprocess.run(
        [python, "-P", "-m", "graphite", "impact", "--graph-json", graph, "--json", rel],
        cwd=root, capture_output=True, text=True, encoding="utf-8",
    )
    if done.returncode != 0:
        return None
    return len(json.loads(done.stdout).get("impacted_files", []))


rows = [(rel, size(before, rel), size(after, rel)) for rel in files]
deltas = [b - a for _rel, a, b in rows if a is not None and b is not None]
print(json.dumps({
    "files": len(rows),
    "errors": sum(1 for _rel, a, b in rows if a is None or b is None),
    "unchanged": sum(1 for d in deltas if d == 0),
    "grew": sum(1 for d in deltas if d > 0),
    "shrank": sum(1 for d in deltas if d < 0),
    "max_growth": max(deltas, default=0),
    "max_shrink": min(deltas, default=0),
    "rows": rows,
}, indent=1))
```

Run it: every file under `src/graphite` for graphite, and every 20th file under `django/` for Django:

```bash
$PY "$SCRATCH/impact_sample.py" "$PY" "$SCRATCH/corpus/graphite-f9e7014" "$EVIDENCE/graph-graphite-f9e7014-before.json" "$EVIDENCE/graph-graphite-f9e7014-after.json" src/graphite 1 > "$EVIDENCE/impact-graphite.json"; echo "EXIT=$?"
$PY "$SCRATCH/impact_sample.py" "$PY" "$SCRATCH/corpus/django-5.2.7" "$EVIDENCE/graph-django-5.2.7-before.json" "$EVIDENCE/graph-django-5.2.7-after.json" django 20 > "$EVIDENCE/impact-django.json"; echo "EXIT=$?"
```

Expected: `EXIT=0` and `errors: 0`. Record the distribution (`unchanged` / `grew` / `shrank` / `max_*`) in `notes.md`; it goes into the release notes.

- [ ] **Step 7: Close the measurement.** `notes.md` now holds a measured answer for every item in Steps 3, 5 and 6. Report the headline figures to the maintainer, and stop if any required check failed. Nothing is committed in this task.

---

### Task 10: retire the two caveats, and document the change

**Files:**
- Modify: `src/graphite/answer_contract.py` (the two registry entries)
- Modify: `tests/test_answer_contract.py`
- Modify: `CHANGELOG.md`, `docs/knowledge-base.md`

**Interfaces:**
- Consumes: Task 9's `$EVIDENCE/notes.md` figures.
- Produces: `active_caveats()` no longer returns `python-nested-name-shared-id` or `python-bare-call-ignores-scope`, and `CAVEAT_REGISTRY` keeps both with `retired_by`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_answer_contract.py`, replace the whole function `test_the_call_binding_blindspots_are_declared_on_every_python_calls_answer` with:

```python
def test_the_call_binding_blindspots_retired_with_scope_identity():
    """#70 and #71 were declared 2026-10-04 and retire with the phase 1 engine,
    which measured zero wrong-target bare-name calls on graphite and Django
    (scripts/pyscopeoracle.py). Retired, never deleted: a consumer that
    recorded either code keeps the meaning it was published with."""
    by_code = {e["code"]: e for e in CAVEAT_REGISTRY}
    active = {e["code"] for e in active_caveats()}
    for code in SCOPE_BINDING_CODES:
        entry = by_code[code]
        assert entry["since"] == "2026-10-04"
        assert date.fromisoformat(entry["retired_by"]) >= date(2026, 10, 5)
        assert code not in active

    g = _graph_ratio(".py", 10, 0)
    for total in (0, 3):
        block = build_answer_block(g, relations=("calls",), languages=["python"], total=total)
        assert not SCOPE_BINDING_CODES & {c["code"] for c in block["caveats"]}
```

Add `from datetime import date` to the file's imports. If `CAVEAT_REGISTRY` is not already imported there, add it to the existing `graphite.answer_contract` import.

In `test_registry_initial_entries`, delete these lines from the expected set:

```python
        # #70 and #71, declared 2026-10-04; both retire with the phase 1
        # scope-identity release.
        "python-nested-name-shared-id",
        "python-bare-call-ignores-scope",
```

- [ ] **Step 2: Run the tests and watch them fail.**

```bash
$PY -m pytest tests/test_answer_contract.py -q > "$SCRATCH/t10.txt" 2>&1; echo "EXIT=$?"; grep "^FAILED" "$SCRATCH/t10.txt"
```

Expected: `EXIT=1`, with `test_the_call_binding_blindspots_retired_with_scope_identity` (`KeyError: 'retired_by'`) and `test_registry_initial_entries` failing.

- [ ] **Step 3: Retire the entries.** In `src/graphite/answer_contract.py`, add a `"retired_by"` key to each of the two entries, directly after its `"since": "2026-10-04",` line. The value is the UTC date on which you make this commit, written as a string literal (get it from `date -u +%F`). Then add one comment line under each entry's existing comment:

```python
        # RETIRED by the phase 1 scope-identity engine: measured 0 wrong-target
        # bare-name calls on graphite and on Django 5.2.7 (scripts/pyscopeoracle.py).
```

- [ ] **Step 4: Run the tests.**

```bash
$PY -m pytest tests/test_answer_contract.py tests/test_published_schemas.py -q > "$SCRATCH/t10b.txt" 2>&1; echo "EXIT=$?"; tail -3 "$SCRATCH/t10b.txt"
```

Expected: `EXIT=0`.

- [ ] **Step 5: CHANGELOG.** In `CHANGELOG.md` under `## [Unreleased]`, add to `### Fixed` (above the existing `graphite init` entry):

```markdown
**Python methods and nested definitions get their own node, and bare-name
calls bind the way Python resolves them (#70, #71).**

- Every `def` and `class` that is not at module scope now has a scope-qualified
  id, built from its qualified name (`Worker.run`, `test_one.fake_build`), so
  same-named methods and nested helpers stop sharing one node.
- Module-scope ids are unchanged.
- Every Python definition node carries `qualname`.
- A bare call now resolves in the scope it is made in: parameters,
  assignments, loop and `with` targets, `except` and `match` captures,
  comprehension targets, function-local imports, `global` and `nonlocal`.
  Enclosing class scopes are skipped from inside a method.
- A call through a local name is kept as an unbound edge rather than bound to
  a same-named definition.

Measured with `scripts/pyscopeoracle.py` against CPython's `symtable`:
- wrong-target bare-name calls went from A to 0 on graphite and from B to 0 on
  Django 5.2.7;
- C call sites on graphite (D on Django) were credited to a different caller
  before this change.

Python node ids changed: E of F definition ids survive with the same meaning
on graphite, G of H on Django, and I id strings now name a different
definition. Do not persist node ids across this upgrade. The Python `calls`
ratio moved from J to K on graphite, because L. The retired caveats
`python-nested-name-shared-id` and `python-bare-call-ignores-scope` stay in the
registry, marked retired.
```

Fill A to L, and nothing else, from these named fields in `$EVIDENCE`:

| Placeholder | Source |
|---|---|
| A, B | `wrong` in `sites-<corpus>-before.json` |
| C, D | `caller-reattributed` in `compare-<corpus>.json` |
| E, F | `same id, same definition` and `def/class ids before` in `ids-graphite-f9e7014.json` |
| G, H | the same two fields in `ids-django-5.2.7.json` |
| I | `same string, different definition`, both corpora, as "N on graphite, M on Django" |
| J, K | `python_calls.ratio` in `stats-graphite-f9e7014-before.json` and `-after.json` |
| L | the cause recorded in `notes.md` |

Then, in the existing `### Added` entry about the two blind spots, replace its last sentence

```
Both retire when scope-qualified Python ids ship
(`docs/superpowers/specs/2026-10-04-python-scope-identity-design.md`).
```

with

```
Both are retired in this same release by the fix under "Fixed".
```

- [ ] **Step 6: Knowledge base.** In `docs/knowledge-base.md`, replace

```
**A node id from an older graph no longer resolves.** → Node ids are stable
only within an engine version; 0.5.0 deliberately changed id construction
(`index.ts` and `index.js` were one node before). → Never persist node ids
across upgrades; re-read them from the new graph, or key on paths and
symbol names.
```

with

```
**A node id from an older graph no longer resolves.** → Node ids are stable
only within an engine version; 0.5.0 deliberately changed id construction
(`index.ts` and `index.js` were one node before), and the Python
scope-identity release changed it again: a Python method or nested
definition's id is now `<file>_<qualname>_<hex>` (`m_py_worker_run_7c6b1a`),
while module-level ids are unchanged. → Never persist node ids across
upgrades; re-read them from the new graph, or key on paths and qualified
names (`graphite query "callers Worker.run"` resolves a qualname).
```

- [ ] **Step 7: Commit, with the maintainer's go.**

```bash
git add src/graphite/answer_contract.py tests/test_answer_contract.py CHANGELOG.md docs/knowledge-base.md
aramid check --staged
git commit -m "docs(answer): retire python-nested-name-shared-id and python-bare-call-ignores-scope; changelog the scope-identity engine (#70, #71)"
```

---

### Task 11: audit graphite's own node-id holders

Spec §6: before release, check every place graphite itself persists node ids, and what reads them back. Use graph queries first (graph-first rule). Read the write sites the graph points to.

**Files:**
- Read: `src/graphite/incident_ledger.py`, `src/graphite/daemon.py` (the supervision manifest), `src/graphite/freshness.py` (`check` records), and RELEASING.md's "Retention and rollback" (the rollback store).
- Output: `$EVIDENCE/node-id-holders.md`

- [ ] **Step 1: For each holder, find what it writes and what reads it back.**

```bash
for f in src/graphite/incident_ledger.py src/graphite/daemon.py src/graphite/freshness.py; do
  python -m graphite context "$f" > "$SCRATCH/ctx-$(basename "$f" .py).txt" 2>&1; echo "$f EXIT=$?"
done
```

Read each context, then the functions it names that write to disk. For each holder, record in `node-id-holders.md`:
- does it persist a node id (not a path, not a symbol name)?
- which function reads it back?
- does that reader compare it against a graph built by a DIFFERENT engine?

For the rollback store, which is wheels plus evidence records, record whether any evidence file holds node ids, and whether anything reads them.

- [ ] **Step 2: Decide.**
  - A holder that persists node ids AND reads them back across an engine change is a release blocker. Stop and report it to the maintainer with the file and line.
  - Otherwise, add one sentence to the CHANGELOG entry from Task 10: "graphite's own node-id holders (incident ledger, daemon manifest, `check` records, rollback store) were audited; none reads a node id back across an engine change."

  Commit that sentence with the maintainer's go:

```bash
git add CHANGELOG.md
aramid check --staged
git commit -m "docs(changelog): node-id holder audit for the scope-identity engine"
```

---

### Task 12: final gate

- [ ] **Step 1: The whole suite, the way the gate runs it.**

```bash
$PY -m pytest -q -p no:cacheprovider > "$SCRATCH/final.txt" 2>&1; echo "EXIT=$?" >> "$SCRATCH/final.txt"; tail -5 "$SCRATCH/final.txt"
```

Expected: `EXIT=0`. Run it in the background (13–15 minutes), and wait for it to finish rather than polling.

- [ ] **Step 2: mypy over every changed source file. The pre-push gate runs it, and pytest does not.**

```bash
python -m mypy src/graphite/extract/ast.py src/graphite/extract/pyscope.py src/graphite/query.py src/graphite/context.py src/graphite/answer_contract.py > "$SCRATCH/final-mypy.txt" 2>&1; echo "MYPY=$?"; cat "$SCRATCH/final-mypy.txt"
```

Expected: `MYPY=0`, and the printed output says `Success`. A filter that matches no lines is not a pass, so read the whole output.

- [ ] **Step 3: Gate state.**
  - Run `aramid ledger filter --status open` and read any finding on the files this plan touched.
  - A WARN that is correct for this change (for example red-proof on a mutation-killing test) gets `aramid override <id> --reason "<cited reason>"`. Anything else gets fixed.

- [ ] **Step 4: Push only with the maintainer's explicit go, and only after the pre-push checks.**
  1. Run the load probe: `$PY -m pytest tests/test_typescript_activation.py -q`. Proceed only if pytest reports 42.5 s or less.
  2. Read aramid's fleet rows.
  3. Make sure the push does not start in a drain window (02, 06, 10, 14, 18 or 22 Z).
  4. Run `git push` in the background. It takes 10–26 minutes under the gate.
  5. Commit nothing while it runs.
  6. Afterwards, confirm CI is green on every job of the pushed sha.

- [ ] **Step 5: Hand over to release.** This plan ends at a pushed, CI-green `main`. The release itself (proposed 1.2.0) follows `RELEASING.md` and is the maintainer's dispatch:
  - the version is measured, with Task 9's id-survival table as the measurement;
  - the release notes carry Task 9's figures and Task 11's audit sentence;
  - there is no channel message (spec §6).

---

## Not in this plan

- **Phase 2, value references** (`2026-10-04-python-value-references-design.md`).
- **A `qualname` tier in `graphite search`.** `search` rows gain `qualname`, but its match tiers are unchanged; a dotted search token still matches through `tokens`.
- **Other languages' id collisions** (spec decision 3, §7).
- **Re-export hops through a package `__init__`** (spec §7; reported by the oracle as `unbound-but-def(reexport)`).
