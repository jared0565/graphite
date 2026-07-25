# Resolution Trust Signal + Honest-Empty Design

**Status:** Approved design, pre-implementation
**Date:** 2026-07-25
**Branch:** `feat/resolution-trust-signal`
**Origin:** Verified field report from aramid's agent (reply:
`F:\Projects\aramid\.superpowers\sdd\graphite-agent-reply-2026-07-25.md`).
The Python resolver binds neither cross-module calls (`extract/ast.py:543`
passes no import-symbol map to `_resolve_call`) nor imports (`ast.py:527-537`
targets dotted-module ids that never match `_file_node_id` path slugs), so
`impact`/`context`/relation verbs return **silently wrong empty answers** —
a false green light on exactly the high-risk edits GRAPHITE.md tells agents
to check first. Measured on aramid at HEAD `62f5235`: 26.1% of `calls` edges
bound, 4.6% of `imports` edges bound, 54.5% of nodes are placeholders.

This round does NOT fix the resolver (that is the next round). It makes the
graph **honest about what it does not know**, machine-readably, so automated
consumers (agents, aramid) can distinguish "no dependents" from "resolver
could not bind".

## 1. Goal

1. A deterministic, inference-free **resolution-health metric** computed from
   the canonical graph.
2. Persisted in build artifacts (for non-graphite readers) and computed live
   on query surfaces (never stale).
3. **Honest-empty semantics**: `impact`, `context`, and relation verbs mark
   empty answers `inconclusive` when the graph is unhealthy, in JSON and in
   human text.
4. **Strict-hook health gate**: `--strict` never denies a grep fallback while
   the graph is unproven or unhealthy (defuses aramid CLASH 1 / Gate D1).
5. Template documentation (DOC_VERSION 5→6) + consumer-repo re-init rollout.

## 2. Operator decisions (2026-07-25)

**Amendment (2026-07-25, during Task 3):** the health block's key is
`resolution_health` on EVERY surface (query envelope, stats, impact, context,
check --json, persisted `analysis.resolution_health`). The originally spec'd
name `resolution` collides with the published query-result v1 contract's
existing per-target input-match `resolution` field, which stays untouched.
Operator-approved rename; additive-only promise preserved.

| # | Decision | Choice |
|---|----------|--------|
| 1 | Inconclusive threshold | Bound-edge ratio `< 0.8` → inconclusive (empty results) / "may be incomplete" (non-empty) |
| 2 | Metric granularity | Measured ratios only (per-relation × per-language + placeholder share). No declared resolver-tier labels. |
| 3 | Strict gating | **Included this round** — strict denial must never ship ungated on health |
| 4 | Template | **DOC_VERSION 5→6 this round** + re-init consumer repos at rollout |

## 3. Non-goals

- No resolver fix (next round: Python import-symbol map + import-target
  file-node resolution).
- No per-repo threshold configuration (one constant; revisit only on demand).
- No LLM/inference anywhere — Canonical Graph Isolation holds: the metric is
  pure arithmetic over the loaded graph.
- No daemon changes, no daemon restart (build children pick up new code; no
  daemon-executed surface changes).
- No incident ledger (queued as its own future round, operator decision
  2026-07-25).
- No routing/lifecycle changes.

## 4. The metric — `resolution_health(g)`

New module `src/graphite/health.py`, one public function plus the threshold
constant. Pure, total, deterministic; single pass over edges + nodes.

```python
RESOLUTION_HEALTHY_RATIO: Final = 0.8

def resolution_health(g: nx.DiGraph) -> dict[str, Any]: ...
```

Output shape (values from aramid for illustration):

```json
{
  "schema": 1,
  "placeholder_nodes": {"total": 4519, "unknown": 2463, "share": 0.545},
  "by_relation": {
    "calls":   {"total": 6631, "bound": 1730, "ratio": 0.261},
    "imports": {"total": 2047, "bound": 94,   "ratio": 0.046}
  },
  "by_language": {
    "python": {
      "calls":   {"total": 6631, "bound": 1730, "ratio": 0.261},
      "imports": {"total": 2047, "bound": 94,   "ratio": 0.046}
    }
  },
  "healthy": false,
  "threshold": 0.8
}
```

Rules (all normative):

- **Bound** = the edge's target node exists in `g` with `kind != "unknown"`.
- **Relation universe** = `{"calls", "imports"}` only. Structural relations
  (`contains`, etc.) are bound by construction and would inflate the ratio.
  Edges with other relations are ignored entirely.
