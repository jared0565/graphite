# Python Resolver Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Python cross-module binding — import edges resolve to real file nodes, calls bind across modules via symbol/alias maps and method dispatch — plus the health-metric external-import amendment (schema 2).

**Architecture:** Extraction-time resolution mirroring the TS path: `SourceIndex.resolve_python_module` (lexical module→file), a per-file symbol/alias collector in the Python walk, `is_method` tagging feeding the existing language-agnostic `_resolve_method_dispatch` post-pass, and `resolution_health` excluding `EXTERNAL_IMPORT` edges from denominators.

**Tech Stack:** Python 3.11+, tree-sitter (tree_sitter_python already bundled), networkx, pytest. No new dependencies. No LLM anywhere.

**Spec:** `docs/superpowers/specs/2026-07-25-python-resolver-binding-design.md`. Baseline: 2132 passed / 44 skipped @ `ef2b1d1`.

## Global Constraints

- Candidate roots for absolute Python modules: exactly `""` then `"src"`; per root try `<path>.py` then `<path>/__init__.py`; first hit in `rel_paths` wins. Purely lexical — no sys.path, no site-packages.
- Relative imports: N dots anchor at the importing file's dir, going up N-1 dirs; bare relative (`from . import x`) with empty module resolves the anchor's `__init__.py`.
- Import edges: exactly ONE per imported module. Resolved → target `_file_node_id(resolved)`, `confidence="EXACT_IMPORT"`. Unresolved → target `_make_id(module_dotted)`, `confidence="EXTERNAL_IMPORT"`. From-import NAMES never produce import edges.
- Call binding order (Python): bare name → symbol map → today's `_resolve_call` fallback; attribute call `a.b` → alias map on `a` → else noise filter (`should_keep_call_target`) → member-dispatch stash. `self`/`cls` attribute calls stash `_member`.
- Python methods (functions directly contained by a class) get `extra={"is_method": True}`; the existing `_resolve_method_dispatch` post-pass and its `_MAX_METHOD_DISPATCH_CANDIDATES` cap are NOT modified.
- Health block: `"schema": 2`; every `imports` cell (top-level and per-language) gains `"external": <count>`; `EXTERNAL_IMPORT`-confidence import edges count ONLY there and are excluded from `total`/`bound`/`ratio`. Edges without a `confidence` attr still count in the ratio (absence of evidence ≠ externality). `calls` cells unchanged in shape. `RESOLUTION_HEALTHY_RATIO` and the `healthy` rule unchanged.
- `Config.cache_version` bumps `"v6"` → `"v7"` (both default and env fallback at config.py:32 and :159).
- Tests run in the worktree: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest <target> -q` (bash). NEVER `pip install -e` from the worktree. No background tasks; no full suite inside a task (the completion gate runs it).
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Ruff stays clean on touched files.
- Fixture-test convention: write real files under `tmp_path`, then `collect_files(tmp_path, cfg)` + `extract_all(entries, cfg)` + `build_graph(...)` + `query(...)` — no parser mocks (see tests/test_call_graph.py:20-58 for the pattern; use `Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite")`).

---

### Task 1: `SourceIndex.resolve_python_module`

**Files:**
- Modify: `src/graphite/resolve.py` (add method to `SourceIndex` + module-level helper)
- Test: `tests/test_python_resolver.py` (new file)

**Interfaces:**
- Produces: `SourceIndex.resolve_python_module(importer_rel_path: str, module: str, relative_dots: int = 0) -> str | None` and helper `_python_module_candidates(base: str) -> list[str]`. Tasks 2–3 call the method through the `source_index` object.

- [ ] **Step 1: Write the failing tests** — create `tests/test_python_resolver.py`:

```python
"""Python module→file resolution, import-edge binding, and call binding."""
from __future__ import annotations

from pathlib import Path

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.graph import build_graph
from graphite.ingest import collect_files
from graphite.query import query
from graphite.resolve import SourceIndex


def _index(rel_paths: set[str]) -> SourceIndex:
    from graphite.ts_bridge import TypeScriptCompilerIndex

    return SourceIndex(
        root=Path("."),
        rel_paths=frozenset(rel_paths),
        path_aliases=(),
        typescript=TypeScriptCompilerIndex.unavailable(),
    )


FILES = {
    "src/pkg/__init__.py",
    "src/pkg/ledger.py",
    "src/pkg/tdd.py",
    "src/pkg/commands/__init__.py",
    "src/pkg/commands/drain.py",
    "flat.py",
    "src/json.py",
}


def test_absolute_module_resolves_under_src_root():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "pkg.ledger") == "src/pkg/ledger.py"


def test_absolute_package_resolves_to_init():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "pkg") == "src/pkg/__init__.py"


def test_repo_root_beats_src_root():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "flat") == "flat.py"


def test_relative_single_dot():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "tdd", relative_dots=1) == "src/pkg/tdd.py"


def test_relative_two_dots():
    idx = _index(FILES)
    assert (
        idx.resolve_python_module("src/pkg/commands/drain.py", "ledger", relative_dots=2)
        == "src/pkg/ledger.py"
    )


def test_bare_relative_resolves_package_init():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "", relative_dots=1) == "src/pkg/__init__.py"


def test_stdlib_module_unresolved():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "pathlib") is None


def test_local_file_shadows_stdlib_name():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "json") == "src/json.py"


def test_empty_absolute_module_is_none():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "") is None
```

