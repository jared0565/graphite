# External-call classification (health schema 3) — design

**Issue:** #4 — "TypeScript calls-binding at 0.667 (healthy:false)".
**Status:** design approved 2026-07-26. Branch `feat/external-call-classification`.

## 1. Problem

`resolution_health` reports a `calls` bound-ratio that is read as *resolver
health*. It is not. It measures **allowlist coverage and external-call density**
at least as much as binding difficulty, and the two are not separable today.

`imports` already solved this: an import that no resolver could bind in-repo is
tagged `confidence="EXTERNAL_IMPORT"`, counted in a separate `external` field,
and excluded from the ratio (`health.py:71-74`, schema 2). **`calls` has no
equivalent.** Every call into `node_modules`, every test-framework injected
global, and every language builtin missing from one hand-maintained allowlist
counts as a resolver failure.

TypeScript pays the most because TS code calls into external packages
constantly, and because vitest/jest inject `expect`/`it`/`describe` as globals
that are never imported at all.

## 2. Evidence

Corpus sweep recorded on issue #4: 8 of 9 TS repos below the 0.8 threshold,
weighted TS `calls` **0.584** (40127/68662); every Python repo ≥ 0.86.
Load-bearing repos: pawscout 0.667, open-design 0.562, pivot-parlor 0.562,
BytesCRM 0.927.

The resolver was **ruled out** as the variable: pawscout rebuilt with
`--typescript-resolver` = `disabled`, `compiler`, and `auto` (separate output
and cache dirs, `metadata.typescript_resolver` confirmed each took effect)
returned byte-identical results all three times. So the original title's
diagnosis — "the 2026-07-25 cross-module fix was Python-only" — is **refuted**.
The observation is real; the cause is classification, not resolution.

Measured 2026-07-26 on current `main` (`d43b88b`) with a scratch classifier.
**The `external` bucket uses substring matching against module ids and is
indicative, not exact** — it cannot see TS named imports at all, which is
itself the bug (§4.1):

| | pawscout (TS) | graphite (Python) |
|---|---|---|
| calls / unbound | 9690 / 3224 → **0.667** | 8922 / 926 → **0.896** |
| injected test globals | 2491 (77%) — `expect` 1806, `it` 456 | 0 |
| externally imported | ~0 detected | 261 — `Path` 181, `dataclass` 54 |
| residual "in-repo" | 733 — `store`, `applyD1Migrations` | 665 — incl. `ValueError` 146, `frozenset` 64 |
| ratio excluding externals | **0.898** | **0.923** |

Two findings the original triage did not have:

- The **Python builtin allowlist is also incomplete** — `ValueError` (146),
  `frozenset` (64), `AssertionError` (39), `OSError` (36) are counted as
  resolver failures in graphite's own graph. This is not a TypeScript problem
  wearing a TypeScript hat; TS is just where it is worst.
- `setTimeout` appears in the same residual bucket, so **JS runtime globals**
  are a third source alongside test injections and package imports.

**Critically: the noisy calls already exist as edges.** pawscout's unbound
targets are file-scoped phantoms (`tests_import_test_expect`,
`tests_blog_test_expect`, …). Relabelling them changes no edge count.
*Adding* them to the drop-list would delete ~2500 edges. That yields the *same
ratio* — a dropped edge and an excluded edge are both outside the denominator —
but it destroys the evidence: no `external` count, no edge in `graph.json`, and
no way for a reader to audit what the denominator excluded or why.

## 3. Scope

**In:** a new `EXTERNAL_CALL` edge confidence; two classification sources;
health schema 3 with `external` on `calls` cells; caveat-registry update;
cache `v8→v9`; agent-facing doc corrections; rollout to the five managed
consumers.

**Out:**
- Issue #12 (zero-health-cell grading). It is the **next** round. Excluding
  external calls can take a language's cells from *degraded* to *absent*,
  which is exactly #12's trigger — so #12 wants to be designed against the
  post-#4 world, and bundling would put the denominator, the grade vocabulary,
  and the renderer all in motion at once.
- Destructured locals (`const { foo } = require(...)`, then `foo()`).
  Unfixed; see §7.
- Issue #11 (`context` neighbour cap).
- Re-pointing external-call edges to shared external nodes (D4).
- Chasing every repo over 0.8 (D5).

## 4. Design

One new edge confidence value, `EXTERNAL_CALL`, assigned at extraction time.
`health.py` excludes it from `calls` `total`/`bound`/`ratio` and reports the
count in a new `external` field — structurally identical to what
`EXTERNAL_IMPORT` does for `imports`.