- **Language attribution** = extension of the edge's `source_file` attribute
  (the file where the edge was observed), via a fixed table:
  `.py→python`, `.ts/.tsx/.mts/.cts→typescript`,
  `.js/.jsx/.mjs/.cjs→javascript`, `.go→go`, `.rs→rust`, anything else
  (including missing `source_file`) → `other`. `by_language` includes only
  languages that have at least one counted edge.
- **Ratios** rounded to 3 decimals (`round(x, 3)`).
- **Empty denominator** → `"ratio": null` for that cell; null ratios are
  excluded from the health verdict (a repo with no `imports` edges is not
  unhealthy for lacking them). If BOTH top-level relations have zero edges,
  `healthy` is `true` (nothing to distrust) — this keeps tiny/new repos
  from being flagged.
- **`healthy`** = every non-null top-level `by_relation` ratio
  `>= RESOLUTION_HEALTHY_RATIO`. Per-language ratios and placeholder share
  are informational only and never affect the verdict (a polyglot repo with
  one broken language still gets `healthy: false` via the top-level ratios,
  because the top-level totals include that language's edges).
- `placeholder_nodes.share` = unknown-kind nodes / total nodes, rounded to 3
  decimals; `share` is `null` when the graph has zero nodes.

## 5. Build persistence

`analyze(g)` (in `analyze.py`) gains a top-level key
`"resolution_health": resolution_health(g)`. Because `_report` (cli.py) writes the
analysis into both `.graphite_analysis.json` and the exported `graph.json`
bundle, the signal lands automatically in:

- `graph-out/.graphite_analysis.json` — small file; the cheap read for
  `check` and the strict hook.
- `graph-out/graph.json` — the bundle non-graphite consumers (aramid) read.

Bundle validation (`validation.py`) is permissive about additive analysis
keys — no validation change is required. Old graphs (built pre-change)
simply lack the key; every consumer fails open (see §9).

## 6. Query surfaces (live-computed)

All query-side consumers compute `resolution_health(g)` from the loaded
graph — never from the persisted copy — so the signal always matches the
graph actually being queried. The computation is one edge pass, negligible
next to JSON load.

### 6.1 `stats`

`_verb_stats` (query.py) adds `"resolution_health": resolution_health(g)` to its
result. Human rendering of stats (where applicable) is unchanged except the
JSON now carries the block.

### 6.2 `impact` (cli.py `_impact` / `cmd_impact`)

JSON result gains:

```json
"resolution_health": { ...full health block... },
"inconclusive": true | false
```

`inconclusive` = (`impacted_files` AND `likely_tests` both empty) AND
(`healthy` is false). Matched start nodes are irrelevant to the rule —
an empty answer from an unhealthy graph is inconclusive even if the input
matched a node.

Human text:

- Empty + unhealthy:
  `Impacted files: none found — INCONCLUSIVE: only 4.6% of import edges and
  26.1% of call edges resolved in this graph; treat as unverified and
  confirm with grep.` (percentages from the live block; a null ratio renders
  as `n/a`).
- Non-empty + unhealthy: normal listing, then one trailing line:
  `note: resolution health low (imports 4.6%, calls 26.1%) — this list may
  be incomplete.`
- Healthy graph: output unchanged from today.

### 6.3 `context`

The impact section inside `context` output (context.py) applies the same
rule: "Impacted files: none found" becomes the INCONCLUSIVE line when the
graph is unhealthy; the "## Direct Dependents" section renders
`no direct dependents found — inconclusive (resolution health low)` instead
of the bare claim when it is empty and the graph is unhealthy. The context
JSON payload carries the same `resolution` + `inconclusive` fields at top
level.

### 6.4 Relation verbs (`callers`, `calls`, `imported-by`, `depends-on`)

`_neighbor_listing` results (query.py) gain the same two fields.
`inconclusive` = (`count == 0`) AND (`healthy` is false). The verbs' human
rendering (where the CLI prints JSON already) needs no separate text change.
`_not_found` results are NOT marked inconclusive — an input that resolved to
no node is a different condition (already explicit) and must stay
distinguishable.

### 6.5 `check --json`

`cmd_check` reads `graph-out/.graphite_analysis.json` (never the full
graph.json — check must stay fast), extracts `analysis["resolution_health"]`, and
adds it to the JSON output as `"resolution_health"`. Missing file, unreadable JSON,
or absent key → `"resolution_health": null`. Human (non-json) output unchanged.

## 7. Strict-hook health gate

In `agent_hooks.handle_pre_tool_use` (strict branch, agent_hooks.py:264):
before honoring a `_strict_denial`, load the persisted health block from
`<root>/graph-out/.graphite_analysis.json`.

- `healthy: true` → strict denial proceeds exactly as today.
- `healthy: false`, block missing, file missing, or ANY read/parse error →
  the denial is **suspended**: the hook returns the remind-style context
  injection instead (same message text as remind mode), with one appended
  sentence: `(strict denial suspended: graph resolution health is low or
  unknown — grep fallback allowed.)`
- Denying requires **proven** health; unknown is treated as unhealthy.
  This is the operator's decision #3: strict can never ship ungated.

The hook's existing top-level `except Exception: return None` fail-open
contract is preserved — a health-gate bug can only ever make the hook MORE
permissive, never break a tool call.

## 8. Template + rollout (DOC_VERSION 5→6)

The managed GRAPHITE.md template (init.py) gains a short **Resolution
health** note in the agent guidance:

> Graph answers include a resolution-health signal. If a result says
> `INCONCLUSIVE` (or JSON has `"inconclusive": true`), the graph could not
> bind enough edges to answer — treat empty as unknown, not safe, and verify
> with grep. `python -m graphite query "stats"` shows the ratios.

`DOC_VERSION` bumps 5→6. Per the managed-template mechanism, consumer repos
re-init at rollout (idempotent `python -m graphite init <path>`) — the same
repo set the graphite-first v5 rollout re-inited (enumerated at rollout time
from the repos carrying a managed GRAPHITE.md, not hard-coded here). Rollout
happens post-merge with operator approval, same as the graphite-first round.

`docs/agent-integration.md` (the published consumer contract) gains a
"Resolution health" section documenting the block shape, the threshold
semantics, `inconclusive`, and the absent-key fail-open rule (old graphs).
The published JSON schemas in `docs/schemas/` are `additionalProperties:
true`; the compat tests must be run to prove the addition is compatible; the
query-result schema gets the new optional fields documented (non-breaking,
additive).

## 9. Error handling (normative)

- `resolution_health` is total: any graph `nx.DiGraph` (including empty)
  returns a valid block; it never raises on well-formed graphs. Call sites
  in query/impact/context/stats do not add try/except (a failure there is a
  real bug and should surface in tests).
- ALL consumers of the **persisted** block (check, strict hook) are
  fail-open: missing/malformed → `null` / suspend-deny respectively; never
  an exception to the user.
- Absent-key on old graphs is the same as missing: fail open. No migration,
  no rebuild requirement; the signal appears on the next scheduled rebuild
  (daemon rebuilds on change).

## 10. Testing

- **health.py unit tests**: bound/unbound mixes; relation filtering
  (structural relations ignored); language attribution incl. missing
  `source_file` → `other`; empty graph; zero-denominator null ratios; both
  relations empty → healthy; threshold boundary (0.8 exactly is healthy);
  rounding.
- **stats**: block present and live-computed.
- **impact**: JSON fields + all three human-text states (empty+unhealthy →
  INCONCLUSIVE, non-empty+unhealthy → note line, healthy → unchanged);
  matched-node-but-empty still inconclusive.
- **context**: same three states for the impact section and the
  direct-dependents claim; JSON fields.
- **relation verbs**: `count 0` + unhealthy → `inconclusive: true`;
  `_not_found` unchanged (no inconclusive field).
- **check --json**: persisted block passthrough; missing file → null;
  malformed JSON → null; human output unchanged.
- **strict hook**: healthy → denial preserved; unhealthy → suspended with
  appended sentence; missing artifact → suspended; malformed artifact →
  suspended; remind mode unaffected.
- **persistence roundtrip**: build a small fixture repo, assert the block in
  `.graphite_analysis.json` and in the exported `graph.json` bundle.
- **schema compat**: existing docs/schemas compat tests stay green.
- **template**: DOC_VERSION 6 emitted; new note present in generated
  GRAPHITE.md.
- Full existing suite stays green (baseline 2101 passed / 44 skipped at
  branch point `62f5235`).

## 11. Rollout / acceptance

Post-merge (operator-approved, per governance):

1. Rebuild + re-init consumer repos (idempotent).
2. Acceptance on aramid (their Gates B/C/D1, from the field report):
   - `impact src/aramid/ledger.py` → INCONCLUSIVE (not silently empty).
   - `query "callers auto_resolve_tdd"` JSON → `inconclusive: true`.
   - `stats` / `check --json` → resolution block with ~0.26 / ~0.05 ratios.
   - `graph-out/graph.json` bundle contains the block (aramid's Gate C1
     consumable).
   - Strict-mode denial provably suspended on aramid's current graph.
3. Notify aramid's agent via the established reply-file channel that Gate C
   is shipped and what the field shapes are.

No live-inference acceptance is involved anywhere in this round (no
routing surfaces touched).