NOTE: if `TypeScriptCompilerIndex` has no `unavailable()` classmethod, construct the inert index the way `resolve.py`'s own tests or `build_typescript_index` fallback do (grep `tests/test_typescript_resolver.py` for the existing inert-construction idiom) — keep the assertions unchanged.

- [ ] **Step 2: Run to verify failure**

Run: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest tests/test_python_resolver.py -q`
Expected: FAIL — `AttributeError: ... no attribute 'resolve_python_module'`

- [ ] **Step 3: Implement** — in `src/graphite/resolve.py`, add inside `SourceIndex`:

```python
    def resolve_python_module(
        self, importer_rel_path: str, module: str, relative_dots: int = 0
    ) -> str | None:
        """Lexically resolve a Python module path to a repo rel_path, else None.

        Absolute: roots "" then "src"; relative: anchored at the importer's
        directory, one level up per dot beyond the first. Purely repo-relative:
        no sys.path, so a repo file shadowing a stdlib name wins.
        """
        parts = [p for p in module.split(".") if p] if module else []
        candidates: list[str] = []
        if relative_dots > 0:
            anchor = PurePosixPath(importer_rel_path).parent
            for _ in range(relative_dots - 1):
                anchor = anchor.parent
            base = anchor.joinpath(*parts).as_posix() if parts else anchor.as_posix()
            if parts:
                candidates.extend(_python_module_candidates(base))
            else:
                candidates.append(posixpath.normpath(f"{base}/__init__.py"))
        else:
            if not parts:
                return None
            joined = "/".join(parts)
            for root in ("", "src"):
                base = f"{root}/{joined}" if root else joined
                candidates.extend(_python_module_candidates(base))
        for candidate in candidates:
            normalized = posixpath.normpath(candidate).lstrip("./")
            if normalized in self.rel_paths:
                return normalized
        return None
```

and at module level (near `_candidate_paths`):

```python
def _python_module_candidates(base: str) -> list[str]:
    normalized = posixpath.normpath(base)
    if normalized in (".", ""):
        return ["__init__.py"]
    return [f"{normalized}.py", f"{normalized}/__init__.py"]
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest tests/test_python_resolver.py tests/test_typescript_resolver.py -q`
Expected: all PASS

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m ruff check src/graphite/resolve.py tests/test_python_resolver.py
git add src/graphite/resolve.py tests/test_python_resolver.py
git commit -m "feat(resolve): lexical python module-to-file resolution on SourceIndex

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Python import-edge rewrite

**Files:**
- Modify: `src/graphite/extract/ast.py` — `_extract_python` signature + import branch (lines ~486, ~527-538), `extract_file` dispatch (lines ~813-822)
- Test: `tests/test_python_resolver.py` (append)

**Interfaces:**
- Consumes: `source_index.resolve_python_module` (Task 1).
- Produces: `_extract_python(file_id, rel_path, source, tree, source_index=None)`; helper `_python_import_modules(node) -> list[tuple[str, int]]` (module_dotted, relative_dots). Import edges per Global Constraints. Task 3 reuses `_python_import_modules` for the maps.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_python_resolver.py`:

