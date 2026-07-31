# Rust import resolver — design

Date: 2026-07-31
Status: approved for implementation planning

## Problem

Rust `use` targets never bind. `extract/ast.py:1321-1328` emits an `imports`
edge to `_make_id(target)` — a synthetic id built from the raw text, e.g.
`crate::policy::Rule` — which matches no real node. There is no resolver, so
the comment there deliberately keeps the confidence at `EXTRACTED` rather than
`EXTERNAL_IMPORT`.

Measured on `F:\Projects\operation-firewall` (3 crates, Rust-dominant),
2026-07-31:

```
by_relation.imports : total 8, bound 0, ratio 0.0,  external 0
by_relation.calls   : total 211, bound 208, ratio 0.986, external 20
healthy: False
```

Consequences, both verified live:

1. `graphite doctor` reports the repo **degraded**, and every `context` /
   `impact` on a Rust file grades **INCONCLUSIVE** — five such incidents were
   logged by Codex on 2026-07-31.
2. **Strict graph-first enforcement is disarmed repo-wide.**
   `agent_hooks.py:322-346` only issues `permissionDecision: deny` when
   `resolution_health.healthy is True`; otherwise it degrades to a reminder
   plus `STRICT_SUSPENSION_NOTE`.

One cell with a denominator of 8 is disabling enforcement across the repo.

## Verified constraints

These were read from the code, not assumed, and the design depends on them.

- **`healthy` is aggregate.** `health.py:106`:
  `all(ratio >= 0.8 for ratio in ratios)` where `ratios` comes from
  `by_relation` and **skips cells whose `total == 0`** (`ratio is None`).
  OF's aggregate `imports` cell is entirely Rust — Python's 11 imports are all
  `EXTERNAL_IMPORT` and already excluded.
- **"Bound" means the target node is real**, not that the edge is tagged.
  `health.py:82`: `bound = int(g.nodes[v].get("kind","unknown") != "unknown")`.
  A resolver that only re-tags confidence changes nothing; it must point the
  edge at an existing node id.
- **`EXTERNAL_IMPORT` only excuses an unbound edge** (`health.py:83`) and
  removes it from `total` entirely. It never demotes a bound edge.