**Invariant: nothing is deleted, and every exclusion stays visible.** No
`calls` edge is removed or re-targeted by this round, and no name moves onto a
drop-list. What keeps the ratio honest is that an excluded call remains
present and countable in `graph.json`, so the denominator can never shrink
silently.

**Amended 2026-07-27 (was "pure relabel").** The original invariant claimed
edge counts are identical before and after. That was **false**, and the round
discovered it during Task 2: `_resolve_method_dispatch`
(`ast.py:1186-1196`) already **drops** a member call whose target resolves to
no known definition, so `z.object()` / `crypto.randomUUID()` were never counted
against the ratio — they did not exist as edges at all. Per operator decision
(D7) those calls are now **retained** as `EXTERNAL_CALL` instead of dropped, so
this round **adds** edges. The retention is narrow: a member call is kept only
when its root is a known external binding (an unresolved import or an entry in
`_EXTERNAL_GLOBALS`). An unattributable receiver — `c.json()`, `db.prepare()`,
`stmt.bind()`, the framework-noise population the filter was built to remove —
is still dropped.

Consequences that must be tracked, not assumed away:

- `calls` `total`/`bound`/`ratio` are unaffected by the retained edges (they are
  external and unbound, so §5 excludes them), but `external` counts rise.
- `placeholder_nodes.unknown` and `share` **worsen**, because each retained edge
  materialises its phantom target as an `unknown` node.
- Graph size grows on TS repos by the attributable-external-member-call
  population. That number is unmeasured — acceptance A1 reports it.

`_edge` (`ast.py:100-121`) already takes `confidence`, defaulting to
`"EXTRACTED"`; call sites currently pass `"LOCAL_CALL"` explicitly. The change
is to pass `"EXTERNAL_CALL"` instead when a call classifies as external.

### 4.1 Source A — import-derived

Both resolvers discard exactly the information needed:

- `_collect_ts_import_symbols` (`ast.py:317-347`) — `if resolved is None:
  continue` at `:342`. A symbol imported from an unresolved module is dropped
  from the map, so its call falls through to a file-scoped phantom.
- `_collect_python_import_maps` (`ast.py:569-638`) — docstring `:576`:
  "Unresolvable modules enter neither map."

Each gains a third output: **the set of local names bound by imports that did
not resolve to an in-repo file.** A call classifies as `EXTERNAL_CALL` when:

- the call is a bare identifier and that identifier is in the set; or
- the call is a member expression and its **root** object is in the set.

The member-root rule is required and cannot reuse existing machinery:
`_resolve_call` (`ast.py:967-981`) only consults `import_symbols` when
`"." not in called` (`:974`), so dotted targets never touch the import map.
`z.object()` must be classified by testing the root `z`, separately.

**TypeScript must additionally collect default and namespace imports.**
`_iter_named_imports` (`ast.py:350-368`) walks only `named_imports`, so
`import z from 'zod'` and `import * as z from 'zod'` are invisible today. For
*resolution* that omission is deliberate and stays (those forms can't be tied
to a single named definition). For *external classification* all three binding
forms must be collected, because the question is only "was this name bound by
a non-in-repo import", which every form answers.

Applies to **TypeScript/JavaScript and Python only** — the only languages with
import resolvers. Go's importer is explicitly resolver-free (`ast.py:783-789`)
and Rust has no equivalent, so no import-derived classification is possible
there; those languages get Source B only.

### 4.2 Source B — global-derived

A new frozenset `_EXTERNAL_GLOBALS`, checked at the same guard point as the
existing drop-list, covering names that are **never imported**:

- test-framework injections — `expect`, `it`, `describe`, `test`, `vi`,
  `jest`, `beforeEach`, `afterEach`, `beforeAll`, `afterAll`, `suite`, `xit`,
  `xdescribe`, `fit`, `fdescribe`;
- JS/Web runtime globals absent from the drop-list — `setTimeout`,
  `setInterval`, `clearTimeout`, `clearInterval`, `fetch`, `queueMicrotask`,
  `structuredClone`, `atob`, `btoa`, `crypto`, `performance`, `process`,
  `Buffer`, `require`;
- Python builtins absent from the drop-list — `ValueError`, `OSError`,
  `AssertionError`, `KeyError`, `IndexError`, `RuntimeError`,
  `NotImplementedError`, `StopIteration`, `frozenset`, `bytearray`, `complex`,
  `object`, `Exception`, `BaseException`, `property`, `staticmethod`,
  `classmethod`, `slice`, `divmod`, `format`.

