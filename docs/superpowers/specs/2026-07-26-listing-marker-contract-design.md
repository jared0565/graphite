# Listing Marker Contract — Design

Date: 2026-07-26
Status: approved design, pre-plan
Branch: `feat/listing-marker-contract` (base `9c0d943`)
Origin: GitHub issues #8 (truncation marker absent in `daemon-status`) and
#10 (empty-list marker absent in `impact`, plus answer-health lines rendered
at list indentation), filed 2026-07-26. Operator scope call: all CLI human
output; `export/md.py` explicitly excluded.

## 1. Problem

A graphite human-output listing can look complete when it is not, in two
distinct ways. Both were found on live repos, not by inspection.

1. **Render-time truncation with no marker.** `daemon-status` reports a
   project count in its summary line and then prints at most 20 rows
   (`cli.py:1344`), with nothing saying rows were dropped. Measured live
   (issue #8): header said 32 projects, 20 rows printed, zero lines matching
   `truncat`. "Absent from this list" carried no information, so the question
   the command was run to answer — is this repo daemon-covered? — could not
   be answered from its output.

2. **A header printed over an empty body.** `impact` prints `Likely tests:`
   unconditionally whenever *either* list is non-empty (`cli.py:1116-1122`),
   so an empty `likely_tests` yields a bare header. When the answer is also
   degraded, `_answer_lines` (`cli.py:1084`) emits its epistemology lines at
   the same two-space indent as list entries, so they land directly under the
   empty header. Measured live (issue #10, FireScraper `impact src/router.ts`,
   `typescript.calls` 0.542):

   ```
   Impacted files:
     - src/index.ts
     - src/server.ts
   Likely tests:
     answer health: calls (typescript) 0.54, imports (typescript) 1.00 — advisory
     known limits: calls to external-package symbols, runtime globals, and destructured locals count as unbound
   ```

   `likely_tests` is `[]`. Nothing says so, and `answer health: …` reads as a
   test entry to a human skimming or to any agent parsing the section
   line-by-line. Note that `note: resolution health low` (`cli.py:1129`,
   printed unindented) does not fire here: FireScraper's aggregate `healthy`
   is `true` while the language the answer used is degraded.

### 1.1 The pattern behind both

A repo-wide inventory of human-output listings (2026-07-26, first-hand)
shows the defect is not scattered forgetfulness. Every site that prints a
truncation marker obtained the fact from the data layer or computed it
inline; every site that does not applies a naked `[:N]` slice at render time.

| Site | Cap | Truncation marker | Empty marker |
|---|---|---|---|
| `cli.py:926` search | data-layer `truncated` | yes — `(truncated)` | yes |
| `cli.py:444` watch change | `[:8]` inline | yes — `" ..."` | yes |
| `review.py:402` | `_bounded_list` → `OUTPUT_TRUNCATED` | yes | yes — `- None` |
| `daemon_health.py:349` inner | `_MAX_GROUP_DETAILS` | yes — `... N more` | n/a |
| `context.py:210` neighbors | `[:20]` inline | **no** — but see §7.1: a *data-layer* cap, issue #11 | yes — `none` / `- none` |
| `cli.py:1120` impact | — | n/a | **no** (#10) |
| `cli.py:1344` daemon-status | `[:20]` inline | **no** (#8) | n/a |
| `cli.py:464/468` watch impact | `[:20]`/`[:30]` inline | **no** | guarded |
| `cli.py:878` validate errors | `[:10]` inline | **no** | guarded |
| `context.py:132/157` | `[:30]` inline | **no** | mixed |
| `daemon_health.py:374/376` outer | `[:20]` inline | **no** | guarded |

Two consequences follow. `query.py` already publishes `count`/`total`/
`truncated`/`limits` as a specified contract (`query_plan.py:22`: "echoed in
result `limits`/`truncated` fields"), so the convention is not folklore —
it is simply unenforced in the render layer. And `daemon-status --json`
(`cli.py:1335`) prints the *uncapped* list while the human view caps at 20,
so the two disagree about how many projects exist.

## 2. Requirements

R1. A rendered listing never prints a header over an empty body. If a
    header is emitted, a body is emitted.
R2. A rendered listing never drops an item silently. When a cap applies,
    the output names the number dropped.
R3. Marker lines are not mistakable for content. The truncation marker
    carries no list bullet; the epistemology lines emitted alongside a
    listing do not share the list indentation.
R4. On answer surfaces, an empty listing states only what the graph
    earned: a bare "none found" is permitted only when no scoped health
    cell for that answer is degraded (see §5).
R5. Whether a header is emitted at all is a per-surface editorial rule
    (§4), stated and tested, not left to each call site's discretion.
R6. Renderer-only. No result dict, JSON payload, or published schema
    changes; no `DOC_VERSION` bump; no consumer-repo rollout.

## 3. Architecture

One new module, `src/graphite/listing.py`, owning one guarantee — the same
shape as `answer_contract.py`: a focused module wired at a small number of
seams, so the rule is testable once rather than at every call site.

```python
def listing_lines(
    values: Sequence[T],
    render: Callable[[T], str] = str,
    *,
    header: str | None = None,
    cap: int | None = None,
    indent: str = "  ",
    empty: str | None = "none found",
    more_hint: str | None = None,
) -> list[str]:
```

Emitted in order:

1. `header`, if given.
2. `{indent}- {render(v)}` for each shown value; or `{indent}- {empty}` if
   `values` is empty.
3. `{indent}... {n} more{more_hint}` if `cap` dropped `n > 0` items.
   `more_hint` is appended verbatim, with no separator inserted — the one
   caller supplies its own leading `" — "`.

`empty=None` omits the listing entirely when there is nothing to show —
header included. This is what makes R1 enforceable by construction: a caller
that wants silence on empty cannot accidentally leave a header behind.
Count-in-summary and conditional-report surfaces (§4) all pass it.

The function is pure and total: it prints nothing, raises nothing, and
returns a list. `cli.py` prints the lines; `context.py` extends its line
lists with them. `daemon_health.py` does not call `listing_lines` at all —
it implements its marker locally (§7 row 8). Parameterisation is
required because the surfaces genuinely differ — `context.py` renders
`- \`path\`` at column 0, `cli.py` renders `  - path`, `daemon-status`
renders `  - {root} | builds=…`.

`more_hint` exists for one caller: `daemon-status` appends
`" — use --json for the full list"`. Issue #8's body proposes adding an
`--all`/`--json` flag; `--json` already exists (`cli.py:1335`) and already
prints the uncapped list, so no new flag is added — the marker points at it.

Wording is `... N more`, matching the existing in-codebase precedent at
`daemon_health.py:349`.

## 4. Surface kinds (R5)

Requirement R1 has a degenerate solution: suppress the header. That is what
`context.py:157` does today, and it is the banked block-absent asymmetry —
so R1 alone would let the fix collapse into the bug. The rule that prevents
it: **a header is mandatory when the surface is answering a question the
user asked.**

| Kind | Surfaces | Header | Empty marker | Truncation marker |
|---|---|---|---|---|
| Answer | `impact` | unconditional within the listing branch | yes, grade-aware (§5) | n/a — `cmd_impact` passes no `cap` at all (§1.1); nothing to truncate |
| Answer | `context` (impacted / tests) | unconditional within the listing branch | yes, grade-aware (§5) | yes — cap 30 |
| Count-in-summary | `daemon-status`, `validate` | none — the summary line states the count | n/a | yes |
| Conditional report | watch impact, `daemon-health` errors/warnings | only when non-empty | n/a | yes |

"Unconditional" is scoped to the branch that lists results. When *every*
list in the answer is empty, `impact` and `context` both print one sentence
whose `empty_meaning` is already "no impacted files or tests reachable
through bound edges" — that answers both halves, and adding per-half blocks
there would make the two renderers diverge for no gain.

Rationale per kind. **Answer** surfaces were asked a question and every part
of the answer must be stated, including the empty parts. **Count-in-summary**
surfaces have no list header at all — `daemon-status` prints rows directly
after `[graphite] updated: …` and `validate` after `graph invalid (N errors,
…)` — and their summary line already carries the total, so emptiness is not
lost and a dangling `- none found` under nothing would read worse than
silence. **Conditional report** surfaces volunteer information rather than
answering: watch fires on every file save, and printing `likely tests: none
found` per save adds noise to the surface whose signal this round protects.
Watch errors remain distinguishable (`impact skipped: {exc}`, `cli.py:459`).

This taxonomy is a judgment and judgments decay — that is how the present
defects arose. Mitigation: the surfaces are a short enumerable list, §9's
test table encodes the kind of each, and a ninth surface must declare its
kind to pass.

## 5. Grade-aware empty marker (R4)

On answer surfaces a bare `- none found` is a claim of trustworthy absence.
The answer contract grades a whole answer, not its parts: `cli.py:405`
computes `total = len(impacted_files) + len(likely_tests)`, so the
FireScraper repro in §1 (2 impacted, 0 tests, `typescript.calls` 0.542)
grades `advisory` — non-empty and degraded. But the `likely_tests` half is
empty *and* degraded, which is the contract's definition of `inconclusive`.
Rendering `- none found` there would assert an absence the graph did not
earn — a confident wrong answer replacing an ambiguous one, which is the
worse failure by this project's own doctrine.

Therefore, on the answer surfaces only, the `empty` string is selected by
whether any scoped health cell in the answer block is degraded:

- no degraded cell → `none found`
- any degraded cell → `none found — INCONCLUSIVE: treat as unverified and
  confirm with grep`

```
Impacted files:
  - src/index.ts
  - src/server.ts
Likely tests:
  - none found — INCONCLUSIVE: treat as unverified and confirm with grep
answer health: calls (typescript) 0.54, imports (typescript) 1.00 — advisory
known limits: calls to external-package symbols, runtime globals, and destructured locals count as unbound
```

The overall answer is advisory; its empty half is inconclusive; both are
now stated. `INCONCLUSIVE` is the contract's existing vocabulary for
degraded-and-empty, not a new term. The block's `empty_meaning` field is
deliberately **not** reused: it describes the whole answer being empty,
which is the wrong claim for a half.

**Known limitation, deliberately not fixed here.** The real gap is that the
answer contract has no sub-answer granularity — it cannot grade
`likely_tests` independently of `impacted_files`. This section compensates
in the renderer. A contract-level fix (per-list grading) is a candidate
follow-up ticket, out of scope for a renderer-only round.

A second, distinct gap sits underneath the first. `empty_marker` (via
`is_degraded`) returns the bare `none found` whenever no scoped health cell
in the block is degraded — and that condition is also true when the
block's `health` dict has **no cells at all**. Verified live: a `.ts` start
node queried in a graph whose only `calls`/`imports` edges are Python
produces `languages: ['typescript']`, `health: {}`, `grade:
decision_grade`, rendering `Likely tests:` / `- none found` with no
epistemology line and no `note:` line — a trustworthy-absence claim resting
on zero health evidence for that language. This is inherited
`answer_contract` v1 behaviour, not something this round introduces: the
documented "no health cell → `decision_grade` on zero evidence" weakness
(D7 territory, same as the sub-answer-granularity gap above). The remedy is
out of this round's renderer-only scope — `listing_lines` and
`empty_marker` can only act on the health data the contract hands them.
Noted here because on this exact path the round's own effect is to convert
output that was previously visibly incomplete (a bare header, or an
unmarked empty list) into a confident, well-formed claim.

## 6. Indentation fix (R3)

`_answer_lines` (`cli.py:1084-1086`) moves from two-space indent to column
0, matching its sibling `context.py:178-181`, which already renders these
lines at column 0, and `note: resolution health low` (`cli.py:1129`), which
already prints unindented. This is not a new convention; it makes `cli.py`
consistent with the two renderers it disagrees with.

Blast radius, verified rather than assumed: `answer health` appears in **no
managed doc template**. The only documentation hit repo-wide is the
historical spec `2026-07-26-honest-answer-contract-design.md:210`.
`src/graphite/init.py` and `docs/agent-integration.md` are clean, so
`DOC_VERSION` stays at 9 and no consumer-repo rollout follows (R6). Exactly
one test asserts the literal indented form — `tests/test_health.py:453` —
and is updated. The `tests/test_context.py` assertions use substring
matching and are indentation-agnostic.

## 7. Call sites

| # | Site | Kind | Change |
|---|---|---|---|
| 1 | `cli.py:1116` `cmd_impact` | Answer | both headers unconditional; grade-aware empty marker (#10) |
| 2 | `cli.py:1344` `cmd_daemon_status` | Count-in-summary | cap 20 → `... N more — use --json for the full list` (#8) |
| 3 | `cli.py:878` `cmd_validate` | Count-in-summary | errors cap 10 → marker |
| 4 | `cli.py:462` `_print_watch_impact` | Conditional | caps 20/30 → markers; guards retained |
| 5 | `context.py:132` | Answer | impacted cap 30 → marker |
| 6 | `context.py:157` | Answer | tests cap 30 → marker; **empty case now rendered** (markdown sibling of #10) |
| ~~7~~ | ~~`context.py:210`~~ | — | **Removed from this round — issue #11** (§7.1). The numbering keeps its gap so cross-references stay stable. |
| 8 | `daemon_health.py:374/376` | Conditional | outer caps 20 → markers; inner marker at :349 unchanged. Implements the marker locally rather than through `listing_lines`, because its body is grouped by issue code rather than being a flat list. `SURFACES` in `tests/test_listing_surfaces.py` declares this as a surface (`daemon_health._issue_lines`, with an expected `listing_lines` call count of zero — it makes none); the marker itself is tested in `tests/test_daemon_health.py`; and the AST reconciliation that ties §9's surface table to real call sites explicitly does not, and cannot, reach a hand-rolled marker. |
| 9 | `cli.py:1084` `_answer_lines` | — | column 0 (§6) |

### 7.1 Neighbour sections: removed from this round (issue #11)

Site 7 was originally specified as "add a truncation marker to
`context.py:210`". Implementation planning showed that is the wrong fix for
the wrong defect, so it is **removed** rather than shipped as something that
would not work.

`build_context` already caps neighbours at the data layer: `_neighbors`
(`context.py:201-203`) does `ids[:limit]` with `limit=neighbor_limit`, and
discards `len(ids)`. The render-site `[:20]` therefore sits on top of an
already-capped list. Two consequences, both measured live on this repo's own
graph:

```
graphite context src/graphite/cli.py --neighbor-limit 50 --json
  direct_dependencies 50, direct_dependents 30
graphite context src/graphite/cli.py --neighbor-limit 50
  Direct Dependencies 20, Direct Dependents 20 — no marker
```

1. At the default `neighbor_limit=20` the render slice can never drop
   anything: it is dead code, and a marker there would never fire.
2. Above the default it **silently overrides the user's explicit
   `--neighbor-limit`** (`cli.py:2282`), which is strictly worse than the
   defects this round fixes.

A render marker cannot repair either one. It would compute its count from
data that was already cut — printing `... 30 more` when the user asked for
50 and the payload holds 50 — and the true dropped count is known only to
`_neighbors`, which throws it away. The fix requires carrying a total and a
truncation signal in the **context result payload**, which R6 forbids here.
`context --json` is affected too, so this is not even a human-output defect.

Filed as issue #11 for a round that can change the result dict.
`direct_dependents` / `direct_dependencies` appear in no file under
`docs/schemas/`, so no published schema version is at stake.

The neighbour section's existing empty markers (`none`, `  - none` at
`context.py:203/208`) and its inconclusive path (`context.py:185-186`) stay
exactly as they are. That path keys off the **aggregate** `unhealthy` flag
rather than the scoped answer block — the pattern this project's doctrine
warns can lie — and remains a separate follow-up (D8).

### 7.2 Deliberately untouched

Named so a later reader knows these were considered, not missed. Each is
already honest:

- `review.py:402` + `_append_code_items` — prints `- None`, and truncation
  is reported upstream as an `OUTPUT_TRUNCATED` warning.
- `cli.py:444` `_print_watch_change` — prints `" ..."` when over 8.
- `cli.py:926` search — renders the data-layer `truncated` flag.
- `query.py` — the published `count`/`total`/`truncated`/`limits` contract
  is the model this round follows, not something it changes.
- `export/md.py` — has both defects (five bare section headers, four
  unmarked caps) but is out of scope by operator call: it writes a report
  file rather than answering a question.

## 8. Error handling

`listing_lines` is total. It has no failure mode of its own: no I/O, no
graph access, no exceptions raised. A `render` callback that raises is a
caller bug and propagates — the helper does not swallow it, because a
silently dropped row is the failure class this round exists to remove.

`cap=None` means no cap and therefore never a truncation marker.
`cap` values of zero or less are treated as no cap; a listing that shows
nothing while claiming a body would violate R1.

Unlike `answer_contract.build_answer_block`, this module is **not**
fail-open, because there is nothing to fail: it is pure formatting over
data the caller already holds.

## 9. Testing

Unit tests, `tests/test_listing.py`:

- empty values → header + `- none found`, no truncation line
- values under cap → header + rows, no truncation line
- values exactly at cap → no truncation line (off-by-one falsifier)
- values over cap → `... N more` with N = `len(values) - cap`
- `header=None` → body only, no leading blank
- custom `indent`, custom `empty`, `more_hint` appended
- truncation line carries no `- ` bullet (R3)

Surface table test, `tests/test_listing_surfaces.py`: `SURFACES` names the
seven listing surfaces — 1–6 and 8, each with its declared kind. Site 7 is
removed (§7.1); site 9 is not a listing and is covered by the epistemology
tests below. `test_listing_lines_marker_shape_is_uniform` and
`test_answer_surfaces_emit_their_header_when_empty` assert the marker shape
and the empty-header behaviour by calling `listing_lines()` directly — they
exercise the function's own contract, not any surface's call site. Within
this file, the one surface driven through its own renderer with a real
over-cap input is `context.likely_tests` (site 6), in
`test_context_likely_tests_cap_is_marked`, which calls
`format_context_markdown` directly. `cmd_impact` (site 1) cannot be tested
that way at all: it passes no `cap` to `listing_lines` (§1.1, §4), so it has
no over-cap input to give. Every other site is marker-tested in its own
suite rather than here — sites 2, 4 and 8 in `tests/test_daemon_health.py`
(`test_daemon_status_marks_truncation_and_points_at_json`,
`test_watch_impact_marks_truncation`,
`test_daemon_health_outer_cap_is_marked`), site 3 in
`tests/test_reliability.py`
(`test_validate_cli_truncates_error_list_and_marks_dropped_count`), and
site 5 in `tests/test_context.py`
(`test_context_impacted_files_cap_is_marked`, 35 impacted files against a
cap of 30). Site 5 is driven through `format_context_markdown` with an
over-cap input exactly as site 6 is; site 6's only distinction is that its
test lives in this file. What this file does assert
mechanically is `test_every_listing_lines_call_site_is_accounted_for`: an
AST walk over `src/graphite` reconciling the real `listing_lines()` call
count against `SURFACES`, so an added or removed call site with no matching
row fails the suite. That reconciliation is what makes "a new surface must
be added to this table to pass" true.

Answer-surface epistemology tests (extend `tests/test_health.py`,
`tests/test_context.py`):

- healthy graph, impacted > 0, tests == 0 → `- none found`, no
  `INCONCLUSIVE` suffix
- degraded graph, same shape → `- none found — INCONCLUSIVE: …`. This is
  the §5 falsifier: without the grade-aware branch it renders a bare
  `none found` and the test fails.
- `_answer_lines` output starts at column 0 (updates `test_health.py:453`)

Regression fixture:
`test_impact_empty_tests_half_is_marked_inconclusive_when_degraded`
(`tests/test_health.py`) — the FireScraper shape from §1 (impacted
non-empty, tests empty, `typescript.calls` degraded; concretely one
impacted file, `src/b.py`) run end to end through `cmd_impact`. It asserts
one substring — the `- none found — INCONCLUSIVE: …` marker line — is
present in the captured output, not a line-by-line comparison of the
rendered block.

## 10. Acceptance

Measured live after merge, falsifier stated before each run:

A1. `graphite impact src/router.ts` in a repo with the §1 shape prints
    `Likely tests:` followed by a marked empty body, and the answer-health
    lines at column 0. Falsifier: a bare header, or `answer health:`
    indented two spaces.
A2. `graphite daemon-status` with more than 20 projects prints
    `... N more — use --json for the full list`, and N + 20 equals the
    project count in the summary line. Falsifier: 20 rows and no marker,
    or an N that does not reconcile with the header count.
A3. `graphite daemon-status --json` still prints the uncapped list.
    Falsifier: JSON gains a cap.
A4. A degraded empty listing shows the `INCONCLUSIVE` suffix; the same
    shape on a healthy graph does not. Falsifier: either the suffix on a
    healthy answer, or its absence on a degraded one.
A5. Full suite green. Falsifier: any failure; run redirected to a file with
    `$?` read directly, not through a pipe.

## 11. Decisions log

D1. Shared `listing.py` module over per-site edits — the convention has now
    failed three rounds running as folklore; nine independent copies of the
    rule is how a fourth failure happens.
D2. Render caps are **not** published via `capabilities`. Doing so would
    invite agents to parse terminal output; `--json` exists precisely so
    they do not.
D3. No new `--all` flag for `daemon-status` — `--json` already serves it.
D4. `review.py`'s `- None` is left as-is rather than normalised to
    `- none found`: it is already honest, and churning its tests buys no
    epistemic gain.
D5. Watch keeps its header guards (§4, Conditional report). Consistency
    would cost a noise line per file save on the surface this round is
    protecting.
D6. The empty marker is grade-aware (§5) rather than a fixed string,
    because the motivating repro is itself a degraded answer and a fixed
    `none found` would overclaim on exactly that case.
D7. Sub-answer grading in the answer contract is out of scope; §5
    compensates in the renderer and names the gap.
D8. The neighbour sections' existing inconclusive path keys off aggregate
    `unhealthy` rather than the scoped grade (§7.1). Left as-is; moving it
    onto the scoped grade is a follow-up, not a silent rider.
D9. Site 7 is **removed** from the round rather than shipped (§7.1). The
    marker it was specified to add would never fire at the default
    `neighbor_limit`, and above the default the real defect is that the
    render cap overrides the user's flag — repairable only by changing the
    result payload, which R6 forbids. Shipping a marker that cannot fire
    would have been the round's own failure mode: output that looks
    complete and is not. Issue #11.

## 12. Follow-ups this round deliberately does not take

- **Issue #11** — `context` neighbour cap overrides `--neighbor-limit`, and
  `_neighbors` discards the total, so `context --json` is affected too
  (D9, §7.1). Found while planning this round.
- Sub-answer grading in `answer_contract` (D7, §5).
- The no-health-cells case of `empty_marker` grading a bare `none found` on
  zero evidence (§5).
- `context.py:185-186` aggregate → scoped health gate (D8, §7.1).
- `export/md.py` markers — five bare headers, four unmarked caps (§7.2).
- `daemon_health.py:670-671` caps `errors`/`warnings` at
  `_MAX_REPORT_ISSUES` (100) before `error_count`/`warning_count` are
  computed from the capped lists — issues beyond 100 vanish from the
  summary with no trace. A data-layer cap in the same class as issue #11
  (§7.1), which this round removed site 7 for rather than ship a marker
  that could not repair it.
