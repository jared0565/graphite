# OpenRouter Live Review Smoke through `execute_approved_route_pool`

**Status:** approved design, 2026-07-21.
**Parent spec:** `2026-07-20-openrouter-development-participation-design.md`. This
extends the OpenRouter track into the AUTHORIZATION category and the **review
role**, which the edit track never exercised. It is a READ_ONLY sibling of the
live routed edit smoke (`2026-07-20-openrouter-routed-smoke-design.md`).

## Goal

Prove the routed **review** role live: an OpenRouter model — through its
READ_ONLY *verification* capability snapshot — reviews a known-correct
CLI-authored diff via `execute_approved_route_pool` and returns a governed
`{verdict, findings}`, with `select_route` selecting the primary
(`kimi-k2.7-code`) against its live ACTIVE AUTHORIZATION authority. The edit
track proved OpenRouter can *write* code through the signed pool; this proves it
can *review* code through the same pool, in the READ_ONLY / AUTHORIZATION
envelope.

## Background (mechanism, already built and tested)

- **`execute_approved_route_pool`** — the same coordinator the routed edit smoke
  used: consumes the signed approval, then loops `select_route` → `runner` →
  `_validate_result` → `evidence_sink`. Reused unchanged; only the `runner` and
  the pool envelope differ.
- **Review role = `execute_openrouter` with a review `output_schema`.** The
  parent design already states review-role execution "needs no new code beyond
  Task 5 — the review schema is just a different `output_schema`."
  `execute_openrouter` is a stateless prompt→response call (no workspace), so the
  diff-under-review is embedded in the review prompt and the model returns a
  json_object `{verdict, findings}`. There is no worktree, no
  `apply_whole_file_edit`, no diff capture, no validation subprocess — the review
  is READ_ONLY by construction.
- **The READ_ONLY verification snapshots are AUTHORIZATION-eligible.** Each
  OpenRouter model has two active-bound snapshots: the WORKSPACE_WRITE *edit*
  snapshot (used by the edit pool) and the READ_ONLY *verification* snapshot.
  Confirmed live (2026-07-21, read-only): both verification snapshots are
  `permission_mode read-only`, `risk_ceiling high`, and bound to the current
  ACTIVE identity — exactly what an AUTHORIZATION (READ_ONLY, HIGH-risk) pool
  needs. No separate "review promotion" exists or is required.
- **`ApprovalAuthority.issue`/`.consume`, `ProviderLifecycleService.route_authority`,
  `select_route`, `record_cli_telemetry`/`CliTelemetryRecord`** — all reused as
  in the routed edit smoke. The CLI-review precedent (`_execute_live_batch`) used
  category AUTHORIZATION, risk HIGH, READ_ONLY, and required verdict `pass` with
  empty findings.

No `graphite` source change is expected.

## Live state (confirmed 2026-07-21, read-only)

- Routing store `events.sqlite3`: 12 capability_snapshots / 12
  lifecycle_snapshot_bindings / **22** cli_telemetry_events (the routed edit
  smoke advanced telemetry 21→22); integrity ok; 0 FK; schema_version 6. Hash
  `aef018b0…`; lifecycle store `a99f7cc4…` (untouched by the edit/routed work).
- Review (verification) snapshots, both READ_ONLY / HIGH / ACTIVE-bound:
  `kimi-k2.7-code` `99db4a4a53aac5a97aae36d1d2ace5de3b194c63a8605c59dbd4837446a205f5`;
  `kimi-k2.6` `6816edafc45f164fa0dfec5009121afc6862c26c72c5963798177358f463b26d`.
  Active identities are the same per-model identities as the edit pool
  (`c8cece35…` / `6333c4d5…`); shared routing policy `916df225…`.
- **Runway:** the run selects the `kimi-k2.7-code` verification snapshot, which
  expires in ~8.9 h (sooner than the edit snapshots). The smoke must run before
  then, else re-verification is a prerequisite (a separate governed step).

## Approved design decisions (operator, 2026-07-21)

1. **Deliverable = live review smoke** (not an offline loadability proof) — route
   a real review through `execute_approved_route_pool`.
