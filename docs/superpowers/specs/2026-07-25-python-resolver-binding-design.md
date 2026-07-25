# Python Resolver Binding Design (Gate A)

**Status:** Approved design, pre-implementation
**Date:** 2026-07-25
**Branch:** `feat/python-resolver-binding`
**Origin:** Aramid field report Gate A (verified root causes: `extract/ast.py:543`
passes no import-symbol map to `_resolve_call`; `ast.py:527-537` emits import
edges targeting dotted-module phantoms that never match `_file_node_id` slugs,
plus bogus per-name edges for from-imports). Follow-on to the trust-signal
round (merged `ef2b1d1`): this round makes the Python graph *bind*; that round
made it honest while unbound.

Measured baseline on aramid (post trust-signal rollout): calls 26.2% bound
(1776/6778), imports 4.7% bound (98/2085), placeholder nodes 54.5%.

## 1. Goal

Python cross-module binding, three mechanisms + one metric amendment:

1. Import edges resolve to real file nodes (module-path→repo-file, incl.
   relative imports); unresolvable imports are tagged external.
2. Cross-module call binding via per-file symbol and module-alias maps
   (`from m import f; f()` and `from p import m; m.f()` both bind).
3. Python member-call dispatch (`instance.method()`) via the existing
   name-based `_resolve_method_dispatch` post-pass.
4. Health metric excludes external imports from its denominators (block
   `schema: 2`) so the ratio measures resolver health, not dependency count.

Operator decisions (2026-07-25): exclude externals from health denominators /
include Python method dispatch / include the trust-signal round's deferred
Minors as a cleanup task. Approach: extraction-time resolution mirroring the
TS path (global name-only binding and full semantic namespace resolution both
rejected — precision and YAGNI respectively).

## 2. Non-goals

- No symbol re-export resolution through `__init__.py` chains
  (`from pkg import X` where `pkg/__init__.py` re-exports `X` from elsewhere
  binds to the `__init__` file's namespace and may remain a placeholder —
  documented known limitation).
- No wildcard-import handling (`from m import *` contributes only the
  module-level import edge).
- No instance-type inference — member dispatch is name-based with the
  existing ambiguity cap.
- No TS/JS/Go/Rust extraction changes (the shared dispatch post-pass gains
  Python inputs; its logic is untouched).
- No LLM/inference anywhere (Canonical Graph Isolation).
- No daemon changes/restart (build children pick up new code; cache_version
  bump forces clean re-extract).

## 3. Mechanism 1 — Python import-edge resolution

New Python module resolution in `resolve.py` (beside the TS machinery),
exposed on `SourceIndex`:

```python
def resolve_python_module(self, importer_rel_path: str, module: str,
                          relative_dots: int = 0) -> str | None:
    """Return the repo rel_path a Python module path resolves to, else None."""
```

Rules (normative):

- **Candidate roots**, in order: `""` (repo root) and `"src"`. For each root
  and module path `a.b.c`, try `<root>/a/b/c.py` then `<root>/a/b/c/__init__.py`;
  first hit in `self.rel_paths` wins.
- **Relative imports**: `relative_dots >= 1` anchors at the importing file's
  directory (1 dot = same package, each extra dot goes one directory up);
  candidates are `<anchor>/<mod/path>.py` then `<anchor>/<mod/path>/__init__.py`;
  a bare relative import (`from . import x`) resolves the anchor's
  `__init__.py` as the module when `module` is empty.
- Resolution is repo-relative and purely lexical: no sys.path, no
  site-packages, no namespace-package guessing. A repo file shadowing a
  stdlib name (a local `json.py`) wins — mirrors real src-layout behavior.

The Python walk's import branch (`_extract_python`, ast.py:527-538) is
rewritten to use tree-sitter **fields**, not child iteration:

- `import_statement`: for each `dotted_name` / `aliased_import` child, the
  module path is the dotted name; emit ONE `imports` edge per imported module.