The guard shape is identical in all four extractors — TS/JS `ast.py:276`,
Python `:730`, Go `:834`, Rust `:934` — so Source B applies uniformly at one
point per extractor.

These lists are **exact and required**, not illustrative: an implementer uses
these names verbatim. Generic words that a repo plausibly defines itself —
`context`, `run`, `setup`, `main` — are deliberately excluded even though some
test frameworks inject them, because the cost of a false external is masking
real code (see the guard below).

**Masking risk and its guard.** Source B matches on name alone, with no scope
awareness. A repo that defines its own `test()` or `process()` would have those
calls tagged external — and a same-file definition *does* bind today, because
`_resolve_call` returns `_make_id(file_id, called)`, which matches the local
node. Excluding a **bound** edge would hide real binding from the ratio, which
is the exact failure this round exists to remove. §5's exclusion rule prevents
it: externality only ever excuses an *unbound* edge.

### 4.3 Which list does a name belong in?

**This round moves no name into or out of `_LANGUAGE_BUILTIN_GLOBALS`.**
Already-dropped names stay dropped; newly-classified names are tagged.

The health ratio is *identical* under either treatment — a dropped edge is
**absent** from the denominator, a tagged edge is **excluded** from it — so
the split costs nothing in measured accuracy. The only difference is graph
visibility, and preserving today's drop behaviour is what makes the
pure-relabel invariant (§4) provable.

This is a scoping decision, not a principle. It leaves a cosmetic
inconsistency: `TypeError` is dropped (it is in the existing list) while
`ValueError` is tagged. Both are excluded from the ratio; only their presence
in `graph.json` differs. Unifying the two lists is a follow-up (§14).

## 5. Health block — schema 3

In `health.py`:

- `_cell` (`:43-51`) emits `external` for **both** counted relations, not just
  `imports`.
- The exclusion test (`:71-74`) generalises from a hardcoded `imports` /
  `EXTERNAL_IMPORT` check to a relation → external-confidence mapping:
  `{"imports": "EXTERNAL_IMPORT", "calls": "EXTERNAL_CALL"}`.
- `schema` (`:87`) becomes `3`.

**Exclusion rule — externality only excuses an unbound edge.** An edge counts
as `external` iff its confidence is the relation's external marker **and** its
target is unbound (`kind == "unknown"`). An external-marked edge whose target
*did* bind is counted normally, in both numerator and denominator.

This is what makes §4.2's name-based matching safe: the worst a false external
can do is fail to exclude, never hide real binding.

For `imports` it is a behaviour change, though an empirically inert one. Schema 2
excluded every `EXTERNAL_IMPORT` edge *unconditionally*; schema 3 excludes only
the unbound ones, so a bound `EXTERNAL_IMPORT` edge now counts in `total` and
`bound`. Such an edge is possible — `EXTERNAL_IMPORT` targets are
`_make_id(module)` phantoms, and a module name can collide with a real file's
node id (`json` ↔ `src/json.py`) — so this is **not** structurally impossible,
only currently absent: graphite's own graph carries 908 `EXTERNAL_IMPORT` edges,
all unbound, verified 2026-07-27. No published imports number moves today.
Stating the rule for both relations keeps one law instead of two, and
`test_external_import_that_bound_is_counted_normally` pins the behaviour.

Unchanged: `RESOLUTION_HEALTHY_RATIO` (0.8), the `healthy` rule, the
`placeholder_nodes` block, `ratio_percent`, and `persisted_resolution`.

Cross-schema ratio comparison is **invalid** — a schema-2 `calls` ratio
includes externals and a schema-3 one does not. Consumers must branch on
`schema`, which is the rule already established for schema 2.

## 6. Cache and engine identity

`cache_version` (`config.py:32` and the env default at `:159`) goes `v8→v9`,
because edge `confidence` values are extraction output and the AST cache holds
them. `engine_identity` already incorporates `cache_version`
(`engine_identity.py:173`), so existing graphs correctly report
`stale (engine_changed)` and re-extract on the next build.

## 7. Caveat registry

`ts-external-calls-unclassified` (`answer_contract.py:37-42`) reads: "calls to
external-package symbols, runtime globals, **and destructured locals** count as
unbound." This round fixes the first two only.