2. **Review oracle = correct diff → verdict `pass`.** The prompt embeds a
   known-correct CLI-authored change (the r7/r8 tenant-authorization
   `can_read_record` implementation and its tests, validated correct by pytest),
   framed as a diff authored by a CLI agent. Success = schema-valid
   `{verdict, findings}` **and** `verdict == "pass"` **and** `findings == []`,
   matching the CLI-review precedent. There is no byte-reference (a verdict is a
   model judgment); a `fail` verdict or any finding fails closed as
   `review_not_accepted` and records the verdict + finding count.
3. **Pool = two-candidate** (`kimi-k2.7-code` primary, `kimi-k2.6` capacity
   fallback), READ_ONLY verification snapshots. The primary executes live; the
   fallback is not forced.
4. **Telemetry persisted**: one routed review `CliTelemetryRecord` (category
   AUTHORIZATION, risk HIGH, `MACHINE_VERIFIED`, `changed_file_count 0`) →
   `cli_telemetry_events` 22→23.

## Pool composition being executed

One pool, task category AUTHORIZATION, permission mode READ_ONLY, task_risk HIGH:

| position | model | capability snapshot | role |
|---|---|---|---|
| `candidate[0]` | `moonshotai/kimi-k2.7-code` | `99db4a4a…` (verification, READ_ONLY) | primary |
| `candidate[1]` | `moonshotai/kimi-k2.6` | `6816edaf…` (verification, READ_ONLY) | capacity fallback |

Candidates carry the same ACTIVE `lifecycle_identity_digest` /
`model_identity_digest` / `routing_policy_digest` as the edit-pool candidates
(identity is per-model; both snapshots bind to it), differing only in
`capability_snapshot_digest` (verification), `permission_mode` READ_ONLY, and the
verification snapshot's expiry. `required_capabilities = ("code","reasoning","vision")`.
Pool envelope: `permission_mode READ_ONLY`, `task_risk HIGH`, a real
`max_cost_microunits` ceiling, small review budgets (`max_output_tokens` ~4096,
`max_input_tokens` sized for the embedded diff), fresh approval/task/nonce ids,
`expires_at ≤` the k2.7-code verification snapshot expiry.

## Component: the review-smoke harness pair

Two scripts in `F:\tmp\graphite-live-acceptance-harness`, a READ_ONLY variant of
the routed-smoke pair; **live-inference and mutating**:

- `_prepare_openrouter_review_smoke.py` — pins the two READ_ONLY candidate specs
  (verification snapshots), the AUTHORIZATION pool envelope, the review
  `output_schema` (`{verdict: "pass"|"fail", findings: [...]}`) and the review
  prompt embedding the tenant-auth diff, the ApprovalAuthority key/quota state dir
  (outside the fixture), the store hash/commit pins and before/after contracts,
  and `BUNDLE` + `BUNDLE_DIGEST`.
- `_execute_openrouter_review_smoke.py --approved <digest>`:
  1. Preflight: approval-digest / expiry / digest / implementation-commit /
     clean-worktree / store-hash-pins / before-audit (12/12/22) / credential.
  2. Build the two READ_ONLY `ApprovedRouteCandidate`s from the live verification
     snapshots + ACTIVE identities (with the drift check), and the READ_ONLY
     AUTHORIZATION `ApprovedRoutePool`.
  3. `ApprovalAuthority(...)` (key/quota outside the store root) → `issue(pool)`.
  4. Live **review runner**: `preflight_openrouter` (pricing; re-check identity
     digest), one `execute_openrouter` call with the review schema + review prompt
     (json_object), parse `{verdict, findings}`, assert `verdict == "pass"` and
     `findings == []`, return `RouteExecutionResult` (`output` = a sanitized
     verdict summary, real tokens/duration/cost). No worktree, no apply.
  5. `authority_loader` derives live AUTHORIZATION `RouteAuthority`s;
     `evidence_sink` persists one routed review `CliTelemetryRecord`.
  6. Run `execute_approved_route_pool`; assert the result is the primary and
     `approval_status(pool.approval_id) == "consumed"`.
  7. Final audit: capability_snapshots 12 and lifecycle_snapshot_bindings 12
     **unchanged**; cli_telemetry_events 22→23; integrity ok; 0 FK. Print one
     sanitized JSON receipt (verdict, finding_count, tokens, cost, digests).

## Store mutation and budget