```python
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(tmp_path: Path):
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite")
    entries = collect_files(tmp_path, cfg)
    return extract_all(entries, cfg)


def _py_fixture(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(
        tmp_path / "src" / "pkg" / "ledger.py",
        "class Ledger:\n"
        "    def record_run(self, run_id):\n"
        "        return [run_id]\n"
        "\n"
        "def scan():\n"
        "    return None\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "tdd.py",
        "def auto_resolve_tdd(run_id):\n"
        "    return run_id\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "pipeline.py",
        "import json\n"
        "from pkg import tdd\n"
        "from pkg.ledger import Ledger\n"
        "from .tdd import auto_resolve_tdd as art\n"
        "\n"
        "def run(run_id):\n"
        "    tdd.auto_resolve_tdd(run_id)\n"
        "    art(run_id)\n"
        "    ledger = Ledger()\n"
        "    ledger.record_run(run_id)\n"
        "    return json.dumps({})\n",
    )


def _import_edges(result, source_file):
    return [
        e for e in result.edges
        if e.get("relation") == "imports" and e.get("source_file") == source_file
    ]


def test_import_edges_shapes(tmp_path):
    _py_fixture(tmp_path)
    result = _extract(tmp_path)
    edges = _import_edges(result, "src/pkg/pipeline.py")
    by_target = {(e["target"], e["confidence"]) for e in edges}
    # stdlib: phantom + EXTERNAL_IMPORT
    assert ("json", "EXTERNAL_IMPORT") in by_target
    # from pkg import tdd -> module edge for pkg (resolved to package __init__)
    assert ("src_pkg_init", "EXACT_IMPORT") in by_target
    # from pkg.ledger import Ledger -> ONE edge for the module, file-node target
    assert ("src_pkg_ledger", "EXACT_IMPORT") in by_target
    # relative .tdd -> file node
    assert ("src_pkg_tdd", "EXACT_IMPORT") in by_target
    # imported NAMES never make import edges
    all_targets = {e["target"] for e in edges}
    assert "ledger" not in all_targets  # no dotted-module phantom for resolved module
    assert not any(t.endswith("auto_resolve_tdd") for t in all_targets)
    assert not any(t == "art" for t in all_targets)


def test_import_edge_count_is_one_per_module(tmp_path):
    _py_fixture(tmp_path)
    result = _extract(tmp_path)
    edges = _import_edges(result, "src/pkg/pipeline.py")
    assert len(edges) == 4  # json, pkg, pkg.ledger, .tdd
```

The package-`__init__` file-node id is deterministic: `_file_node_id("src/pkg/__init__.py")` = `src_pkg_init` (strip trailing underscores, collapse runs). If an assertion misfires on a slug, print the id once and pin the actual value — adjust slugs only, never the binding assertions.

- [ ] **Step 2: Run to verify failure**

Run: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest tests/test_python_resolver.py -q`
Expected: new tests FAIL (targets are today's dotted/per-name phantoms with default confidence)

- [ ] **Step 3: Implement** — in `src/graphite/extract/ast.py`:

Add the helper near `_collect_ts_import_symbols`:

```python
def _python_import_modules(node: Any) -> list[tuple[str, int]]:
    """(module_dotted, relative_dots) per module imported by this statement.

    import_statement: one entry per dotted_name / aliased_import child.
    import_from_statement: exactly one entry from the module_name field —
    imported NAMES are deliberately ignored (they are symbols, not modules).
    """
    def _text(n: Any) -> str:
        return n.text.decode("utf-8", errors="ignore") if n is not None and n.text else ""

    out: list[tuple[str, int]] = []
    if node.type == "import_statement":
        for child in node.children:
            if child.type == "dotted_name":
                if _text(child):
                    out.append((_text(child), 0))
            elif child.type == "aliased_import":
                name = child.child_by_field_name("name")
                if _text(name):
                    out.append((_text(name), 0))
    elif node.type == "import_from_statement":
        module = node.child_by_field_name("module_name")
        if module is None:
            return out
        if module.type == "relative_import":
            dots = 0
            dotted = ""
            for child in module.children:
                if child.type == "import_prefix":
                    dots = len(_text(child))
                elif child.type == "dotted_name":
                    dotted = _text(child)
            if dots:
                out.append((dotted, dots))
        elif module.type == "dotted_name" and _text(module):
            out.append((_text(module), 0))
    return out
```

Change `_extract_python`'s signature and import branch:

```python
def _extract_python(file_id: str, rel_path: str, source: bytes, tree: Any, source_index: SourceIndex | None = None) -> ExtractionResult:
```

```python
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
                else:
                    result.edges.append(_edge(
                        file_id, _make_id(module) if module else _make_id("package"),
                        "imports", rel_path, _line(node), confidence="EXTERNAL_IMPORT",
                    ))
            walk_children(node, parent_id, scope_id)
```

Change `extract_file`'s dispatch (lines ~813-822) so Python receives the index:

```python
    elif entry.language in ("python", "go", "rust"):
        parser = _LOADER.parser(entry.language)
        if parser is None:
            return _extract_generic(file_id, rel_path, source, entry.language)
        try:
            tree = parser.parse(source)
        except Exception as e:
            return ExtractionResult(error=f"parse_error: {e}")
        if entry.language == "python":
            result = _extract_python(file_id, rel_path, source, tree, source_index)
        else:
            extractor = {"go": _extract_go, "rust": _extract_rust}[entry.language]
            result = extractor(file_id, rel_path, source, tree)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest tests/test_python_resolver.py tests/test_call_graph.py tests/test_smoke.py -q`
Expected: all PASS (existing suites will re-extract with new edge shapes; if an existing test pins old Python import-edge shapes, update it and justify in your report)

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m ruff check src/graphite/extract/ast.py tests/test_python_resolver.py
git add src/graphite/extract/ast.py tests/test_python_resolver.py
git commit -m "feat(extract): python import edges resolve to file nodes; external imports tagged

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Symbol/alias maps + Python call binding

**Files:**
- Modify: `src/graphite/extract/ast.py` — new collector + `_python_call_target` + rewritten Python `call` branch
- Test: `tests/test_python_resolver.py` (append)

**Interfaces:**
- Consumes: `_python_import_modules` (Task 2), `resolve_python_module` (Task 1), existing `_resolve_call`, `should_keep_call_target` (already imported in ast.py from resolve.py).
- Produces: `_collect_python_import_maps(root, rel_path, source_index) -> tuple[dict[str, str], dict[str, str]]` (symbol_map: local → definition node id; alias_map: local → module FILE node id); `_python_call_target(func) -> tuple[str | None, str | None, str | None]` (bare, obj, attr). Task 4 relies on the `_member` stash added here.

- [ ] **Step 1: Write the failing tests** — append:

```python
def _graph_for(tmp_path):
    result = _extract(tmp_path)
    return build_graph(result.nodes, result.edges)