The registry's process rule (`:25-27`) is that a published code's meaning never
changes — fixed classes get `retired_by` and are never emitted again. So:

- **Retire** `ts-external-calls-unclassified` with `retired_by: "2026-07-26"`.
- **Add** a narrower successor, `ts-destructured-locals-unbound`, relations
  `("calls",)`, languages `("typescript", "javascript")`, summarising that
  calls through destructured local bindings still count as unbound.

Retiring the old code without a successor would silently drop a live blindspot;
amending its summary in place would change what a published code means for
consumers that cached it.

## 8. Documentation

- `docs/agent-integration.md:109-149` — the health-block shape, the `schema 2`
  paragraph (`:133-138`), and the `external` definition (`:135`) all need the
  `calls` case.
- **`agent-integration.md:146-149` currently tells consumers the `calls` ratio
  is "the discriminating health signal" precisely because `imports` is
  saturated by construction.** This round partially invalidates that: `calls`
  also trends up once externals are excluded. That guidance must be corrected
  rather than left to contradict the new numbers.
- The machine-level `CLAUDE.md` doctrine carries the same "calls ratio is the
  discriminating signal" claim and the "TypeScript `calls` is systemically
  under-bound" block; both need updating after acceptance (§12).
- `DOC_VERSION` (`init.py:17`, currently 9) bumps only if managed template
  text changes; `test_template_change_requires_doc_version_bump` pins that
  pairing.

## 9. Testing

Unit, per language:

- A call to a named / default / namespace import from an **unresolved** module
  is tagged `EXTERNAL_CALL` (TS); from `import x`, `import x as y`, and
  `from m import n` where `m` is unresolvable (Python).
- A member call whose **root** is externally bound is tagged (`z.object()`).
- A call to an **in-repo** symbol is still `LOCAL_CALL` and still binds.
- A name in `_EXTERNAL_GLOBALS` is tagged, in all four extractors.
- A name in `_LANGUAGE_BUILTIN_GLOBALS` is still **dropped** (no edge).

Health:

- `EXTERNAL_CALL` edges are excluded from `calls` `total`/`bound`/`ratio` and
  counted in `external`.
- `schema == 3`; `imports` behaviour byte-identical to schema 2.
- A `calls` cell with only external edges reports `ratio: null`, not `0.0`.
- **An `EXTERNAL_CALL` edge whose target is bound is counted normally**, in
  both numerator and denominator — §5's rule. This is the masking guard;
  without a test it will regress to a plain confidence check.

Invariant and falsifier:

- **Edge-count regression:** total edges and nodes on a fixture are identical
  before and after classification. This is the guard on §4's pure-relabel
  invariant.
- **The falsifier that matters:** a fixture containing a genuine in-repo
  binding miss must **still** count as unbound and must still drag the ratio
  below threshold. It is easy to write a version of this change that tags
  everything and reports 1.00; a test suite that only checks "ratio went up"
  cannot tell that apart from success.

## 10. Live acceptance

Run pre-merge, falsifier stated before each run.

- **A1** — pawscout `calls` 0.667 → **≥ 0.89**. The edge count is expected to
  **rise**, not hold, because D7 retains external member calls the phantom
  filter used to drop. **Record the before/after edge count, node count, and
  `placeholder_nodes.share` explicitly** — this is the round's only measurement
  of how much the graph grew, and A1 fails if the edge count *drops*, which
  would mean deletion rather than classification.
- **A2** — graphite's own `calls` 0.896 → **≥ 0.92**, `python` cell reports a
  non-zero `external`. The bar comes from §2's indicative measurement (0.923
  excluding the detected externals alone) and should be *beaten*, because the
  285 mis-bucketed Python builtins (`ValueError`, `frozenset`,
  `AssertionError`, `OSError`) are also excluded under §4.2. If the measured
  result lands near 0.92 rather than above it, the builtin list did not take
  effect — investigate rather than accept.
- **A3** — open-design and pivot-parlor (0.562, the import-derived cases) move
  materially. Recorded, not required to cross 0.8 (D5).
- **A4** — a graph with a real in-repo binding gap still reports
  `healthy: false`. Discriminating in both directions.
- **A5** — `schema` reads 3 everywhere it is published: `stats`, `impact`,
  `context`, relation-verb JSON, `check --json`, `graph.json`
  `analysis.resolution_health`, and `.graphite_analysis.json`.
- **A6** — full suite green, exit code read directly (never through a pipe).

## 11. Decisions

