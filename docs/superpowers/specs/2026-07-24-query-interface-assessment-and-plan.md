# Assessment: Agent-Agnostic Search and Natural-Language Querying

**Responds to:** `GRAPHITE_QUERY_INTERFACE_RECOMMENDATION.md` (external, prepared 2026-07-23
against graphite 0.1.0, received from the Medication Reminder project)
**Status:** IMPLEMENTED 2026-07-24 — Phases 1, 2, 3, and 5 shipped (Phase 1: cd85a7c and
ancestors; Phase 2: 56af5c4..a49244c; Phase 3: 7746f3a..b99b26f; Phase 5: ce9678a, 24d63b1).
Phase 4 remains rejected by design (see D1). Published contract: docs/schemas/ +
docs/agent-integration.md.
**Date:** 2026-07-24

## 1. Codebase-validated current state

Every claim below was verified against source, not assumed.

| Proposal claim | Reality | Verdict |
|---|---|---|
| Unknown verb yields `{"error": "unknown query verb: X"}` | Confirmed — `query.py:187`. Worse than the doc states: `cmd_query` always exits **0**, even on errors (`cli.py:672-676`), so scripts cannot branch on exit code | Confirmed gap |
| `graphite query "acceptPairing"` treated as unknown verb | Confirmed — verb is `tokens[0]` of the lowercased input (`query.py:27`) | Confirmed gap |
| `query --help` does not expose operations | Confirmed — help shows one example string; the verb list lives only in the `query()` docstring. Verbs are hardcoded `if`-branches, no registry | Confirmed gap |
| "Agents must already know the private grammar; no resolution help" | **Partially stale.** Graphite already has a shared deterministic resolver `_find_node_detail` (`query.py:216-247`) with a 4-step precedence (exact id → case-insensitive name → path suffix → substring fuzzy), match-type metadata, up to 3-4 `alternates`, and a not-found path that returns scored `candidates` (`query.py:190-213`). Used by query, context, and impact | Exists (partial) |
| No general search | Confirmed — no search verb/command. But `_candidates()` is a proto-search scorer (token split, haystack `id+name+source_file`, top 5) that only fires inside not-found errors. Phase 1 promotes this machinery, not green-field work | Missing (groundwork exists) |
| No capability discovery | Confirmed — nothing machine-readable lists operations | Missing |
| No stable error codes in query/context/impact | Confirmed — human strings only. Reusable in-repo conventions exist: `GraphReadError.code` (`graph_io.py:15-29`), `ValidationIssue.code`, `DoctorCheck` codes/statuses | Missing (conventions to reuse) |
| No versioned envelope | Confirmed — no `schema_version`/`ok` in query/context/impact (`ok` exists in validate/check/doctor) | Missing |
| Limits needed | Partial — 128 MiB bounded graph read (`graph_io.py:13`), context depth/neighbor caps, sample truncations. **No depth cap on `path`/`reaches`; no result cap on neighbor-listing verbs** | Partial gap |
| Search should cover "extracted documentation" | Not currently possible — the node model has **no doc field** (`extract/ast.py:78-96`, schema requires only `id`) | Defer (extraction change) |

Additional gap the document missed: `query` has no `--json` flag because it is **always** JSON —
fine — but the exit-0-on-error contract is undocumented and untested at the CLI level.

## 2. Disagreements with the proposal

- **D1 — Phase 4 provider adapters inside `graphite query --natural`: rejected as specified.**
  `query` is a canonical command. Canonical Graph Isolation (generated GRAPHITE.md contract,
  CLI gate at `cli.py:1850-1855`, test-pinned) guarantees canonical commands are inference-free
  and reject LLM flags; operator governance additionally forbids CLI wiring of remote providers.
  Preferred alternative: canonical `--natural` is deterministic-local **only** and fails closed
  with clarification candidates. If provider translation is ever wanted, it must be a separate
  non-canonical command in the governed overlay boundary (same lifecycle discipline as
  `overlay build`) — and is explicitly out of scope for this effort.
- **D2 — camelCase contract fields → snake_case** (`schema_version`, `resolved_node_id`,
  `max_depth`), matching every existing graphite JSON contract.
- **D3 — `AMBIGUOUS_TARGET` hard errors on close matches: rejected for existing verbs.**
  Current semantics (deterministic tie-break + `alternates` surfaced) are ergonomic for agents
  and backward-compatible. Ambiguity stays data, not an error: ranked results in `search`,
  `resolution` metadata + alternates elsewhere. Plan validation (Phase 2) may reject genuinely
  unresolvable targets with a stable code.
- **D4 — Automatic natural-language detection for non-verb input: rejected**, even as an option,
  in this effort. CLI semantics must not depend on input-classification heuristics. The improved
  unknown-verb error (stable code + suggestions pointing at `search`/`capabilities`/`--natural`)
  covers the UX need.