def test_module_attribute_call_binds_cross_module(tmp_path):
    _py_fixture(tmp_path)
    g = _graph_for(tmp_path)
    out = query(g, "callers auto_resolve_tdd")
    ids = [c["id"] for c in out.get("callers", [])]
    assert "src_pkg_pipeline_run" in ids  # tdd.auto_resolve_tdd(...) bound


def test_from_import_aliased_symbol_call_binds(tmp_path):
    _py_fixture(tmp_path)
    g = _graph_for(tmp_path)
    # run() calls the def twice: tdd.auto_resolve_tdd(...) AND art(...) (alias).
    # build_graph merges duplicate edges by incrementing weight -> weight >= 2
    # proves BOTH the module-attr path and the aliased-symbol path bound.
    edge = g.get_edge_data("src_pkg_pipeline_run", "src_pkg_tdd_auto_resolve_tdd")
    assert edge is not None and edge.get("weight", 0) >= 2.0


def test_class_instantiation_binds_to_class_node(tmp_path):
    _py_fixture(tmp_path)
    g = _graph_for(tmp_path)
    # Ledger() in pipeline.run binds to the class node in ledger.py
    assert g.has_edge("src_pkg_pipeline_run", "src_pkg_ledger_ledger")


def test_same_file_call_binding_unchanged(tmp_path):
    _write(
        tmp_path / "solo.py",
        "def helper():\n    return 1\n\ndef main():\n    return helper()\n",
    )
    g = _graph_for(tmp_path)
    out = query(g, "callers helper")
    assert [c["id"] for c in out.get("callers", [])] == ["solo_main"]


def test_function_local_import_binds(tmp_path):
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(tmp_path / "src" / "pkg" / "util.py", "def deep():\n    return 1\n")
    _write(
        tmp_path / "src" / "pkg" / "lazy.py",
        "def caller():\n"
        "    from pkg.util import deep\n"
        "    return deep()\n",
    )
    g = _graph_for(tmp_path)
    out = query(g, "callers deep")
    assert "src_pkg_lazy_caller" in [c["id"] for c in out.get("callers", [])]
```

(`query(g, "callers X")` resolves `X` by node name; class `Ledger`'s node id is `_make_id("src_pkg_ledger", "Ledger")` → `src_pkg_ledger_ledger`. Verify the exact ids by printing once if an assertion misfires — ids are deterministic slugs, adjust only if the slug differs, never the binding assertion.)

- [ ] **Step 2: Run to verify failure**

Run: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest tests/test_python_resolver.py -q`
Expected: the new tests FAIL (0 callers — nothing binds yet)

- [ ] **Step 3: Implement** — in `src/graphite/extract/ast.py`:

Collector (near `_collect_ts_import_symbols`):