- **D1** — One `EXTERNAL_CALL` value for both classification sources, rather
  than separate values per provenance. Provenance is recoverable from the
  graph (an externally-imported name has a matching `EXTERNAL_IMPORT` edge in
  the same file); a second value would fragment the health mapping for no
  consumer benefit.
- **D2** — Relabel, don't drop. Rejected the cheaper "add `expect` to the
  drop-list" fix: it improves the ratio by deleting evidence, cannot report
  `external` for calls, and does nothing for the import-derived repos.
- **D3** — Two allowlists with adjacent semantics, per §4.3, accepted with the
  inconsistency documented rather than resolved.
- **D4** — External-call edges keep their current file-scoped phantom targets.
  Re-pointing them to shared external nodes would also reduce
  `placeholder_nodes.unknown`, but that changes node counts and breaks the
  pure-relabel invariant that makes this round auditable. Separate concern.
- **D5** — Acceptance requires movement and an unchanged edge count, **not**
  that every repo crosses 0.8. A repo still below threshold after
  classification is reporting a genuine binding gap; that is a true finding
  and a follow-up, not a failure of this round.
- **D6** — Retire-and-replace the caveat rather than amend it in place (§7).
- **D7** (operator decision, 2026-07-27) — **Retain attributable external member
  calls instead of dropping them.** `_resolve_method_dispatch` drops member
  calls that resolve to no known definition, which made the member-root branch
  of `_call_confidence` unobservable: a surviving member call is bound, and §5
  never excludes a bound edge, so the branch could not change any number.
  Three options were put to the operator — keep the rule and test it on bare
  calls only, drop the rule, or make it observable. The operator chose to make
  it observable, accepting that the round now adds edges and grows TS graphs.
  Rationale: "record, don't delete" is the round's governing principle, and a
  call into a known external package is exactly the evidence the health block
  should be able to account for. The narrowing to *attributable* roots is what
  keeps the original filter's purpose intact.

## 12. Rollout

1. Merge to `main`.
2. Re-init the five managed consumers (`aramid`, `BytesAI Learning`,
   `misc\Medication Reminder`, `demo-store2`, `demo-store2\pawscout-worker`)
   via `graphite init`, rebuild each graph, and record the schema-3 `calls`
   ratio for each.

   **Do not treat `init`'s exit code as evidence that a consumer was updated.**
   `init` cannot upgrade a doc it classifies as *legacy unversioned*: it reports
   the file, changes nothing, and exits 0 (issue #13, reproduced 2026-07-26 —
   `git diff` byte-identical before and after). The rollout's acceptance check
   is a **marker survey**, not `init`'s output:

   ```
   grep -l 'graphite:managed version=' <repo>/{GRAPHITE,CLAUDE,AGENTS,ANTIGRAVITY}.md \
     <repo>/.github/copilot-instructions.md
   ```

   Every managed doc must report the current `DOC_VERSION`. A repo with a legacy
   doc silently never receives template changes — which is how two of the five
   consumers came to be missing the graphite-first and answer-contract text while
   being believed current.

   Audited and remediated 2026-07-26, ahead of this round: all five consumers ×
   five docs now carry `version=9` (25/25). Repo-specific content was preserved
   by hand where legacy docs held it — notably `pawscout-worker/CLAUDE.md`, which
   carried an owner rule about operational Shopify Admin API access.
3. **Restart the daemon.** Extraction is a daemon-executed surface and this
   round changes it, which meets the operator rule for a restart. The daemon
   spawns fresh `-m graphite build` children and holds no cache-version state,
   but it does hold extraction code in memory.
4. Correct the machine `CLAUDE.md` doctrine (§8) using the *measured*
   post-rollout numbers, not the projections in this spec.

## 13. Follow-ups

- Issue #12 — zero-health-cell grading. Next round; design against schema 3.
- Issue #13 — `init` cannot upgrade legacy unversioned docs. Filed during this
  round's consumer audit; §12 works around it with a marker survey, but the
  workaround is manual and every future template change inherits the same trap.
- Destructured-local call binding (the new caveat's subject).
- Unify `_LANGUAGE_BUILTIN_GLOBALS` and `_EXTERNAL_GLOBALS` into one list with
  one behaviour (§4.3).
- Node-level consolidation of external-call phantoms (D4), which would improve
  `placeholder_nodes.share`.
- Re-run the 9-repo corpus sweep under schema 3 and republish the weighted TS
  figure, replacing the 0.584 recorded on issue #4.