- **D5 — Wrapping existing outputs in a new `ok` envelope: rejected.** Additive fields only
  (`error_code`, `suggestions`, `schema_version`); the `error` string and all current keys stay
  verbatim. New commands (`search`, `capabilities`) get the full envelope from day one.
- **D6 — Documentation/doc-text search: deferred** — requires an extraction-side node-model
  change; v1 search covers id / name / qualified name / path / suffix / kind / tokens.

## 3. Implementation plan (safe, testable phases)

### Phase 1 — Discoverability and deterministic search (no existing-behavior changes)
1. Golden-pin existing verb outputs (test-only commit) so the refactor is provably inert.
2. Verb **registry** in `query.py` (name, aliases, arg shape, description) driving dispatch,
   argparse help epilog, and capabilities — replaces the `if`-chain.
3. `graphite capabilities [--json]`: commands, verbs + aliases + arg shapes, node/edge kinds,
   limits, `schema_version`, `natural: false`. Canonical, offline.
4. `graphite search <text> [--json] [--limit N]` (canonical, added to `_CANONICAL_COMMANDS`):
   generalizes `_find_node_detail`/`_candidates` into ranked multi-result search with
   `match_type`, deterministic score, `source_file`; bounded (default 20). Exit 0 (results in
   JSON; count may be 0).
5. Additive structured errors in query responses: `error_code`
   (`unknown_query_verb`, `node_not_found`, `no_path`, `empty_query`) + `suggestions`.
6. Docs: README + GRAPHITE.md/pointer template guidance (search-first wording per the
   proposal's documentation section) → **bump `DOC_VERSION` to 3** and update the pinned
   template digest (mechanism shipped 2026-07-24 in `162ed45`; versioned files refresh on next
   `init`, legacy files report `legacy unversioned`).

### Phase 2 — Canonical query plans
1. `query_plan.py`: snake_case plan schema v1 (operation, targets w/ resolution, options/limits,
   source.mode) + strict validation **reusing `routing/schema_validation.py`** (zero-dep,
   fail-closed subset validator already reviewed and hardened in the Track-1 program).
2. Existing verbs constructed as plans internally; `--show-plan` / `--plan-only` on `query`.
3. Result envelope additions (additive): `schema_version`, `resolution`, `truncated`, `limits`.
4. New bounds with generous documented defaults: max depth for `path`/`reaches`, result caps on
   neighbor-listing verbs, all reported via `capabilities` and `truncated` flags.

### Phase 3 — Deterministic natural-language parser (canonical-safe)
`query --natural "<question>"`: a local, grammar-based intent parser (dependencies, dependents,
path, impact, context, tests, else fall back to search) using the existing resolver; prints the
resulting plan; returns clarification candidates on ambiguity; never touches the network.
No providers, no config knobs that could enable them.

### Phase 4 — Provider translation: **not planned** (see D1)

### Phase 5 — Integration tooling
Publish plan/result JSON schemas under docs/, agent-instruction examples, and compatibility
tests exercising the published schemas.

## 4. Affected surface

Modules: `query.py` (registry, search, errors), `cli.py` (subcommands + flags),
new `query_plan.py`, `context.py` (additive envelope fields), `init.py` (template bump),
`README.md`. Tests: `test_call_graph.py` (goldens + registry parity), new `test_search.py`,
`test_capabilities.py`, `test_query_plan.py`; `test_init.py` (digest pin), `test_documentation.py`.

Migration: zero key removals; `error` strings verbatim; `query` exit code stays 0 (documented);
template bump propagates via the managed-region versioning; legacy instruction files unaffected.

## 5. Risks

| Risk | Mitigation |
|---|---|
| Registry refactor changes dispatch semantics | Golden-pin all verb outputs before refactor |
| New limits change results of previously unbounded verbs | Generous defaults + `truncated` flag + deliberate test updates in one reviewed commit |
| Search noise | Deterministic precedence ranking with match reasons; bounded results |
| NL scope creep toward inference | Phase 3 is grammar-only; Phase 4 explicitly not planned; canonical gate already rejects LLM flags |
| Agent docs drift | Template guidance ships behind `DOC_VERSION` bump |

## 6. Acceptance criteria (Phases 1–2 = v1)

- `graphite search "acceptPairing"` and `graphite search "web/sync.js"` return correct ranked
  matches with reasons, offline, bounded.
- `graphite capabilities --json` lists every dispatchable verb — enforced by a registry-parity
  test (a verb cannot exist without appearing in capabilities).
- Unknown-verb responses carry `error_code` + actionable `suggestions`.
- Every pre-existing structured query output is byte-compatible (golden tests).
- `--plan-only` emits a validated plan without traversal; malformed plans rejected with stable codes.
- Full suite green locally under `CI=1` and on the GitHub Actions gate.