- **Zero-cell answers do not grade `decision_grade`** (issue #12). A scoped
  answer whose cells are all empty grades `advisory` when non-empty and
  `inconclusive` when empty.

The fourth point is the sharpest design constraint: **a change that raises the
ratio by removing edges can flip the aggregate green while leaving the actual
answers no better.** The objective is therefore *maximising bound in-repo
edges*, not maximising the ratio.

## Goals

- Bind in-repo Rust `use` targets to real file nodes.
- Classify genuinely external targets as `EXTERNAL_IMPORT`, precisely.
- Emit module-structure edges for `mod` declarations, which currently produce
  nothing at all.
- Leave a genuinely unresolvable import as `EXTRACTED` so it stays visible to
  `resolution_health`.

## Non-goals

- **Binding `use` to symbol nodes** rather than file nodes. More precise, but
  inconsistent with Python/TS imports and materially riskier. Deferred; it is
  the prerequisite for later binding `Rule::new()` cross-crate.
- `#[path = "..."]` attributes and custom `mod` paths.
- Reading Cargo lockfiles, resolving versions, or any network/registry access.
- Any change to Go extraction.

## Design

### 1. `SourceIndex` additions (`resolve.py`)

Mirrors the existing `_load_workspace_packages` / `_load_tsconfig_aliases`
pattern: read manifests found in the already-scanned file set, at index build
time.

```python
# (crate_name_normalised, crate_dir_rel, src_root_rel)
cargo_crates: tuple[tuple[str, str, str], ...] = ()
# dependency names, normalised, unioned across all manifests
cargo_dependencies: frozenset[str] = frozenset()
```

- `_load_cargo_crates(root, rel_paths)` — for every `Cargo.toml` in
  `rel_paths`, parse with `tomllib` (stdlib; `requires-python >= 3.11`), read
  `[package].name`, normalise `-` → `_` (Cargo's own crate-name rule), and
  record its `src/` root. Skip virtual manifests (workspace-only, no
  `[package]`).
- `_load_cargo_dependencies(root, rel_paths)` — union of keys under
  `[dependencies]`, `[dev-dependencies]`, `[build-dependencies]`, normalised.
- Both must be total: a malformed or unreadable manifest is skipped, never
  raised. Extraction must not fail because a repo has a bad `Cargo.toml`.

### 2. `SourceIndex.resolve_rust_use(importer_rel_path, use_path, inline_mod_depth)`

Returns `ResolvedImport(rel_path, confidence)` for a bound target, a sentinel
for external, or `None` for unresolved.

**Step 1 — locate the importer's crate.** The longest `crate_dir_rel` that
prefixes `importer_rel_path`. If none, only the external allowlist applies.

**Step 2 — classify the first segment.**

| First segment | Outcome |
| --- | --- |
| `std`, `core`, `alloc`, `proc_macro` | `EXTERNAL_IMPORT` |
| `crate` | anchor = importer crate's `src/` |
| `self` | anchor = the importer's module directory (defined under §3) |
| `super` | `inline_mod_depth > 0` → the importer's own file; else the parent module — the containing directory of the importer's module directory |
| matches a `cargo_crates` name | anchor = that crate's `src/`, target defaults to its `lib.rs` |
| in `cargo_dependencies` | `EXTERNAL_IMPORT` |
| anything else | `None` — unresolved, stays `EXTRACTED` |

**Step 3 — walk the remaining segments.** A `use` path blends module path and
item name with no lexical boundary, so generate candidates **longest-prefix
first** and take the first present in `rel_paths`. For `crate::a::b::C`:

```
<src>/a/b/C.rs, <src>/a/b/C/mod.rs,
<src>/a/b.rs,   <src>/a/b/mod.rs,
<src>/a.rs,     <src>/a/mod.rs,
<src>/lib.rs
```

**CamelCase cutoff:** stop extending the module path at the first segment
beginning with an uppercase letter. Rust modules are `snake_case` and items are
`CamelCase`, so `C` above is an item, not a directory. This narrows the search
and reduces mis-binding.

### 3. `mod` declarations

`mod_item` has no branch in the Rust walk today, so `mod foo;` emits nothing —
meaning a multi-file crate has **no module-structure edges at all**. Add:

- **Non-inline `mod foo;`** (no body) → `imports` edge to
  `<module_dir>/foo.rs` or `<module_dir>/foo/mod.rs`. Unambiguous; no prefix
  search needed.
- **Inline `mod foo { ... }`** (has a body) → no edge; recurse with
  `inline_mod_depth + 1`.

**Definition — the module directory of a file**, used by both `mod` resolution
and `self::` anchoring. This is the directory Rust searches for that file's
child modules:

| File | Module directory |
| --- | --- |
| `src/lib.rs`, `src/main.rs`, `src/a/mod.rs` | its own containing directory (`src/`, `src/a/`) |
| any other `src/a/foo.rs` | `src/a/foo/` — a directory named after the module |

So `mod bar;` in `src/lib.rs` resolves to `src/bar.rs` or `src/bar/mod.rs`,
while `mod bar;` in `src/foo.rs` resolves to `src/foo/bar.rs` or
`src/foo/bar/mod.rs`.

### 4. Extractor changes (`extract/ast.py`)

- `_extract_rust` gains a `source_index: SourceIndex | None = None` parameter.
  Dispatch at `:1456-1459` currently passes `source_index` only to
  `_extract_python`; Rust must be called with it too. Go is unchanged.
- `walk` threads `inline_mod_depth` alongside the existing `in_impl`.
- The `use_declaration` branch calls the resolver and emits, in order of
  precedence:
  - resolved in-repo → edge to the resolved **file node id**, default confidence;
  - external → edge to the synthetic id, `confidence="EXTERNAL_IMPORT"`;
  - unresolved → today's behaviour exactly (synthetic id, `EXTRACTED`).
- **Self-referential import edges are dropped** — an edge from a file to
  itself carries no dependency information. This is a general rule, not an
  inline-module special case, and is what removes the `use super::…` edges
  inside `#[cfg(test)] mod tests`.
- With `source_index is None` (its default), behaviour is byte-identical to
  today. This keeps every existing direct-call test valid.

## Testing

Unit (`resolve.py`):
- each first-segment class in the table above;
- `crate::`/`self::`/`super::` anchoring, including `super` at
  `inline_mod_depth` 0 vs > 0;
- longest-prefix ordering, and the CamelCase cutoff;
- workspace crate name normalisation (`ofw-contracts` → `ofw_contracts`);
- malformed / virtual / unreadable `Cargo.toml` is skipped, not raised.

Extraction (`ast.py`):
- `use std::…` → `EXTERNAL_IMPORT`;
- a declared third-party dep → `EXTERNAL_IMPORT`;
- an in-repo cross-crate `use` → bound to the real file node;
- **an unresolvable `use typo_crate::Thing` stays `EXTRACTED`** — the explicit
  guard against laundering misses as "external";
- `mod foo;` → edge to `foo.rs`; inline `mod tests { }` → no edge;
- `source_index=None` → output identical to current behaviour.

Live acceptance on Operation Firewall — see below.

## Acceptance criteria

1. graphite's own suite green as the bare `pytest` console script, and
   `python -m ruff check .` clean.
2. On OF: `by_relation.imports.ratio` rises from `0.0`, `by_language.rust`
   imports binds its in-repo edges, and `healthy` becomes `True`.
3. On OF: `context` and `impact` on `crates/ofw-*/src/lib.rs` grade
   `decision_grade` rather than `INCONCLUSIVE`.
4. `graphite doctor` on OF no longer reports the imports-driven degradation.
5. A deliberately unresolvable import is still counted against the ratio —
   verified by test, not by inspection.

## Known limitations, accepted

- **OF ends with only ~2 in-repo Rust import edges.** Its three crates are each
  a single `lib.rs`, so the imports cell stays thin and one bad import could
  re-break `healthy`. Inherent to the repo's shape; it becomes robust as OF
  grows into multi-file crates, which is when `mod` edges start carrying
  weight. This is the reason `mod` support is in scope now rather than later.
- **`Cargo.toml` *content* changes do not invalidate the extraction cache.**
  `file_set_digest` hashes rel-path names only. Pre-existing and shared with
  `tsconfig` aliases; matched deliberately rather than inventing a new
  mechanism. Adding a dependency then rebuilding without touching a `.rs` file
  can serve a stale classification.
- **Dependency names are unioned across manifests**, not scoped per crate, so a
  crate could classify as external a dependency it does not itself declare.
  Accepted for simplicity; the failure mode is a slightly over-generous
  external tag, never a wrong binding.
- `use foo::*` and `pub use` resolve as ordinary `use`.
- `use super::super::X` escaping past the inline-module depth is not modelled.