```python
def _collect_python_import_maps(
    root: Any, rel_path: str, source_index: SourceIndex | None
) -> tuple[dict[str, str], dict[str, str]]:
    """(symbol_map: local -> definition node id, alias_map: local -> module file id).

    Walked at ALL depths (Python allows function-local imports). For
    `from P import name`, `P.name` is tried as a MODULE first (alias), then
    as a symbol defined in P's file. Unresolvable modules enter neither map.
    Last binding wins, matching Python shadowing.
    """
    symbol_map: dict[str, str] = {}
    alias_map: dict[str, str] = {}
    if source_index is None:
        return symbol_map, alias_map

    def _text(n: Any) -> str:
        return n.text.decode("utf-8", errors="ignore") if n is not None and n.text else ""

    def visit(node: Any) -> None:
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    module = _text(child)
                    if module and "." not in module:
                        resolved = source_index.resolve_python_module(rel_path, module)
                        if resolved:
                            alias_map[module] = _file_node_id(resolved)
                elif child.type == "aliased_import":
                    module = _text(child.child_by_field_name("name"))
                    local = _text(child.child_by_field_name("alias"))
                    if module and local:
                        resolved = source_index.resolve_python_module(rel_path, module)
                        if resolved:
                            alias_map[local] = _file_node_id(resolved)
        elif node.type == "import_from_statement":
            modules = _python_import_modules(node)
            if modules:
                base_module, dots = modules[0]
                for child in node.children:
                    local = original = None
                    if child.type == "dotted_name" and child.prev_sibling is not None \
                            and _text(child.prev_sibling) in ("import", ","):
                        original = local = _text(child)
                    elif child.type == "aliased_import":
                        original = _text(child.child_by_field_name("name"))
                        local = _text(child.child_by_field_name("alias"))
                    if not original or not local or "." in original:
                        continue
                    sub = f"{base_module}.{original}" if base_module else original
                    as_module = source_index.resolve_python_module(rel_path, sub, dots)
                    if as_module:
                        alias_map[local] = _file_node_id(as_module)
                        continue
                    parent = source_index.resolve_python_module(rel_path, base_module, dots)
                    if parent:
                        symbol_map[local] = _make_id(_file_node_id(parent), original)
        for child in node.children:
            visit(child)

    visit(root)
    return symbol_map, alias_map
```

CAREFUL: the `module_name` field's own `dotted_name` is ALSO a `dotted_name` child of the statement — the `prev_sibling in ("import", ",")` guard is what keeps the module itself out of the imported-names loop. Verify against the real parse in your tests; if the sibling text check is fragile in practice, the robust alternative is: skip any child that IS the `module_name` field node (`child.id == node.child_by_field_name("module_name").id`) and treat remaining `dotted_name`/`aliased_import` children after the `import` keyword as names. Use whichever the parse tree supports — the TESTS define correctness.

Call-target helper:

```python
def _python_call_target(func: Any) -> tuple[str | None, str | None, str | None]:
    """(bare_name, object_name, attribute_name) for a Python call's function node."""
    def _text(n: Any) -> str | None:
        return n.text.decode("utf-8", errors="ignore") if n is not None and n.text else None

    if func.type == "identifier":
        return _text(func), None, None
    if func.type == "attribute":
        obj = func.child_by_field_name("object")
        attr = _text(func.child_by_field_name("attribute"))
        obj_name = _text(obj) if obj is not None and obj.type == "identifier" else None
        return None, obj_name, attr
    return None, None, None
```

In `_extract_python`: collect the maps once before the walk —

```python
    symbol_map, alias_map = _collect_python_import_maps(root, rel_path, source_index)
```

and replace the `call` branch:

```python
        elif node.type == "call":
            func = node.child_by_field_name("function")
            bare, obj_name, attr = _python_call_target(func) if func is not None else (None, None, None)
            edge = None
            if bare and bare not in _LANGUAGE_BUILTIN_GLOBALS:
                target = symbol_map.get(bare) or _resolve_call(file_id, bare)
                edge = _edge(scope_id, target, "calls", rel_path, _line(node), confidence="LOCAL_CALL")
            elif attr:
                dotted = f"{obj_name}.{attr}" if obj_name else attr
                if obj_name and obj_name in alias_map:
                    edge = _edge(scope_id, _make_id(alias_map[obj_name], attr), "calls", rel_path, _line(node), confidence="LOCAL_CALL")
                elif should_keep_call_target(dotted):
                    # Unresolved member call: file-scoped phantom now, re-pointed
                    # (or dropped) by the method-dispatch post-pass via _member.
                    edge = _edge(scope_id, _resolve_call(file_id, dotted), "calls", rel_path, _line(node), confidence="LOCAL_CALL")
                    edge["_member"] = attr
            if edge is not None:
                result.edges.append(edge)
            walk_children(node, parent_id, scope_id)
```

(`should_keep_call_target` drops noisy stdlib-ish members like `.append`/`.get` — the same policy TS uses. `_LANGUAGE_BUILTIN_GLOBALS` keeps filtering bare builtins.)

- [ ] **Step 4: Run to verify pass**

Run: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest tests/test_python_resolver.py tests/test_call_graph.py tests/test_health.py -q`
Expected: all PASS. (test_health builds Python-ish fixtures via `_graph` directly — unaffected; if any existing test pinned old Python phantom call targets, update + justify.)

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m ruff check src/graphite/extract/ast.py tests/test_python_resolver.py
git add src/graphite/extract/ast.py tests/test_python_resolver.py
git commit -m "feat(extract): python symbol/alias import maps bind cross-module calls

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Python method dispatch (`is_method` + `_member` flow-through)

**Files:**
- Modify: `src/graphite/extract/ast.py` — Python `function_definition` branch (tag methods), no dispatch-pass changes
- Test: `tests/test_python_resolver.py` (append)

**Interfaces:**
- Consumes: `_member` stash from Task 3; existing `_resolve_method_dispatch` (ast.py:875) and its cap — DO NOT modify the pass.
- Produces: Python method nodes carry `is_method: True`.

- [ ] **Step 1: Write the failing tests** — append:

```python
def test_instance_method_call_binds_via_dispatch(tmp_path):
    _py_fixture(tmp_path)
    g = _graph_for(tmp_path)
    out = query(g, "callers record_run")
    assert "src_pkg_pipeline_run" in [c["id"] for c in out.get("callers", [])]