Mutating + budget-spending, like the routed edit smoke:

- Writes: `approval_records` +1 (issued → consumed), a machine-quota reservation,
  one routed AUTHORIZATION `cli_telemetry_events` row (22→23). Unchanged:
  capability_snapshots 12, lifecycle_snapshot_bindings 12 (no promotion, no
  lifecycle transition). Post-run hashes/counts become the new pinned baseline.
- Budget: exactly one live `kimi-k2.7-code` review inference — small (a verdict,
  not a file), well under a real `max_cost_microunits` ceiling enforced by the
  pool and the runner.

## Error handling (fail-closed, sanitized)

- Manifest/approval/expiry/commit/worktree/store-hash/before-audit/credential
  failures → standard codes before any issue or spend.
- Candidate snapshot missing/unbound/expired/non-ACTIVE, or live-vs-pinned drift
  → the routed-smoke categories (`pool_candidate_source_expired` /
  `pool_candidate_unbound` / `pool_candidate_inactive` / `pool_candidate_drift`),
  before any spend.
- `ApprovalError` from issue/consume → surfaced.
- Adapter errors inside the runner → `AdapterError` codes; a real capacity
  failure advances once to `kimi-k2.6` (recorded, not forced).
- `review_response_invalid` (unparseable or schema-invalid `{verdict, findings}`);
  `review_not_accepted` (`verdict != "pass"` or `findings != []`) — records the
  verdict and finding count, no diff/finding text.
- `route_*` coordinator codes surfaced verbatim.

Receipts carry the verdict string, finding count, tokens, cost, durations,
digests, and booleans only — never the diff content, prompt, finding text, or raw
provider output. `raw_provider_output_persistence: false`.

## Governance

Full live-inference gate: displayed manifest → operator
`Approved: graphite_openrouter_review_smoke bundle <BUNDLE_DIGEST>` → operator
runs the execute in-session via `!` **with no inline `OPENROUTER_API_KEY`**
(ambient key — an inline placeholder shadowed the real key and burned two
approvals on the routed smoke). The key is read only from the session
environment; never in argv, bundle, or receipt. Never touches
`F:\Projects\graphite` (main); no merge, push, or deploy. Budgets never weakened;
a non-`pass` verdict is never rewritten to force acceptance.

## Evidence

Append a review-smoke section to
`docs/superpowers/implementation-notes/2026-07-19-provider-lifecycle-evidence.md`
with the sanitized receipt (verdict, finding count, tokens, cost), the routed
selection and AUTHORIZATION category, the pre/post store hashes and telemetry
count (22→23), the unchanged snapshots/bindings, and the statement that exactly
one live review inference ran, READ_ONLY, no promotion; commit.

## Testing / verification

- Offline dry check before approval: `py_compile` both scripts; run the prepare
  script to confirm the digest computes; a monkeypatched dry run against a store
  **copy** with a fake transport (and a stubbed review runner) returning a canned
  `{verdict: "pass", findings: []}`, asserting the primary is selected, the
  verdict is accepted, the approval is consumed on the copy, and telemetry is
  written (22→23). Mirrors the routed-smoke dry-run discipline.
- The existing route-pool, executor, and profile tests remain green (no source
  change; any minimal fix a live gap forces gets its own test).

## Success criteria

- `execute_approved_route_pool` consumes the signed approval; `select_route`
  selects the primary (`kimi-k2.7-code`) against its live ACTIVE READ_ONLY
  AUTHORIZATION authority.
- The live review returns a schema-valid `{verdict: "pass", findings: []}` within
  budget; `approval_status == "consumed"`; one routed AUTHORIZATION telemetry row
  persisted (22→23); capability_snapshots and lifecycle_snapshot_bindings
  unchanged (12/12).
- Sanitized receipt printed; evidence committed on `feat/claude-codex-router`.

## Out of scope (deferred)

- Forcing the live capacity-fallback to `kimi-k2.6` (non-deterministic; the edit
  track already proved fallback selectability offline).
- Reviewing an intentionally-flawed diff (discriminative review — a stronger but
  separate smoke).
- The three unverified models (`kimi-k3`, `glm-5.2`, `muse-spark-1.1`).
- Branch integration (push/merge of `feat/claude-codex-router`).