- `import_from_statement`: the module is `child_by_field_name("module_name")`
  (a `dotted_name` or `relative_import` node); emit exactly ONE `imports`
  edge for the module. **The imported names no longer produce import edges**
  (today's bogus per-name edges disappear).
- Edge target + confidence:
  - Resolved → target `_file_node_id(resolved_rel_path)`,
    `confidence="EXACT_IMPORT"`.
  - Unresolved → target `_make_id(module_dotted)` (today's phantom, stable),
    `confidence="EXTERNAL_IMPORT"`.
  - Relative imports that fail to resolve target
    `_make_id(module_dotted or "package")` with `confidence="EXTERNAL_IMPORT"`
    (rare; malformed trees).

## 4. Mechanism 2 — per-file symbol and alias maps

New collector in ast.py, mirrored on `_collect_ts_import_symbols` but walked
at ALL depths (Python has function-local imports; graphite itself does this):

```python
def _collect_python_import_maps(root, rel_path, source_index) \
        -> tuple[dict[str, str], dict[str, str]]:
    """(symbol_map: local name -> definition node id,
        alias_map:  local name -> module FILE node id)"""
```

- `import mod` → alias_map[`mod`] = file id of resolved `mod`; `import p.m`
  → alias_map[`p`] stays unmapped (attribute chains through packages are out
  of scope) BUT `import p.m as x` → alias_map[`x`] = file id of `p.m`.
- `from P import name [as local]` (absolute or relative P):
  1. Try `P.name` as a MODULE (resolve_python_module) → alias_map[local].
  2. Else, if `P` resolves to a file → symbol_map[local] =
     `_make_id(<P file id>, name)`.
  3. Else (P unresolvable — stdlib/pip): neither map; calls fall back to
     today's file-local behavior.
- Unresolvable names never enter the maps; collisions (same local name bound
  twice) — last binding wins (matches Python shadowing semantics closely
  enough for a lexical tool).

Call binding in the Python `call` branch (ast.py:539-543) becomes:

```python
called = _call_target_name(func, source)
if called and called not in _LANGUAGE_BUILTIN_GLOBALS:
    if "." not in called and called in symbol_map:
        target = symbol_map[called]
    elif "." in called:
        obj, _, attr = called.partition(".")
        if obj in alias_map:
            target = _make_id(alias_map[obj], attr)          # m.f() cross-module
        elif obj in symbol_map or obj in ("self", "cls"):
            target = _resolve_call(file_id, called)          # keep today's phantom
            member = attr                                    # dispatch candidate
        else:
            target = _resolve_call(file_id, called)
            member = attr                                    # dispatch candidate
    else:
        target = _resolve_call(file_id, called)
    edge = _edge(scope_id, target, "calls", rel_path, _line(node), confidence="LOCAL_CALL")
    if member: edge["_member"] = member
```

(Exact code in the plan; the spec point is the ORDER: symbol map → alias map
→ member-dispatch stash → today's fallback. Only single-attribute chains
`a.b` bind via alias map; deeper chains `a.b.c` stash the final attribute as
`_member` like TS does.)

Amendment (matches the as-implemented code, not the pseudocode above): the
pseudocode gives `self`/`cls` attribute calls their own arm ahead of the
noise filter, implying they always get a member-dispatch stash. The shipped
code instead routes `self.x()` / `cls.x()` through the same
`should_keep_call_target` noise filter as any other attribute call — a
noisy-named method (e.g. `self.get()`) emits no edge at all, even though
`self` unambiguously names the owning class. This is an accepted tradeoff:
filtering by member name only, with no special case for the unambiguous
receiver, keeps the noise list a single source of truth and avoids
phantom/mis-dispatch edges for high-fan-out names, at the cost of dropping
a small number of legitimately-resolvable self/cls calls.

## 5. Mechanism 3 — Python member dispatch

- Python `function_definition` nodes whose `parent_id` is a class node get
  `is_method=True` in `_node(...)` (mirrors TS `method_definition` tagging).
- The Python call branch stashes `_member` per §4 for attribute calls not
  bound by the alias map (incl. `self.x()` / `cls.x()`).
- The existing `_resolve_method_dispatch` post-pass (language-agnostic:
  operates on `_member` + `is_method` + `known_ids`) re-points those edges to
  matching method definitions under the existing
  `_MAX_METHOD_DISPATCH_CANDIDATES` cap, keeps real-target edges, and DROPS
  unresolved member-call phantoms — the same policy TS ships with. Expected
  side effect: substantial reduction of `kind: unknown` placeholder nodes in
  Python graphs (today's `self.foo`/`obj.foo` phantoms).

## 6. Health metric amendment (block schema 2)

`resolution_health(g)` changes:

- `"schema": 2`.
- Every `imports` cell (top-level and per-language) gains
  `"external": <count>`; edges with `confidence == "EXTERNAL_IMPORT"` are
  counted there and EXCLUDED from that cell's `total`/`bound`/`ratio`.
- `calls` cells gain no external field (builtin/noisy calls are already
  filtered at extraction; member-phantom drops further clean the residue).
- `healthy` rule unchanged in form (all non-null ratios ≥ 0.8) — but ratios
  now measure only should-bind-in-repo edges.
- Docs updated: `docs/agent-integration.md` §7 (schema 2, `external` field,
  changed denominator semantics — call out that pre-change graphs report
  schema 1 and consumers that read ratios should branch on the schema field) and the query-result schema property description.
- Old graphs (schema 1) remain valid; consumers that only read `healthy`
  (the recommended pattern) are unaffected.

Note for tests: after this round the trust-signal tests that fabricate
unhealthy graphs must tag their import edges (untagged = counted, so existing
fixtures stay valid — untagged edges have no `confidence` attr and are
treated as in-repo/countable by design: absence of evidence is not
externality).

## 7. Cache + engine identity

`Config.cache_version` bumps `"v6"` → `"v7"` (comment: "v7: python
import/file-node resolution, import maps, python method dispatch").
This invalidates all cached extractions on first post-upgrade build (full
re-extract; daemon builds pick it up automatically via children). The
`engine_identity` already incorporates cache_version — `check` will report
`engine_changed` staleness on old graphs, prompting rebuilds.

## 8. Testing

- **resolve_python_module unit tests**: root layouts ("" and "src"),
  package `__init__.py`, relative dots (1..3), bare relative import,
  unresolvable stdlib, local-shadowing-stdlib, non-Python rel_paths ignored.
- **Import-edge tests**: absolute from-import (one edge, EXACT_IMPORT, file
  node target), plain import, aliased import, relative import, stdlib import
  (EXTERNAL_IMPORT + phantom target), NO per-name edges for from-imports.
- **Symbol/alias map tests**: `from p import m` module-vs-symbol
  disambiguation; `import m as x`; function-local imports collected; last
  binding wins; unresolvable → absent.
- **Call binding tests** (aramid-shaped fixture: src-layout mini repo):
  bare from-imported call binds cross-module; `m.f()` module-attr call binds;
  `instance.method()` binds via dispatch; `self.helper()` binds to own class
  method; unresolvable member phantom dropped; builtin calls still filtered;
  same-file behavior unchanged.
- **is_method tagging**: methods tagged, top-level functions not, nested
  functions in methods not double-tagged.
- **Health schema 2 tests**: external counted + excluded; untagged counted;
  mixed graph ratios; healthy flips when externals excluded; schema field.
- **End-to-end**: build the mini repo → `callers`/`impact`/`imported-by`
  return the real cross-module answers; ratios ≥ 0.8 → `healthy: true`;
  bound-edge assertions on the persisted bundle.
- **Cleanup-task tests** (§9): context note, independent golden pin,
  neighbor empty+unhealthy, hook non-bool healthy, context full-block.
- Full suite green; ruff clean.

## 9. Cleanup task (deferred Minors from the trust-signal round)

1. `format_context_markdown`: non-empty impact + unhealthy graph → trailing
   `note: resolution health low (imports X%, calls Y%) — this list may be
   incomplete.` (mirrors `cmd_impact`), + test.
2. `tests/test_call_graph.py` golden pin: pin the expected
   `resolution_health` block as a literal (schema-2 shape), not via the
   function under test.
3. Coverage nits: `_neighbor_listing` empty+unhealthy direct test
   (`depends-on`), strict-hook non-bool `healthy` test, context full-block
   assertion.

## 10. Acceptance (post-merge, live, no approval-gated surfaces)

On aramid (rebuild, then):
- A1: `query "callers auto_resolve_tdd"` includes `pipeline` (cross-module).
- A2: `query "callers record_run"` includes pipeline, drain, init callers.
- A3: `query "callers scan_scoped"` includes BOTH red_proof.scan and the
  pipeline caller.
- A4: `stats` ratios materially up (imports in-repo ratio expected near 1.0;
  calls substantially above 26%); if both ≥ 0.8, `healthy: true` and the
  strict gate re-arms (no action needed — automatic).
- `impact src/aramid/ledger.py` lists real dependents and test modules
  (no INCONCLUSIVE if healthy).
On graphite's own repo: rebuild + spot-check `callers resolution_health`
finds its cross-module callers (query.py/cli.py/context.py/analyze.py).
Then: notify aramid's agent via the established reply-file channel
(Gate A status update; ratios before/after; whether `healthy` flipped).

## 11. Rollout

No template/DOC_VERSION change this round. Consumer repos need only a
rebuild, which daemons do automatically on next change (or `graphite build`
manually during acceptance). Memory + spec/plan status updates at close.