def test_self_call_binds_to_own_class_method(tmp_path):
    _write(
        tmp_path / "svc.py",
        "class Svc:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "    def run(self):\n"
        "        return self.helper()\n",
    )
    g = _graph_for(tmp_path)
    out = query(g, "callers helper")
    assert "svc_run" in [c["id"] for c in out.get("callers", [])]


def test_unresolved_member_phantom_dropped(tmp_path):
    _write(
        tmp_path / "ext.py",
        "def go(conn):\n"
        "    return conn.execute_special_thing()\n",
    )
    result = _extract(tmp_path)
    g = build_graph(result.nodes, result.edges)
    # no method named execute_special_thing exists -> edge dropped, no phantom node
    assert not any("execute_special_thing" in n for n in g.nodes())


def test_noisy_member_calls_still_filtered(tmp_path):
    _write(
        tmp_path / "noise.py",
        "def go(items):\n"
        "    items.append(1)\n"
        "    return items\n",
    )
    result = _extract(tmp_path)
    assert not any(
        e.get("relation") == "calls" and "append" in e.get("target", "")
        for e in result.edges
    )


def test_python_methods_tagged_top_level_functions_not(tmp_path):
    _py_fixture(tmp_path)
    result = _extract(tmp_path)
    by_id = {n["id"]: n for n in result.nodes}
    assert by_id["src_pkg_ledger_record_run"].get("is_method") is True
    assert by_id["src_pkg_tdd_auto_resolve_tdd"].get("is_method") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest tests/test_python_resolver.py -q`
Expected: the dispatch/tagging tests FAIL (`is_method` never set for Python; member edges keep phantoms)

- [ ] **Step 3: Implement** — in `_extract_python`, maintain a class-id set and tag:

Add before the walk: `class_ids: set[str] = set()`. In the `class_definition` branch, after computing `cid`: `class_ids.add(cid)`. In the `function_definition` branch, replace the node append with:

```python
                extra = {"is_method": True} if parent_id in class_ids else None
                result.nodes.append(_node(mid, "function", name, rel_path, _line(node), extra))
```

(The dispatch post-pass already strips `_member`, re-points to `is_method` nodes, and drops unresolvable member phantoms — Tasks 3+4 only feed it.)

- [ ] **Step 4: Run to verify pass**

Run: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest tests/test_python_resolver.py tests/test_call_graph.py -q`
Expected: all PASS

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m ruff check src/graphite/extract/ast.py tests/test_python_resolver.py
git add src/graphite/extract/ast.py tests/test_python_resolver.py
git commit -m "feat(extract): python method dispatch — is_method tagging + member flow-through

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Health schema 2 (external-import exclusion) + docs

**Files:**
- Modify: `src/graphite/health.py`, `docs/agent-integration.md` (§7), `docs/schemas/query-result.v1.schema.json` (description only)
- Test: `tests/test_health.py` (update schema assertions + append)

**Interfaces:**
- Consumes: `confidence="EXTERNAL_IMPORT"` tagging (Task 2).
- Produces: block `"schema": 2`; imports cells (top-level + per-language) shaped `{"total", "bound", "ratio", "external"}` where external edges are excluded from total/bound/ratio; calls cells unchanged.

- [ ] **Step 1: Write the failing tests** — in `tests/test_health.py`, update every `["schema"] == 1` assertion to `== 2`, then append:

```python
def test_external_imports_excluded_from_ratio():
    g = nx.DiGraph()
    g.add_node("f", kind="file", source_file="a.py")
    g.add_node("dep", kind="file", source_file="b.py")
    g.add_node("pathlib", kind="unknown")
    g.add_edge("f", "dep", relation="imports", source_file="a.py", confidence="EXACT_IMPORT")
    g.add_edge("f", "pathlib", relation="imports", source_file="a.py", confidence="EXTERNAL_IMPORT")
    block = resolution_health(g)
    assert block["schema"] == 2
    assert block["by_relation"]["imports"] == {
        "total": 1, "bound": 1, "ratio": 1.0, "external": 1,
    }
    assert block["healthy"] is True
    assert block["by_language"]["python"]["imports"]["external"] == 1


def test_untagged_import_edges_still_count():
    g = nx.DiGraph()
    g.add_node("f", kind="file", source_file="a.py")
    g.add_node("ghost", kind="unknown")
    g.add_edge("f", "ghost", relation="imports", source_file="a.py")  # no confidence
    block = resolution_health(g)
    assert block["by_relation"]["imports"] == {
        "total": 1, "bound": 0, "ratio": 0.0, "external": 0,
    }
    assert block["healthy"] is False


def test_calls_cells_have_no_external_field():
    block = resolution_health(nx.DiGraph())
    assert "external" not in block["by_relation"]["calls"]
    assert block["by_relation"]["imports"]["external"] == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest tests/test_health.py -q`
