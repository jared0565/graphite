# Agent integration guide

How a coding agent (or any script) should drive graphite's query interface.
Everything on this page is canonical: deterministic, offline, inference-free.
The JSON contracts referenced here are published in [`docs/schemas/`](schemas/).

## 1. Discover, don't guess

```bash
graphite capabilities --json
```

Returns the machine-readable contract
([capabilities.v1](schemas/capabilities.v1.schema.json)): canonical commands,
every query verb with aliases/target roles/limits, search limits, plan flags,
and the complete natural-language grammar. Read this once per session instead
of guessing verbs or phrasings.

## 2. Locate nodes with search

```bash
graphite search "acceptPairing" --json
graphite search "web/sync.js" --json
```

Deterministic ranked search
([search-result.v1](schemas/search-result.v1.schema.json)) over ids, names,
paths, and concept tokens. `match_type` explains each hit (`exact-id`, `name`,
`path-suffix`, `id-substring`, `name-substring`, `tokens`); results are bounded
(`--limit`, default 20, max 100) with `truncated`/`total_matches` reported.
Exit code is 0 even for zero matches — branch on the JSON.

## 3. Query the graph

```bash
graphite query "callers acceptPairing"
graphite query "reaches handler -> db.write"
```

Responses follow the [query-result.v1](schemas/query-result.v1.schema.json)
envelope:

- Branch on the presence of `error_code` first. Stable codes: `empty_query`,
  `unknown_query_verb` (with `suggestions`), `invalid_query_format`,
  `node_not_found` (with `candidates`), `no_path`, `invalid_plan`.
- On success, `resolution` lists how every input resolved
  (`role`/`input`/`node`/`type`, plus `alternates` when a token was
  ambiguous). Check it before trusting results — a `fuzzy` resolution with
  alternates may mean the wrong node was picked.
- Traversal is bounded with generous defaults (path/reaches `max_depth` 32,
  neighbor listings `max_results` 200). Results report `truncated` and
  `limits`. Important nuance: a `no_path` error with `truncated: true` means
  the depth bound was hit — a longer path may exist; `truncated: false` means
  absence is proven.
- `query` always exits 0; errors live in the JSON (documented contract).

## 4. Validate before you spend

Every query executes through a canonical plan
([query-plan.v1](schemas/query-plan.v1.schema.json)):

```bash
graphite query "callers acceptPairing" --plan-only   # validate offline, no graph load
graphite query "callers acceptPairing" --show-plan   # execute and include the plan
```

`--plan-only` is a free syntax check: it never loads the graph, so agents can
verify a query (or a `--natural` translation) before paying for execution.

## 5. Natural-language questions (fixed grammar, no inference)

```bash
graphite query --natural "who calls acceptPairing?"
graphite query --natural "what breaks if I change db.ts"
```

`--natural` is a fixed deterministic grammar — anchored patterns, first match
wins, published in full under `natural_language.intents` in capabilities. It
is not an LLM and never touches the network. Three outcomes:

1. **Recognized query question** → translated to a plan and executed; the
   response includes `natural` (matched pattern) and `plan` for transparency.
2. **impact / tests / context question** → a `suggestion` with the exact
   canonical command to run instead (e.g. tests questions point at
   `graphite impact`, whose `likely_tests` field answers them).
3. **Anything else** → deterministic search fallback: the response embeds
   ranked `search` results as clarification candidates plus the
   `graphite search` command to refine. (`natural_no_terms` if nothing usable
   remains after stopword stripping.)

Unmatched questions never guess a verb — they fail closed to search.

## 6. Validating outputs yourself

The published schemas deliberately stay inside graphite's zero-dependency
subset validator (`graphite.routing.schema_validation`): no combinator
keywords (`anyOf`/`oneOf`/`$ref`/...), so you can validate with that module
verbatim — or with any standard JSON Schema validator. One semantic to know:
the subset validator treats an *absent* `additionalProperties` as closed,
which is why every deliberately open object in the published files carries
`additionalProperties: true` explicitly.

`tests/test_published_schemas.py` keeps these files honest: live outputs of
every verb, every error shape, natural-language answers, search, and
capabilities are validated against them in CI.

## 7. Resolution health (trust signal)

Every graph carries a measured resolver-health block; consumers must use it
to distinguish "no results" from "the resolver could not bind".

- Shape (in `stats`, `impact`, `context`, relation-verb JSON, `check --json`,
  and persisted in `graph.json` under `analysis.resolution_health` and in
  `graph-out/.graphite_analysis.json`):

```json
{
  "schema": 2,
  "placeholder_nodes": {"total": 4519, "unknown": 2463, "share": 0.545},
  "by_relation": {
    "calls":   {"total": 6631, "bound": 1730, "ratio": 0.261},
    "imports": {"total": 2047, "bound": 94,   "ratio": 0.046, "external": 127}
  },
  "by_language": {"python": {"calls": {"total": 6631, "bound": 1730, "ratio": 0.261},
                              "imports": {"total": 2047, "bound": 94, "ratio": 0.046, "external": 127}}},
  "healthy": false,
  "threshold": 0.8
}
```

- `healthy` is `true` iff every non-null `by_relation` ratio is `>= threshold`
  (0.8). Zero-edge relations have `ratio: null` and do not count against health.
- **schema 2**: imports cells carry an `external` count of imports outside the repo
  (stdlib, pip packages). Ratios count only should-bind-in-repo edges and exclude
  externals. `external` counts edges with `confidence="EXTERNAL_IMPORT"`.
  Ratios over graphs built before this change (schema 1) include externals — when
  reading ratios, branch on `schema` to interpret correctly. Consumers reading
  only `healthy` need no change.
- `impact`, `context`, and the relation verbs (`callers`, `calls`,
  `imported-by`, `depends-on`) additionally return `"inconclusive": true` when
  the result is EMPTY and the graph is unhealthy. **An inconclusive empty
  answer means "unknown", never "safe"** — fall back to grep and say so.
- ABSENT block (graphs built before 2026-07-25): treat exactly like
  `inconclusive` on empty results — fail open, never assume health.
- `check --json` reports `"resolution_health": null` when no persisted block exists.

## Non-goals (governance)

Canonical commands are inference-free by contract: `--llm` flags are rejected
with exit 2, and there is no provider-backed natural-language mode — by
design, not omission. Any future provider translation would live outside the
canonical surface, in the governed overlay boundary.
