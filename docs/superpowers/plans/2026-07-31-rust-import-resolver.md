# Rust Import Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind Rust `use` and `mod` targets to real file nodes so `resolution_health` stops reporting `imports 0/8` and strict graph-first enforcement re-arms.

**Architecture:** Extend `SourceIndex` (`src/graphite/resolve.py`) with Cargo manifest indexing and two lexical resolvers, mirroring the existing Python/TypeScript pattern — generate candidate paths, test membership in `rel_paths`, no compiler. Then thread `SourceIndex` into `_extract_rust`, which currently receives none.

**Tech Stack:** Python 3.11+, `tomllib` (stdlib), tree-sitter, pytest, ruff.

Spec: `docs/superpowers/specs/2026-07-31-rust-import-resolver-design.md`

## Global Constraints

- Python floor is `>=3.11` (`pyproject.toml`) — `tomllib` is stdlib, no new dependency may be added.
- Resolution is **purely lexical**: candidate paths tested against `SourceIndex.rel_paths`. No filesystem walking beyond reading manifests already present in the scanned file set, no network, no cargo invocation.
- **"Bound" means the edge target node is real** (`health.py:82`: `kind != "unknown"`). Re-tagging confidence alone achieves nothing.
- **`EXTERNAL_IMPORT` is only for targets a resolver positively confirmed leave the repo** (allowlisted root, or a declared Cargo dependency). An unresolved target MUST stay default confidence so it is counted against the ratio. Never tag "unresolved" as "external".
- With `source_index=None`, `_extract_rust` output must be byte-identical to current behaviour.
- Manifest parsing must be total — a malformed, unreadable, or virtual `Cargo.toml` is skipped, never raised.
- **A resolved edge target MUST be `_file_node_id(rel_path)`** (`extract/ast.py:167`), exactly as the TypeScript branch does at `:405`. Using `_make_id(rel_path)` produces a different id, the target node will not exist, and the edge counts as *unbound* — silently defeating the entire task.
- Rust extraction tests live in **`tests/test_go_rust.py`**, not `tests/test_call_graph.py`. Reuse its existing `_extract(tmp_path)` and `_rust_fixture(tmp_path)` helpers.
- **`tests/test_go_rust.py::test_rust_use_declarations_become_import_edges` currently pins the broken behaviour** (`:167-168` asserts the synthetic id `crate_store_store`). Task 5 must update it deliberately, not delete it.
- Run the suite as the bare `pytest` console script, redirect to a file, and read the exit code from the file (a pipe reports the last command's status).
- Repo is graphite-first: use `python -m graphite query ...` for cross-file questions; grep only for literal text.

---

### Task 1: Cargo manifest indexing

**Files:**
- Modify: `src/graphite/resolve.py`
- Test: `tests/test_resolve.py` (create if absent)

**Interfaces:**
- Consumes: nothing.
- Produces: `_normalize_crate_name(raw: str) -> str`; `_load_cargo_crates(root: Path, rel_paths: frozenset[str]) -> tuple[tuple[str, str, str], ...]` yielding `(normalised_name, crate_dir_rel, src_root_rel)`; `_load_cargo_dependencies(root: Path, rel_paths: frozenset[str]) -> frozenset[str]`; `SourceIndex.cargo_crates` and `SourceIndex.cargo_dependencies` fields.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_resolve.py
from pathlib import Path

from graphite.resolve import (
    _load_cargo_crates,
    _load_cargo_dependencies,
    _normalize_crate_name,
)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_normalize_crate_name_uses_cargo_underscore_rule():
    assert _normalize_crate_name("ofw-contracts") == "ofw_contracts"
    assert _normalize_crate_name("  ofw_policy ") == "ofw_policy"


def test_load_cargo_crates_maps_name_to_src_root(tmp_path: Path):
    _write(tmp_path, "crates/ofw-contracts/Cargo.toml", '[package]\nname = "ofw-contracts"\n')
    rel_paths = frozenset({"crates/ofw-contracts/Cargo.toml"})

    assert _load_cargo_crates(tmp_path, rel_paths) == (
        ("ofw_contracts", "crates/ofw-contracts", "crates/ofw-contracts/src"),
    )


def test_load_cargo_crates_skips_virtual_and_malformed_manifests(tmp_path: Path):
    _write(tmp_path, "Cargo.toml", '[workspace]\nmembers = ["crates/*"]\n')
    _write(tmp_path, "crates/broken/Cargo.toml", "this is not valid toml {{{")
    rel_paths = frozenset({"Cargo.toml", "crates/broken/Cargo.toml"})

    assert _load_cargo_crates(tmp_path, rel_paths) == ()


def test_load_cargo_dependencies_unions_all_tables(tmp_path: Path):
    _write(
        tmp_path,
        "crates/a/Cargo.toml",
        '[package]\nname = "a"\n'
        '[dependencies]\nserde = "1"\n'
        '[dev-dependencies]\nproptest = "1"\n'
        '[build-dependencies]\ncc = "1"\n',
    )
    _write(tmp_path, "Cargo.toml", '[workspace.dependencies]\ntokio-util = "0.7"\n')
    rel_paths = frozenset({"crates/a/Cargo.toml", "Cargo.toml"})

    assert _load_cargo_dependencies(tmp_path, rel_paths) == frozenset(
        {"serde", "proptest", "cc", "tokio_util"}
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_resolve.py -v`
Expected: FAIL — `ImportError: cannot import name '_load_cargo_crates'`

- [ ] **Step 3: Implement**

In `src/graphite/resolve.py`, add `import tomllib` beside the existing imports, then:

```python
_RUST_EXTERNAL_ROOTS: Final = frozenset({"std", "core", "alloc", "proc_macro"})
_CARGO_DEPENDENCY_TABLES: Final = ("dependencies", "dev-dependencies", "build-dependencies")


def _normalize_crate_name(raw: str) -> str:
    """Cargo maps a package name's hyphens to underscores for the crate ident."""
    return raw.strip().replace("-", "_")


def _read_cargo_manifest(root: Path, rel: str) -> dict[str, object] | None:
    """Parse one Cargo.toml. Total: unreadable or malformed yields None."""
    try:
        return tomllib.loads((root / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


def _cargo_manifest_paths(rel_paths: frozenset[str]) -> list[str]:
    return sorted(rel for rel in rel_paths if PurePosixPath(rel).name == "Cargo.toml")


def _load_cargo_crates(root: Path, rel_paths: frozenset[str]) -> tuple[tuple[str, str, str], ...]:
    """(crate ident, crate dir, src root) per real package manifest in the scan."""
    crates: list[tuple[str, str, str]] = []
    for rel in _cargo_manifest_paths(rel_paths):
        data = _read_cargo_manifest(root, rel)
        if data is None:
            continue
        package = data.get("package")
        if not isinstance(package, dict):
            continue  # virtual/workspace-only manifest declares no crate
        name = package.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        parent = PurePosixPath(rel).parent.as_posix()
        crate_dir = "" if parent == "." else parent
        src_root = f"{crate_dir}/src" if crate_dir else "src"
        crates.append((_normalize_crate_name(name), crate_dir, src_root))
    return tuple(crates)


def _load_cargo_dependencies(root: Path, rel_paths: frozenset[str]) -> frozenset[str]:
    """Declared dependency idents across every manifest, including workspace ones."""
    names: set[str] = set()
    for rel in _cargo_manifest_paths(rel_paths):
        data = _read_cargo_manifest(root, rel)
        if data is None:
            continue
        scopes: list[object] = [data]
        workspace = data.get("workspace")
        if isinstance(workspace, dict):
            scopes.append(workspace)
        for scope in scopes:
            if not isinstance(scope, dict):
                continue
            for table in _CARGO_DEPENDENCY_TABLES:
                section = scope.get(table)
                if isinstance(section, dict):
                    names.update(
                        _normalize_crate_name(key) for key in section if isinstance(key, str)
                    )
    return frozenset(names)
```

Add the fields to `SourceIndex` (after `workspace_packages`, so existing positional construction is unaffected):

```python
    cargo_crates: tuple[tuple[str, str, str], ...] = ()
    cargo_dependencies: frozenset[str] = frozenset()
```

And wire them in `SourceIndex.from_entries`, alongside the existing `workspace_packages=` argument:

```python
            cargo_crates=_load_cargo_crates(root, frozenset(rel_paths)),
            cargo_dependencies=_load_cargo_dependencies(root, frozenset(rel_paths)),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_resolve.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/graphite/resolve.py tests/test_resolve.py
git commit -m "feat(resolve): index Cargo manifests for Rust import resolution"
```

---

### Task 2: Rust module-path helpers

**Files:**
- Modify: `src/graphite/resolve.py`
- Test: `tests/test_resolve.py`

**Interfaces:**
- Consumes: Task 1's module-level constants.
- Produces: `_rust_module_dir(rel_path: str) -> str`; `_rust_module_candidates(base: str) -> list[str]`; `_rust_module_segments(segments: list[str]) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

```python
from graphite.resolve import (
    _rust_module_candidates,
    _rust_module_dir,
    _rust_module_segments,
)


def test_rust_module_dir_root_files_use_their_own_directory():
    assert _rust_module_dir("crates/a/src/lib.rs") == "crates/a/src"
    assert _rust_module_dir("crates/a/src/main.rs") == "crates/a/src"
    assert _rust_module_dir("crates/a/src/net/mod.rs") == "crates/a/src/net"


def test_rust_module_dir_file_module_uses_a_directory_named_after_it():
    assert _rust_module_dir("crates/a/src/policy.rs") == "crates/a/src/policy"


def test_rust_module_candidates_covers_both_layouts():
    assert _rust_module_candidates("src/policy") == ["src/policy.rs", "src/policy/mod.rs"]


def test_rust_module_segments_stops_at_the_first_item_segment():
    # Modules are snake_case, items are CamelCase -- Rule is an item, not a dir.
    assert _rust_module_segments(["policy", "rule", "Rule"]) == ["policy", "rule"]
    assert _rust_module_segments(["Rule"]) == []
    assert _rust_module_segments(["policy", "rule"]) == ["policy", "rule"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_resolve.py -v -k rust_module`
Expected: FAIL — `ImportError: cannot import name '_rust_module_dir'`

- [ ] **Step 3: Implement**

```python
_RUST_MODULE_ROOT_FILES: Final = frozenset({"lib.rs", "main.rs", "mod.rs"})


def _rust_module_dir(rel_path: str) -> str:
    """Directory Rust searches for this file's child modules.

    `lib.rs`/`main.rs`/`mod.rs` are their module's root, so their children sit
    beside them. Any other `foo.rs` owns a sibling directory `foo/`.
    """
    path = PurePosixPath(rel_path)
    parent_raw = path.parent.as_posix()
    parent = "" if parent_raw == "." else parent_raw
    if path.name in _RUST_MODULE_ROOT_FILES:
        return parent
    return f"{parent}/{path.stem}" if parent else path.stem


def _rust_module_candidates(base: str) -> list[str]:
    return [f"{base}.rs", f"{base}/mod.rs"]


def _rust_module_segments(segments: list[str]) -> list[str]:
    """Leading segments that can be modules.

    A `use` path blends module path and item name with no lexical boundary.
    Rust modules are snake_case and items CamelCase, so an uppercase initial
    ends the module portion.
    """
    modules: list[str] = []
    for segment in segments:
        if segment[:1].isupper():
            break
        modules.append(segment)
    return modules
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_resolve.py -v -k rust_module`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/graphite/resolve.py tests/test_resolve.py
git commit -m "feat(resolve): add Rust module-path helpers"
```

---

### Task 3: `resolve_rust_use`

**Files:**
- Modify: `src/graphite/resolve.py`
- Test: `tests/test_resolve.py`

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: `RustUseResolution` dataclass with fields `rel_path: str | None` and `external: bool`; `SourceIndex.resolve_rust_use(importer_rel_path: str, use_path: str, inline_mod_depth: int = 0) -> RustUseResolution`; private `SourceIndex._rust_crate_for` and `SourceIndex._rust_walk`.
- Three-state contract: `(rel_path=<path>, external=False)` bound; `(None, True)` confirmed external; `(None, False)` unresolved — the caller must keep default confidence for the third.

- [ ] **Step 1: Write the failing tests**

```python
from graphite.resolve import RustUseResolution, SourceIndex


def _index(tmp_path: Path, rel_paths: set[str], crates=(), deps=frozenset()) -> SourceIndex:
    return SourceIndex(
        root=tmp_path,
        rel_paths=frozenset(rel_paths),
        path_aliases=(),
        typescript=None,
        cargo_crates=crates,
        cargo_dependencies=deps,
    )


CRATES = (
    ("ofw_policy", "crates/ofw-policy", "crates/ofw-policy/src"),
    ("ofw_contracts", "crates/ofw-contracts", "crates/ofw-contracts/src"),
)
FILES = {
    "crates/ofw-policy/src/lib.rs",
    "crates/ofw-policy/src/rule.rs",
    "crates/ofw-contracts/src/lib.rs",
}


def test_resolve_rust_use_tags_allowlisted_roots_external(tmp_path: Path):
    index = _index(tmp_path, FILES, CRATES)
    assert index.resolve_rust_use(
        "crates/ofw-policy/src/lib.rs", "std::collections::BTreeSet"
    ) == RustUseResolution(None, True)


def test_resolve_rust_use_tags_declared_dependencies_external(tmp_path: Path):
    index = _index(tmp_path, FILES, CRATES, frozenset({"serde"}))
    assert index.resolve_rust_use(
        "crates/ofw-policy/src/lib.rs", "serde::Serialize"
    ) == RustUseResolution(None, True)


def test_resolve_rust_use_leaves_an_unknown_root_unresolved(tmp_path: Path):
    # The guard against laundering a real miss as "external".
    index = _index(tmp_path, FILES, CRATES, frozenset({"serde"}))
    assert index.resolve_rust_use(
        "crates/ofw-policy/src/lib.rs", "typo_crate::Thing"
    ) == RustUseResolution(None, False)


def test_resolve_rust_use_binds_a_sibling_workspace_crate(tmp_path: Path):
    index = _index(tmp_path, FILES, CRATES)
    assert index.resolve_rust_use(
        "crates/ofw-policy/src/lib.rs", "ofw_contracts::Rule"
    ) == RustUseResolution("crates/ofw-contracts/src/lib.rs", False)


def test_resolve_rust_use_binds_crate_relative_module(tmp_path: Path):
    index = _index(tmp_path, FILES, CRATES)
    assert index.resolve_rust_use(
        "crates/ofw-policy/src/lib.rs", "crate::rule::Rule"
    ) == RustUseResolution("crates/ofw-policy/src/rule.rs", False)


def test_resolve_rust_use_super_inside_an_inline_module_is_the_same_file(tmp_path: Path):
    index = _index(tmp_path, FILES, CRATES)
    assert index.resolve_rust_use(
        "crates/ofw-policy/src/lib.rs", "super::Rule", inline_mod_depth=1
    ) == RustUseResolution("crates/ofw-policy/src/lib.rs", False)


def test_resolve_rust_use_super_at_file_scope_is_the_parent_module(tmp_path: Path):
    index = _index(tmp_path, FILES, CRATES)
    assert index.resolve_rust_use(
        "crates/ofw-policy/src/rule.rs", "super::Thing", inline_mod_depth=0
    ) == RustUseResolution("crates/ofw-policy/src/lib.rs", False)


def test_resolve_rust_use_crate_falls_back_to_nearest_src_without_a_manifest(tmp_path: Path):
    """A repo whose Cargo.toml was not scanned must still resolve `crate::`."""
    index = _index(tmp_path, {"src/app.rs", "src/store.rs"}, crates=())
    assert index.resolve_rust_use(
        "src/app.rs", "crate::store::Store"
    ) == RustUseResolution("src/store.rs", False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_resolve.py -v -k resolve_rust_use`
Expected: FAIL — `ImportError: cannot import name 'RustUseResolution'`

- [ ] **Step 3: Implement**

```python
@dataclass(frozen=True)
class RustUseResolution:
    """Three states: bound (rel_path set), confirmed external, or unresolved."""

    rel_path: str | None
    external: bool


_RUST_UNRESOLVED: Final = RustUseResolution(None, False)
_RUST_EXTERNAL: Final = RustUseResolution(None, True)
```

Then, as methods on `SourceIndex`:

```python
    def _rust_crate_for(self, rel_path: str) -> tuple[str, str, str] | None:
        """Innermost crate containing this file (longest matching crate dir).

        Falls back to the nearest `src/` ancestor when no manifest matches, so
        `crate::` still resolves in a repo whose Cargo.toml was not scanned.
        Without this, a crate-relative import silently fails to bind.
        """
        best: tuple[str, str, str] | None = None
        for entry in self.cargo_crates:
            crate_dir = entry[1]
            if crate_dir and not rel_path.startswith(f"{crate_dir}/"):
                continue
            if best is None or len(crate_dir) > len(best[1]):
                best = entry
        if best is not None:
            return best
        parts = PurePosixPath(rel_path).parts
        if "src" in parts:
            index = len(parts) - 1 - parts[::-1].index("src")
            src_root = "/".join(parts[: index + 1])
            crate_dir = "/".join(parts[:index])
            return ("", crate_dir, src_root)
        return None

    def _rust_walk(self, anchor: str, rest: list[str]) -> str | None:
        """Longest-module-prefix-first search for a file under `anchor`."""
        modules = _rust_module_segments(rest)
        for depth in range(len(modules), -1, -1):
            parts = modules[:depth]
            if parts:
                joined = "/".join(parts)
                base = f"{anchor}/{joined}" if anchor else joined
                candidates = _rust_module_candidates(base)
            else:
                candidates = [
                    f"{anchor}/{name}" if anchor else name
                    for name in ("lib.rs", "mod.rs", "main.rs")
                ]
            for candidate in candidates:
                normalized = posixpath.normpath(candidate).lstrip("./")
                if normalized in self.rel_paths:
                    return normalized
        return None

    def resolve_rust_use(
        self, importer_rel_path: str, use_path: str, inline_mod_depth: int = 0
    ) -> RustUseResolution:
        """Lexically resolve a Rust `use` path. See RustUseResolution."""
        segments = [s for s in use_path.replace(" ", "").split("::") if s]
        if not segments:
            return _RUST_UNRESOLVED
        head, rest = segments[0], segments[1:]
        if head in _RUST_EXTERNAL_ROOTS:
            return _RUST_EXTERNAL

        if head == "crate":
            crate = self._rust_crate_for(importer_rel_path)
            if crate is None:
                return _RUST_UNRESOLVED
            anchor = crate[2]
        elif head == "self":
            anchor = _rust_module_dir(importer_rel_path)
        elif head == "super":
            if inline_mod_depth > 0:
                # `super` from an inline `mod` block is still this file.
                return RustUseResolution(importer_rel_path, False)
            parent_raw = PurePosixPath(_rust_module_dir(importer_rel_path)).parent.as_posix()
            anchor = "" if parent_raw == "." else parent_raw
        else:
            target = next((c for c in self.cargo_crates if c[0] == head), None)
            if target is None:
                return _RUST_EXTERNAL if head in self.cargo_dependencies else _RUST_UNRESOLVED
            resolved = self._rust_walk(target[2], rest)
            return RustUseResolution(resolved, False)

        return RustUseResolution(self._rust_walk(anchor, rest), False)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_resolve.py -v -k resolve_rust_use`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/graphite/resolve.py tests/test_resolve.py
git commit -m "feat(resolve): resolve Rust use paths to repo files"
```

---

### Task 4: `resolve_rust_mod`

**Files:**
- Modify: `src/graphite/resolve.py`
- Test: `tests/test_resolve.py`

**Interfaces:**
- Consumes: Task 2's `_rust_module_dir` and `_rust_module_candidates`.
- Produces: `SourceIndex.resolve_rust_mod(importer_rel_path: str, mod_name: str) -> str | None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_resolve_rust_mod_from_lib_finds_a_sibling_file(tmp_path: Path):
    index = _index(tmp_path, FILES, CRATES)
    assert (
        index.resolve_rust_mod("crates/ofw-policy/src/lib.rs", "rule")
        == "crates/ofw-policy/src/rule.rs"
    )


def test_resolve_rust_mod_from_a_file_module_descends_into_its_directory(tmp_path: Path):
    files = FILES | {"crates/ofw-policy/src/rule/kind.rs"}
    index = _index(tmp_path, files, CRATES)
    assert (
        index.resolve_rust_mod("crates/ofw-policy/src/rule.rs", "kind")
        == "crates/ofw-policy/src/rule/kind.rs"
    )


def test_resolve_rust_mod_accepts_the_mod_rs_layout(tmp_path: Path):
    files = FILES | {"crates/ofw-policy/src/net/mod.rs"}
    index = _index(tmp_path, files, CRATES)
    assert (
        index.resolve_rust_mod("crates/ofw-policy/src/lib.rs", "net")
        == "crates/ofw-policy/src/net/mod.rs"
    )


def test_resolve_rust_mod_returns_none_when_absent(tmp_path: Path):
    index = _index(tmp_path, FILES, CRATES)
    assert index.resolve_rust_mod("crates/ofw-policy/src/lib.rs", "missing") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_resolve.py -v -k resolve_rust_mod`
Expected: FAIL — `AttributeError: 'SourceIndex' object has no attribute 'resolve_rust_mod'`

- [ ] **Step 3: Implement**

```python
    def resolve_rust_mod(self, importer_rel_path: str, mod_name: str) -> str | None:
        """Resolve a `mod foo;` declaration to the file it pulls in.

        Unambiguous, unlike `use`: exactly two legal layouts.
        """
        if not mod_name:
            return None
        base_dir = _rust_module_dir(importer_rel_path)
        base = f"{base_dir}/{mod_name}" if base_dir else mod_name
        for candidate in _rust_module_candidates(base):
            normalized = posixpath.normpath(candidate).lstrip("./")
            if normalized in self.rel_paths:
                return normalized
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_resolve.py -v -k resolve_rust_mod`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/graphite/resolve.py tests/test_resolve.py
git commit -m "feat(resolve): resolve Rust mod declarations"
```

---

### Task 5: Extractor — thread `SourceIndex` and emit resolved `use` edges

**Files:**
- Modify: `src/graphite/extract/ast.py` (signature at `:1266`, `use_declaration` branch at `:1321-1328`, dispatch at `:1456-1459`)
- Modify: `tests/test_go_rust.py` — including the existing `test_rust_use_declarations_become_import_edges` at `:162`

**Interfaces:**
- Consumes: Task 3's `SourceIndex.resolve_rust_use` and `RustUseResolution`.
- Produces: `_extract_rust(file_id, rel_path, source, tree, source_index=None)`; `walk(node, parent_id, scope_id, in_impl, inline_mod_depth)`.

- [ ] **Step 1: Update the existing test that pins the broken behaviour**

`tests/test_go_rust.py::test_rust_use_declarations_become_import_edges` (`:162`) currently asserts the synthetic ids. `_rust_fixture` writes `src/app.rs` with `use std::collections::HashMap;` and `use crate::store::Store;`, and `src/store.rs`. After this task, `std::…` keeps its synthetic target (now tagged external) while `crate::store::Store` binds to the real file node. Rewrite it — the old assertion described a defect, not a contract:

```python
def test_rust_use_declarations_become_import_edges(tmp_path: Path) -> None:
    _rust_fixture(tmp_path)
    result = _extract(tmp_path)
    imports = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "imports"}
    confidence = {
        e["target"]: e.get("confidence")
        for e in result.edges
        if e["relation"] == "imports" and e["source"] == "src_app"
    }

    # std is confirmed external: unbound synthetic target, tagged so the
    # health ratio excludes it.
    assert ("src_app", "std_collections_hashmap") in imports
    assert confidence["std_collections_hashmap"] == "EXTERNAL_IMPORT"

    # crate::store::Store now binds to the real file node. The fixture has no
    # Cargo.toml, so this also exercises the nearest-`src` fallback.
    assert ("src_app", "src_store") in imports
    assert confidence["src_store"] != "EXTERNAL_IMPORT"
```

Confirm the exact file node id with `python -c "from graphite.extract.ast import _file_node_id; print(_file_node_id('src/store.rs'))"` and use whatever it prints instead of `src_store` if they differ.

- [ ] **Step 2: Write the new failing tests**

Add to `tests/test_go_rust.py`, reusing its `_extract` helper:

```python
def _rust_workspace_fixture(tmp_path: Path) -> None:
    (tmp_path / "crates/a/src").mkdir(parents=True)
    (tmp_path / "crates/b/src").mkdir(parents=True)
    (tmp_path / "crates/a/Cargo.toml").write_text(
        '[package]\nname = "a"\n[dependencies]\nserde = "1"\n', encoding="utf-8"
    )
    (tmp_path / "crates/b/Cargo.toml").write_text('[package]\nname = "b"\n', encoding="utf-8")
    (tmp_path / "crates/b/src/lib.rs").write_text("pub struct Rule;\n", encoding="utf-8")
    (tmp_path / "crates/a/src/lib.rs").write_text(
        "use std::collections::BTreeSet;\n"
        "use serde::Serialize;\n"
        "use b::Rule;\n"
        "use typo_crate::Thing;\n",
        encoding="utf-8",
    )


def test_rust_use_binds_in_repo_and_tags_only_confirmed_external(tmp_path: Path) -> None:
    _rust_workspace_fixture(tmp_path)
    result = _extract(tmp_path)
    edges = [
        e for e in result.edges if e["relation"] == "imports" and e["source"] == "crates_a_src_lib"
    ]
    confidence = {e["target"]: e.get("confidence") for e in edges}
    targets = set(confidence)

    assert "crates_b_src_lib" in targets            # workspace crate binds
    assert confidence["crates_b_src_lib"] != "EXTERNAL_IMPORT"
    assert confidence["std_collections_btreeset"] == "EXTERNAL_IMPORT"
    assert confidence["serde_serialize"] == "EXTERNAL_IMPORT"
    # The anti-laundering guard: a real miss stays counted against the ratio.
    assert confidence["typo_crate_thing"] != "EXTERNAL_IMPORT"


def test_rust_use_super_in_inline_test_module_emits_no_self_edge(tmp_path: Path) -> None:
    (tmp_path / "crates/a/src").mkdir(parents=True)
    (tmp_path / "crates/a/Cargo.toml").write_text('[package]\nname = "a"\n', encoding="utf-8")
    (tmp_path / "crates/a/src/lib.rs").write_text(
        "pub struct Rule;\n#[cfg(test)]\nmod tests {\n    use super::Rule;\n}\n",
        encoding="utf-8",
    )
    result = _extract(tmp_path)

    assert [e for e in result.edges if e["relation"] == "imports"] == []
```

The literal ids above (`crates_a_src_lib`, `std_collections_btreeset`, …) are predictions of `_file_node_id` / `_make_id` output. Print them once as in Step 1 and correct any that differ rather than adjusting the implementation to match a guess.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_go_rust.py -v -k rust_use`
Expected: FAIL — every import edge still points at a synthetic id, so both the binding assertion and the `EXTERNAL_IMPORT` confidence assertions fail.

- [ ] **Step 4: Implement**

Change the signature at `ast.py:1266`:

```python
def _extract_rust(
    file_id: str,
    rel_path: str,
    source: bytes,
    tree: Any,
    source_index: SourceIndex | None = None,
) -> ExtractionResult:
```

Change the dispatch at `ast.py:1456-1459` so Rust receives the index (Go is unchanged):

```python
            result = _extract_python(file_id, rel_path, source, tree, source_index)
        elif entry.language == "rust":
            result = _extract_rust(file_id, rel_path, source, tree, source_index)
        else:
            result = _extract_go(file_id, rel_path, source, tree)
```

Give `walk` the new parameter — `def walk(node, parent_id, scope_id, in_impl, inline_mod_depth)` — and thread `inline_mod_depth` unchanged through every existing recursive call and through `walk_children`. Replace the `use_declaration` branch:

```python
        elif t == "use_declaration":
            target = _use_target(node)
            if target:
                resolution = (
                    source_index.resolve_rust_use(rel_path, target, inline_mod_depth)
                    if source_index is not None
                    else None
                )
                if resolution is not None and resolution.rel_path is not None:
                    if resolution.rel_path != rel_path:
                        # A file importing itself carries no dependency; drop it.
                        # _file_node_id, NOT _make_id -- the target must be the
                        # id the file node was actually created under, or the
                        # edge stays unbound (see Global Constraints).
                        result.edges.append(
                            _edge(
                                file_id,
                                _file_node_id(resolution.rel_path),
                                "imports",
                                rel_path,
                                _line(node),
                            )
                        )
                elif resolution is not None and resolution.external:
                    result.edges.append(
                        _edge(
                            file_id,
                            _make_id(target),
                            "imports",
                            rel_path,
                            _line(node),
                            confidence="EXTERNAL_IMPORT",
                        )
                    )
                else:
                    # Unresolved, or no index: unchanged from before, and still
                    # counted against the ratio.
                    result.edges.append(
                        _edge(file_id, _make_id(target), "imports", rel_path, _line(node))
                    )
```

Confirm how a file node id is derived elsewhere in this module (the `_make_id(rel_path)` form used when creating the file node at the top of each extractor) and use exactly that, so the edge target matches the real node.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_go_rust.py -v -k rust_use`
Expected: PASS

Then confirm the whole Go/Rust module is green, which also covers callers that pass no index:

Run: `pytest tests/test_go_rust.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/graphite/extract/ast.py tests/test_go_rust.py
git commit -m "feat(extract): bind Rust use targets to real files"
```

---

### Task 6: Extractor — `mod` declarations

**Files:**
- Modify: `src/graphite/extract/ast.py` (Rust `walk`)
- Modify: `tests/test_go_rust.py`

**Interfaces:**
- Consumes: Task 4's `resolve_rust_mod`, Task 5's `inline_mod_depth` parameter.
- Produces: no new signatures.

- [ ] **Step 1: Write the failing tests**

```python
def test_rust_mod_declaration_links_to_the_module_file(tmp_path: Path) -> None:
    (tmp_path / "crates/a/src").mkdir(parents=True)
    (tmp_path / "crates/a/Cargo.toml").write_text('[package]\nname = "a"\n', encoding="utf-8")
    (tmp_path / "crates/a/src/lib.rs").write_text("mod rule;\n", encoding="utf-8")
    (tmp_path / "crates/a/src/rule.rs").write_text("pub struct Rule;\n", encoding="utf-8")
    result = _extract(tmp_path)

    imports = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "imports"}
    assert ("crates_a_src_lib", "crates_a_src_rule") in imports


def test_rust_inline_mod_emits_no_module_edge(tmp_path: Path) -> None:
    (tmp_path / "crates/a/src").mkdir(parents=True)
    (tmp_path / "crates/a/Cargo.toml").write_text('[package]\nname = "a"\n', encoding="utf-8")
    (tmp_path / "crates/a/src/lib.rs").write_text(
        "mod tests {\n    pub fn helper() {}\n}\n", encoding="utf-8"
    )
    result = _extract(tmp_path)

    assert [e for e in result.edges if e["relation"] == "imports"] == []
```

As in Task 5, verify the literal node ids with `_file_node_id` before assuming them.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_go_rust.py -v -k "rust_mod or rust_inline"`
Expected: FAIL — `mod_item` has no branch in the Rust walk, so no edge exists at all.

- [ ] **Step 3: Implement**

Add a `mod_item` branch to the Rust `walk`, before the `use_declaration` branch:

```python
        elif t == "mod_item":
            mod_name = _text(node.child_by_field_name("name"))
            body = node.child_by_field_name("body")
            if body is None:
                # `mod foo;` -- a file module. This is Rust's file-inclusion
                # mechanism and the only structural link between crate files.
                if mod_name and source_index is not None:
                    resolved = source_index.resolve_rust_mod(rel_path, mod_name)
                    if resolved is not None and resolved != rel_path:
                        result.edges.append(
                            _edge(
                                file_id,
                                _file_node_id(resolved),  # NOT _make_id
                                "imports",
                                rel_path,
                                _line(node),
                            )
                        )
            else:
                # Inline `mod foo { ... }` -- same file, so `super` inside it
                # still refers to this file.
                walk_children(node, parent_id, scope_id, in_impl, inline_mod_depth + 1)
```

Verify against the tree-sitter Rust grammar that the inline body field is named `body`; if a different name is used, adjust and note it in the commit message.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_go_rust.py -v -k "rust_mod or rust_inline"`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/graphite/extract/ast.py tests/test_go_rust.py
git commit -m "feat(extract): emit edges for Rust mod declarations"
```

---

### Task 7: Full verification and live acceptance

**Files:**
- Modify: none expected (fix-forward only if something fails)

**Interfaces:**
- Consumes: Tasks 1-6.
- Produces: evidence for the spec's five acceptance criteria.

- [ ] **Step 1: Run lint**

```bash
python -m ruff check . > /tmp/lint.log 2>&1; echo "EXIT=$?" >> /tmp/lint.log; tail -5 /tmp/lint.log
```
Expected: `All checks passed!` and `EXIT=0`.

- [ ] **Step 2: Run the full suite as the bare console script**

```bash
pytest -q > /tmp/suite.log 2>&1; echo "EXIT=$?" >> /tmp/suite.log; tail -4 /tmp/suite.log
```
Expected: `EXIT=0`, passed count equal to the pre-change baseline plus exactly the number of tests added across Tasks 1-6. Do not read the exit status through a pipe.

- [ ] **Step 3: Rebuild Operation Firewall's graph**

```bash
cd /f/Projects/operation-firewall && python -m graphite build .
```

- [ ] **Step 4: Verify the health criteria (spec acceptance 2)**

```bash
cd /f/Projects/operation-firewall && python -m graphite query "stats"
```
Expected: `by_relation.imports.ratio` well above `0.0`, `by_language.rust.imports.bound` greater than zero, and `healthy: true`.

**If `by_language.rust.imports.total` is 0**, stop and report rather than declaring success: a zero-total cell is excluded from `healthy` and grades scoped answers `inconclusive` (issue #12). That outcome means externality was applied too broadly and must be investigated, not celebrated.

- [ ] **Step 5: Verify the answer grades (spec acceptance 3 and 4)**

```bash
cd /f/Projects/operation-firewall && python -m graphite query "context crates/ofw-policy/src/lib.rs"
cd /f/Projects/operation-firewall && python -m graphite query "impact crates/ofw-contracts/src/lib.rs"
python -m graphite doctor "F:/Projects/operation-firewall"
```
Expected: both queries report `answer.grade` of `decision_grade` rather than an INCONCLUSIVE marker, and `doctor` no longer reports imports-driven degradation.

- [ ] **Step 6: Commit any fixes and record the evidence**

```bash
git add -A
git commit -m "test: verify Rust import resolver against operation-firewall"
```

Report the before/after numbers explicitly. A green CI run is not evidence the resolver works — the measured ratio change is.

---

## Notes for the implementer

- `SourceIndex` is a frozen dataclass; the new fields must have defaults or the ~20 existing positional constructions in the suite break.
- The `typescript=` argument in the Task 3 test helper is passed `None`. If `SourceIndex` rejects that, build a real empty `TypeScriptCompilerIndex` instead — check `build_typescript_index` for the cheapest construction.
- Do not add a `cache_version` bump. The extraction cache partitions on engine identity, so an extraction change invalidates its own partition.
- Editing `Cargo.toml` alone will not invalidate a cached extraction — `file_set_digest` hashes rel-path names only. When testing manually, touch a `.rs` file or delete `graph-out/`.
