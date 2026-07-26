# Honest Answer Contract — Design

Date: 2026-07-26
Status: approved design, pre-plan
Branch: `feat/honest-answer-contract` (base `36e2528`)
Origin: aramid triage of issues #4–#9 (2026-07-26); operator requirement:
"when the graph is queried, it will always provide sensible, accurate,
reliable, and defensible information."

## 1. Problem

Two proven failure classes let a graph answer look authoritative while
being wrong or unexplained:

1. **Bound-but-wrong-target edges are invisible to `resolution_health`.**
   `from aramid import pipeline` binds its import edge to the package
   `__init__.py`, not the `pipeline` submodule (`_python_import_modules`,
   `src/graphite/extract/ast.py:487-523`, consumed at `ast.py:660-671`).
   The edge counts as bound, the graph reports healthy (calls 0.945,
   imports 0.903), and `impact` under-reported a change's test blast
   radius by 18 of 26 sites (issue #7, measured on aramid).
2. **Aggregate `healthy` masks per-language failure.** firescraper
   reports `healthy: true` while its TypeScript calls bind at 0.542 —
   JS at 0.973 pulls the aggregate over the 0.8 threshold (measured,
   2026-07-26 corpus sweep).

Additionally (issue #5, measured): `imported_by` / `depends_on` result
entries omit `source_file`, so path-based filtering of importers fails
silently — while `stats`' `top_incoming` entries carry the field.

An empty answer today prints nothing about what was searched or what the
emptiness means; `inconclusive` fires only when the **aggregate** graph
is unhealthy.

## 2. Requirements

R1. Every canonical graph answer (see §7 for the covered surfaces)
    carries a machine-readable `answer` block: the relations the verb
    walked, the languages in scope, the per-relation **per-language**
    health cells for exactly those, a derived grade, the applicable
    caveat codes, and — when the primary result is empty — an
    `empty_meaning` sentence.
R2. Human output prints an epistemology line when the answer is empty
    OR any scoped health cell is degraded. Healthy non-empty answers
    are visually unchanged.
R3. Known blindspot classes are disclosed through a static, versioned
    caveat registry, filtered per answer by relation×language
    applicability, and published via `capabilities`. Disclosure is
    decoupled from fixes: a confirmed class gets an entry the day it is
    confirmed.
R4. The Python resolver binds `from <package> import <name>` to the
    submodule's file node when `<name>` resolves as a module, in
    addition to the existing package edge (fix for issue #7).
R5. `imported_by` and `depends_on` entries carry `id`, `name`, `kind`,
    `source_file` — identical shape to `stats.top_incoming` (fix for
    issue #5).
R6. The contract must be fail-open: no query may error or change its
    primary result because the answer block could not be computed.
R7. All changes to published surfaces are additive or explicitly
    documented semantic refinements (§9).

Non-goals (explicitly out of this round): the TS calls-classification
project (issue #4, separately planned), daemon discovery/engine-staleness
(issue #6), daemon-status truncation marker (#8), `daemon status`
space-form suggestion (#9), any `--explain` provenance trace, and any
change to `search` (string matching, already truncation-honest) or to
`stats` (it is the health overview; an answer block there is redundant).

## 3. Architecture

One new module, `src/graphite/answer_contract.py`, owning:

- `CAVEAT_REGISTRY` — static tuple of caveat entries (§5).
- `build_answer_block(g, *, relations, languages, total) -> dict | None`
  — the single constructor every surface calls. Returns `None` on any
  internal failure (fail-open, R6); callers omit the key when `None`.
- `GRADE_DECISION`, `GRADE_ADVISORY`, `GRADE_INCONCLUSIVE` constants.

Wiring (the only call sites):

- `src/graphite/query.py::_attach_resolution` (line 18) — covers all
  relation verbs routed through it, including `--natural` via
  `execute_plan`.
- `src/graphite/cli.py::_impact` (line 342) — covers `impact`, `watch
  --impact`, and `review`'s impact block (they consume `_impact`'s
  dict; their printers are unchanged this round).
- `src/graphite/context.py` impact builder (line ~197) — covers
  `context`.

Human printers updated: `cmd_query` relation-verb rendering, `cmd_impact`
(cli.py ~1035), `context` markdown (context.py ~122). Exact line format
in §6, pinned so tests can snapshot it.

`cmd_capabilities` (cli.py:890) gains an `answer_contract` key (§8).

## 4. The `answer` block (contract)

```json
"answer": {
  "schema": 1,
  "relations": ["calls"],
  "languages": ["python"],
  "health": {
    "calls": {
      "python": {"bound": 4748, "total": 5025, "ratio": 0.945, "healthy": true}
    }
  },
  "grade": "decision_grade",
  "caveats": [
    {"code": "python-dynamic-dispatch",
     "summary": "dynamically dispatched calls (getattr, decorator rebinding) are not modeled"}
  ],
  "empty_meaning": "no bound callers found"
}
```

(The caveat appears because its relations×languages intersect this
answer's; an imports-only answer would not carry it.)

Field rules:

- `relations`: declared per verb (§7 table), not inferred from edges.
- `languages`: the language(s) of the matched seed node(s) — union over
  seeds when a surface takes several (e.g. `impact` with multiple
  files) — derived from source-file extension via the existing
  extension→language map through one shared helper in
  `answer_contract.py` (all call sites use it; no divergent
  derivations). When there is no seed (graph-wide answers) or no seed
  has a source file, all languages present in the graph's `by_language`
  health.
- `health`: for each declared relation × in-scope language, the cell
  from `resolution_health(g)["by_language"]` (computed live — works on
  graphs built before this round). A relation/language combination with
  no cell (e.g. zero edges of that kind) is omitted from `health` and
  does not degrade the grade.
- `grade`:
  - `decision_grade` — every included cell has `healthy: true`
    (ratio ≥ threshold 0.8, same threshold as today).
  - `advisory` — at least one cell degraded AND the primary result is
    non-empty.
  - `inconclusive` — at least one cell degraded AND the primary result
    is empty.
- `caveats`: registry entries whose relations ∩ `answer.relations` ≠ ∅
  AND languages ∩ `answer.languages` ≠ ∅, projected to `{code, summary}`.
- `empty_meaning`: present iff the primary result is empty. Wording per
  verb, fixed in the plan (e.g. callers → "no bound callers found";
  imported-by → "no bound importers found"; impact → "no impacted files
  or tests reachable through bound edges").
- Health-cell `healthy` uses the per-cell ratio; the aggregate
  `resolution_health.healthy` is NOT consulted anywhere in this block.

Legacy `inconclusive` key: retained on every surface that has it today,
derivation upgraded from "empty AND aggregate unhealthy" to "empty AND
any scoped cell degraded" (i.e. `grade == "inconclusive"`). This is a
deliberate semantic refinement: it starts firing for
firescraper-shaped cases (empty TS answer, aggregate healthy), which
also makes the existing `query_inconclusive` incident record accurately.
Documented in the new agent-integration.md answer-contract section
(§8); consumers that branch on
`inconclusive` get strictly more accurate signal, never less.

`resolution_health` (full block) remains attached unchanged wherever it
is attached today.

## 5. Caveat registry

Entry shape (in code; published verbatim via capabilities):

```python
{"code": "python-dynamic-dispatch",
 "relations": ("calls",),
 "languages": ("python",),
 "summary": "dynamically dispatched calls (getattr, decorator rebinding) are not modeled",
 "since": "2026-07-26"}
```

Optional field `retired_by` (version string) — retired entries are kept
in the registry source for history but never emitted in answers and
never published as active.

Initial entries (both live, both real):

| code | relations | languages | rationale |
|---|---|---|---|
| `python-dynamic-dispatch` | calls | python | permanent residual, documented since the resolver round |
| `ts-external-calls-unclassified` | calls | typescript, javascript | calls to external-package symbols / runtime globals / destructured locals count as unbound; retires when the issue-#4 round lands |

The `from-package-import-submodule` class gets NO entry: R4 fixes it in
this same round, so it is never live under the contract.

Process rule (normative): when a blindspot class is confirmed (the
incident/triage loop), a registry entry is added the same day, in a
plain commit, without waiting for the fix. Registry changes are additive
data changes, not schema changes.

## 6. Human output

Printed when `total == 0` OR any included health cell is degraded, on
the surfaces in §7:

```
[graphite] 0 callers found for detect_tests — no bound callers found
  answer health: calls (python) 0.94 — decision-grade
  known limits: dynamically dispatched calls (getattr, decorator rebinding) are not modeled
```

- Line 1: existing result line, plus " — " + `empty_meaning` when empty.
- Line 2: one line, all included cells, `relation (language) ratio`,
  comma-separated, then "— " + grade with underscores rendered as
  hyphens.
- Line 3: "known limits: " + caveat summaries, "; "-separated. Omitted
  when no caveats apply.
- Degraded non-empty answers print lines 2–3 after their normal output.

Healthy non-empty answers: byte-identical output to today.

## 7. Covered surfaces and per-verb relations

| surface | relations declared | notes |
|---|---|---|
| `query callers` | calls | via `_attach_resolution` |
| `query calls` | calls | 〃 |
| `query reaches` | calls, imports | 〃 |
| `query path` | calls, imports | 〃 |
| `query depends-on` | calls, imports | 〃 (also gains `source_file`, R5) |
| `query imported-by` | imports | 〃 (also gains `source_file`, R5) |
| `query community-of` | calls, imports | 〃 |
| `impact` | calls, imports | via `_impact`; `watch --impact` and `review` inherit the dict |
| `context` | calls, imports | via context.py builder |
| `query --natural` | per resolved verb | inherits through `execute_plan` |
| `stats` | — exempt | it is the health overview |
| `search` | — out of scope | string matching; already truncation-honest |

## 8. Published-surface changes

- `docs/schemas/query-result.v1.schema.json`: additive `answer`
  property (object, `additionalProperties: true` per repo convention;
  `schema`, `relations`, `languages`, `health`, `grade`, `caveats`
  required inside it; `empty_meaning` optional). Top-level `answer` is
  NOT added to the required list.
- `docs/agent-integration.md`: new section — how to read `answer.grade`
  (empty + decision_grade = trustworthy absence; advisory = verify with
  grep and say so; inconclusive = unknown, not safe), the caveat
  vocabulary, and the `inconclusive` derivation refinement.
- `capabilities`: additive `answer_contract` key:
  `{"schema": 1, "grades": [...], "caveats": [active entries]}`.
- `src/graphite/init.py`: `DOC_VERSION` 8 → 9; managed template gains
  the answer-grade guidance for agents.
- `src/graphite/config.py:32,159`: `cache_version` "v7" → "v8"
  (extraction output changes, R4) — forces full re-extraction on next
  build; daemon child builds pick it up automatically; no daemon
  restart (no daemon-executed surface changes).

## 9. Resolver fix (R4, issue #7)

At `ast.py:660-671` (`import_statement` / `import_from_statement`
edge emission): for `import_from_statement`, after emitting the
base-module edge exactly as today, iterate the imported names (same
child-walk and identity-skip as `_collect_python_import_maps`,
ast.py:565-582) and for each name `n` with no dot: try
`source_index.resolve_python_module(rel_path, f"{base}.{n}" if base
else n, dots)`; when it resolves, emit an additional
`file_id -> _file_node_id(resolved)` `imports` edge with confidence
`EXACT_IMPORT`. Names that do not resolve as modules emit nothing new
(they are symbols; the base edge already covers them).

Covered idioms (each gets a regression test):

| idiom | today | after |
|---|---|---|
| `from aramid import pipeline` | edge → `aramid/__init__.py` only | + edge → `pipeline.py` |
| `from aramid import config, gitutil, pipeline` | 〃 | + one edge per resolving submodule |
| `from aramid.runners import tests as tests_runner` | edge → `runners/__init__` | + edge → `runners/tests.py` |
| `from . import sibling` | edge → package `__init__` | + edge → `sibling.py` (dots honored) |
| `from pkg import (a, b)` parenthesized | base edge | + submodule edges (identity-skip already paren-safe) |
| `from pkg import SYMBOL` (not a module) | base edge | unchanged |

Expected consequences: imports `bound` and `total` both rise (new edges
are bound); `impact`/`likely_tests`/`imported-by` topology gains the
missing test files (acceptance §12); no removed edges.

## 10. Field fix (R5, issue #5)

`query.py::_neighbor_listing` line 139-142: replace the inline
`{id, name, kind}` dict with `_node_view(g, n)` (query.py:531). Entries
gain `source_file` (`""` for file-less nodes — same sentinel as
`top_incoming`). Parity test asserts the entry shape equals
`top_incoming`'s minus degree fields.

## 11. Error handling

- `build_answer_block` wraps its whole body; any exception → `None`;
  callers omit the `answer` key and leave legacy fields exactly as
  today. A dropped block is the only failure mode (R6).
- Human printers guard on key presence; absence prints today's output.
- Registry is static data; no I/O anywhere in the contract path.
- `resolution_health(g)` is already exception-safe on the graphs the
  verbs accept; the wrap covers any surprise regardless.

## 12. Testing and acceptance

Unit:
- Grade matrix: {all healthy, one degraded, all degraded} × {empty,
  non-empty} → expected grade + legacy `inconclusive`.
- Caveat filtering by relation×language intersection; retired entries
  never emitted.
- Fail-open: monkeypatched `resolution_health` raising → block omitted,
  query result otherwise intact, exit code unchanged.
- Resolver idiom table (§9), each as extraction test asserting exact
  edge sets.
- `_neighbor_listing` ↔ `top_incoming` shape parity.
- Human-line snapshots: empty+healthy, empty+degraded, non-empty
  degraded, non-empty healthy (byte-identical to today).
- Schema compat: published query-result schema validates a live answer
  with and without the block; capabilities carries `answer_contract`.

Integration (fixture repos under tmp):
- aramid-idiom fixture: file A; package `p` with `__init__`, `b.py`;
  test file using `from p import b`; assert `impact a-target` lists the
  test post-fix and `imported-by b` includes it with `source_file`.
- Mixed-language fixture (firescraper regression): python cells healthy,
  ts cells degraded; a TS-seeded empty answer must grade
  `inconclusive` (legacy `inconclusive: true`) while
  `resolution_health.healthy` is true.

Live acceptance (operator-gated, mirrors the triage reproductions):
- A1: aramid rebuilt @ v8 → `impact src/aramid/detectors.py` Likely
  tests include `tests/unit/test_pipeline.py` AND
  `tests/unit/test_runner_tests.py`.
- A2: aramid `query "imported-by pipeline"` includes
  `tests/unit/test_pipeline.py` with populated `source_file`.
- A3: aramid: an empty answer on the healthy graph prints the
  epistemology lines with `decision-grade`.
- A4: firescraper rebuilt @ v8 → a TS-seeded empty answer carries
  `grade: inconclusive` despite aggregate `healthy: true`.
- Falsifiers stated with each gate at run time, per working rules.

Suite green (full pytest) + ruff clean before merge; final whole-branch
review per SDD process.

## 13. Rollout (post-merge, operator-gated where noted)

1. Merge + push per finishing-a-development-branch (operator chooses).
2. Consumer re-init to template v9 (aramid, BytesAI Learning,
   misc\Medication Reminder — same set as v8 rollout).
3. Machine CLAUDE.md doctrine block rewritten to the graded vocabulary
   (operator approves wording).
4. Notify aramid's agent (they filed #4–#9; #7/#5 close, contract is
   new surface for them to consume).
5. Close issues #5 and #7 with the two-answer rule (fix present AND
   problem gone, verified live).
6. No daemon restart (no daemon-executed surface changed).

## 14. Decisions log

- Scope: contract + #7 + #5 rider (operator, 2026-07-26).
- Coverage: JSON block always; human line on empty/degraded only
  (operator, 2026-07-26).
- Approach 1 (answer-scoped block via one shared seam) over
  extend-inconclusive and full-provenance (operator, 2026-07-26).
- `stats` exempt, `search` out of scope (design, approved with the
  whole design 2026-07-26).
- Legacy `inconclusive` semantics refined, not renamed (design,
  approved 2026-07-26).