Expected: new tests FAIL; updated schema assertions FAIL (still 1)

- [ ] **Step 3: Implement** — in `src/graphite/health.py`:

- Change the counting loop: track per-relation `external` (imports only):

```python
    relation_counts = {rel: [0, 0, 0] for rel in _COUNTED_RELATIONS}  # [bound, total, external]
    ...
    for _u, v, data in g.edges(data=True):
        relation = data.get("relation")
        counts = relation_counts.get(relation)
        if counts is None:
            continue
        language = _edge_language(data.get("source_file"))
        buckets = language_counts.setdefault(
            language, {rel: [0, 0, 0] for rel in _COUNTED_RELATIONS}
        )
        if relation == "imports" and data.get("confidence") == "EXTERNAL_IMPORT":
            counts[2] += 1
            buckets[relation][2] += 1
            continue
        bound = int(g.nodes[v].get("kind", "unknown") != "unknown")
        counts[0] += bound
        counts[1] += 1
        buckets[relation][0] += bound
        buckets[relation][1] += 1
```

- `_cell` gains the relation-aware shape:

```python
def _cell(bound: int, total: int, external: int, relation: str) -> dict[str, Any]:
    cell: dict[str, Any] = {
        "total": total,
        "bound": bound,
        "ratio": None if total == 0 else round(bound / total, 3),
    }
    if relation == "imports":
        cell["external"] = external
    return cell
```

with both build sites passing `relation` (`_cell(c[0], c[1], c[2], rel)`), and `"schema": 2` in the return.

- `docs/agent-integration.md` §7: update the example block to schema 2 with `"external"` in the imports cells, and add one paragraph: ratios count only should-bind-in-repo edges; `external` counts imports of modules outside the repo (stdlib/pip); graphs built before this change report `"schema": 1` and ratios that include externals — consumers reading ratios should branch on `schema`; consumers reading only `healthy` need no change.
- `docs/schemas/query-result.v1.schema.json`: extend the `resolution_health` property description with: "schema 2: imports cells carry an `external` count; external imports are excluded from ratios."

- [ ] **Step 4: Run to verify pass**

Run: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest tests/test_health.py tests/test_context.py tests/test_agent_hooks.py tests/test_published_schemas.py tests/test_documentation.py -q`
Expected: all PASS

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m ruff check src/graphite/health.py tests/test_health.py
git add src/graphite/health.py tests/test_health.py docs/agent-integration.md docs/schemas/query-result.v1.schema.json
git commit -m "feat(health): schema 2 — external imports counted separately, excluded from ratios

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: cache_version v7 + end-to-end binding acceptance test

**Files:**
- Modify: `src/graphite/config.py:32` and `:159`
- Test: `tests/test_python_resolver.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 1–5.
- Produces: `cache_version` default `"v7"`; an end-to-end test proving the aramid-shaped fixture reaches `healthy: true` and answers callers/impact correctly through the full build path.

- [ ] **Step 1: Write the failing test** — append:

```python
def test_end_to_end_build_binds_and_is_healthy(tmp_path, monkeypatch):
    import json as _json

    _py_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    from graphite.cli import _build_project

    _build_project(tmp_path, Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite"))
    bundle = _json.loads((tmp_path / "graph-out" / "graph.json").read_text(encoding="utf-8"))
    block = bundle["analysis"]["resolution_health"]
    assert block["schema"] == 2
    assert block["healthy"] is True
    assert block["by_relation"]["imports"]["ratio"] == 1.0
    assert block["by_relation"]["imports"]["external"] >= 1  # json import
    assert block["by_relation"]["calls"]["ratio"] >= 0.8


def test_cache_version_is_v7():
    assert Config().cache_version == "v7"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest tests/test_python_resolver.py -q`
Expected: `test_cache_version_is_v7` FAILS (v6); the e2e test should already PASS if Tasks 1–5 are correct — if it fails, that is a real integration bug to fix before proceeding, not a test to adjust.

- [ ] **Step 3: Implement** — `src/graphite/config.py`: change both `"v6"` literals to `"v7"` and extend the line-32 comment: `# bump on extraction-format changes (v7: python import/file-node resolution, import maps, python method dispatch; v6: per-package tsconfig path aliases; ...)`.

- [ ] **Step 4: Run to verify pass**

Run: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest tests/test_python_resolver.py tests/test_engine_identity.py tests/test_smoke.py -q`
Expected: all PASS

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m ruff check src/graphite/config.py tests/test_python_resolver.py
git add src/graphite/config.py tests/test_python_resolver.py
git commit -m "feat(config): cache v7 + end-to-end python binding acceptance test

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Cleanup — deferred Minors from the trust-signal round

**Files:**
- Modify: `src/graphite/context.py` (note line), `tests/test_call_graph.py` (golden pin), `tests/test_health.py` + `tests/test_context.py` + `tests/test_agent_hooks.py` (coverage nits)

**Interfaces:**
- Consumes: `ratio_percent` from `graphite.health`; schema-2 block shape (Task 5).
- Produces: context markdown parity with `cmd_impact`; independent golden pin; three coverage tests.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_context.py`:

```python
def test_context_notes_incomplete_when_nonempty_but_unhealthy():
    import networkx as nx

    from graphite.context import build_context, format_context_markdown

    g = nx.DiGraph()
    g.add_node("caller_file", kind="file", source_file="caller.py")
    g.add_node("target_file", kind="file", source_file="target.py")
    g.add_node("ghost", kind="unknown")
    g.add_edge("caller_file", "target_file", relation="imports", source_file="caller.py")
    g.add_edge("caller_file", "ghost", relation="calls", source_file="caller.py")
    context = build_context(g, ["target_file"])
    text = format_context_markdown(context)
    assert "may be incomplete" in text
    assert "INCONCLUSIVE" not in text


def test_context_carries_full_health_block():
    import networkx as nx

    from graphite.context import build_context

    context = build_context(nx.DiGraph(), [])
    block = context["resolution_health"]
    assert set(block) >= {"schema", "placeholder_nodes", "by_relation", "by_language", "healthy", "threshold"}
```

Append to `tests/test_health.py`:

```python
def test_neighbor_listing_empty_on_unhealthy_graph_is_inconclusive():
    from graphite.query import query

    result = query(_unhealthy_graph(), "depends-on lonely")
    assert result["total"] == 0
    assert result["inconclusive"] is True
```

Append to `tests/test_agent_hooks.py` (reuse that file's strict-mode arrangement + the `_write_analysis`-style helper already added there, writing a non-bool healthy):

```python
def test_strict_denial_suspended_when_healthy_is_not_bool(...existing fixture args...):
    # arrange exactly like the existing unhealthy-suspension test, but write
    # {"resolution_health": {"schema": 2, "healthy": "true"}}  (a string)
    # assert: no permissionDecision; "strict denial suspended" in additionalContext
```

In `tests/test_call_graph.py`: replace the golden test's computed `resolution_health` expectation with a hard literal — run the golden fixture once, print the block, pin it verbatim (schema-2 shape), and delete the `resolution_health(...)` call from the expectation-building code. The pinned literal must not reference the function under test.

- [ ] **Step 2: Run to verify failure**

Run: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest tests/test_context.py tests/test_health.py tests/test_agent_hooks.py tests/test_call_graph.py -q`
Expected: context-note test FAILS (no note line exists); non-bool test FAILS only if arrangement wrong (gate already handles it — this is a coverage pin, it may PASS immediately; that is fine, note it); others per state.

- [ ] **Step 3: Implement** — in `src/graphite/context.py` `format_context_markdown`, after the Impact section's listing branch (the `if impact["impacted_files"]:` arm) and after the Likely tests lines, add:

```python
    if unhealthy and (impact["impacted_files"] or impact["likely_tests"]):
        lines.append(
            f"note: resolution health low (imports {ratio_percent(health, 'imports')}, "
            f"calls {ratio_percent(health, 'calls')}) — this list may be incomplete."
        )
```

(`unhealthy` and `health` already exist in that function from the trust-signal round.)

- [ ] **Step 4: Run to verify pass**

Run: `cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest tests/test_context.py tests/test_health.py tests/test_agent_hooks.py tests/test_call_graph.py -q`
Expected: all PASS

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-python-resolver && PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m ruff check src/graphite/context.py tests
git add src/graphite/context.py tests/test_call_graph.py tests/test_context.py tests/test_health.py tests/test_agent_hooks.py
git commit -m "chore(cleanup): context incomplete-note, independent golden pin, coverage nits

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Completion

Full suite (`PYTHONPATH='F:/tmp/graphite-python-resolver/src' python -m pytest -q`, expect ≥2132+new passed / 44 skipped / 0 failed) + `ruff check src tests`, final whole-branch review per superpowers:subagent-driven-development, then superpowers:finishing-a-development-branch. Post-merge (operator-gated): rebuild aramid + graphite graphs, run spec §10 acceptance (Gates A1–A4, ratio movement, healthy flip check), notify aramid's agent via the reply-file channel, update memory.
