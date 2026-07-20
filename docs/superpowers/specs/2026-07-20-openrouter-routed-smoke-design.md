# OpenRouter Live Routed Edit Smoke through `execute_approved_route_pool`

**Status:** approved design, 2026-07-20.
**Parent spec:** `2026-07-20-openrouter-development-participation-design.md`. That
spec's live-acceptance order ended at step 4 (pool registration), and its
success criteria are already met. This design is a **deliberate extension
beyond** the parent scope: it exercises the persisted, signed-pool routing path
that pool registration proved *selectable* but never *executed*.

## Goal

Prove the routed OpenRouter edit path end-to-end, live: a real edit is routed
through `execute_approved_route_pool` against a **signed and consumed**
two-candidate pool, `select_route` picks the primary (`kimi-k2.7-code`), the
runner performs the actual `json_object` inference and whole-file edit, the
result validates against the pinned reference diff, the approval is consumed,
and one routed telemetry record is persisted. This closes the last gap in the
OpenRouter edit track: the offline registration proof
(`2026-07-20-openrouter-pool-registration-design.md`) constructed the pool and
showed `select_route` would pick the candidates, but bypassed real execution
and persisted nothing. Here the same composition is signed, consumed, and
*run*.

## Background (mechanism, already built and tested)

- **`execute_approved_route_pool`** (`src/graphite/routing/route_pool_execution.py`):
  keyword-only coordinator taking `pool`, `signed_approval`,
  `approval_authority`, `authority_loader`, `runner`, `evidence_sink`,
  `repository_quota_tokens`, `machine_quota_tokens`, `now`. It first
  `approval_authority.consume(...)`s the signed approval (a store write plus
  quota reservation), then loops up to `pool.max_attempts`: `select_route` →
  `runner(selection)` → `_validate_result` (budget-bounded) → `evidence_sink`,
  returning the first `RouteExecutionResult`. A `runner` that raises
  `RouteAttemptFailure(RouteAttemptEvidence)` records the failure and advances
  once on `capacity_unavailable`; any other exception fails closed
  (`route_attempt_failed`). Covered by `tests/test_route_pool.py`
  (`test_coordinator_*`) with in-memory fakes; this design supplies the first
  *live* runner.
- **`ApprovalAuthority`** (`src/graphite/routing/approval.py`):
  `issue(pool)` canonicalizes and HMAC-signs the pool manifest and writes an
  `approval_records` row (no lifecycle-binding args for a route pool);
  `consume(signed, pool, repository_quota_tokens=, machine_quota_tokens=)`
  verifies the signature, checks expiry, reserves quota, and marks the record
  `consumed`. Constructed with `(store, key_path=, quota_path=, now=)`.
- **`ApprovedRoutePool` / `ApprovedRouteCandidate` / `select_route` /
  `ProviderLifecycleService.route_authority`** — the exact machinery the
  offline registration proof used; reused unchanged. The two candidates are
  byte-identical to the registered ones (candidate digests `da0e80ea…` /
  `6f6c3b63…`); only the pool envelope differs (a real cost ceiling, fresh
  approval/task/nonce identifiers, budgets sized for a live edit).
- **`preflight_openrouter` / `execute_openrouter` / `apply_whole_file_edit`**
  (`src/graphite/routing/openrouter_executor.py`) — the proven single-call
  `json_object` edit channel (r7/r8). `preflight_openrouter` returns fresh
  pricing and the runtime identity; `execute_openrouter` performs exactly one
  bounded, schema-bound inference; `apply_whole_file_edit` writes the scoped
  files atomically.
- **`collect_diff_evidence`, `run_validation`, `create_task_worktree`,
  `record_cli_telemetry` / `CliTelemetryRecord`** — reused from the r7/r8 edit
  harness for diff capture, deterministic validation, worktree isolation, and
  telemetry persistence.

No `graphite` source change is expected.

## Live state (confirmed 2026-07-20, read-only)

- Routing store `events.sqlite3`: 12 capability_snapshots / 12
  lifecycle_snapshot_bindings / 21 cli_telemetry_events; integrity ok; 0 FK;
  schema_version 6; `approval_records` count 0 (no approval issued against this
  fixture before). Current hash `21e73d3f…`; lifecycle store `a99f7cc4…`.
- Both target models lifecycle-ACTIVE; edit snapshots `kimi-k2.7-code`
  `500cda19…` (identity `c8cece35…`), `kimi-k2.6` `1b2a7c8e…` (identity
  `6333c4d5…`); shared routing policy `916df225…`.
- Runway: the routed run selects the `kimi-k2.7-code` **edit** snapshot; it must
  run before that snapshot expires (`1784648044`), else a governed re-promotion
  is a prerequisite (separate step).

## Approved design decisions (operator, 2026-07-20)

1. **Edit target = the r7/r8 tenant-authorization edit**, reused byte-for-byte
   (same prompt, schema, `edit_scope = ("src/access.py", "tests/test_access.py")`),
   so the applied diff has a pinned reference — `diff_sha256 005f1ae8…`, 2 files,
   1007 bytes — as a deterministic success oracle.
2. **Pool = two-candidate** (`kimi-k2.7-code` primary, `kimi-k2.6` capacity
   fallback), the registered composition, now with a real cost ceiling. The live
   run selects and executes the primary; the fallback path is *not* forced live
   (it cannot be triggered deterministically) and was already proven selectable
   in the offline registration.
3. **Telemetry persisted**: the routed execution writes one routed
   `CliTelemetryRecord` (outcome `succeeded`, `MACHINE_VERIFIED`) to the live
   store (`cli_telemetry_events` 21→22).

## Pool composition being executed

One pool, task category ISOLATED_CODE, permission mode WORKSPACE_WRITE:

| position | model | capability snapshot | role |
|---|---|---|---|
| `candidate[0]` | `moonshotai/kimi-k2.7-code` | `500cda19…` (edit) | primary |
| `candidate[1]` | `moonshotai/kimi-k2.6` | `1b2a7c8e…` (edit) | capacity fallback |

Pool envelope differs from the offline-registered pool only in: `max_cost_microunits`
set to a real ceiling (worst-case `kimi-k2.7-code` cost ×2, computed from
preflight pricing), `max_output_tokens` / `max_input_tokens` sized for a live
whole-file edit (16384 / 65536, matching the r7/r8 edit smoke and within the
pool limits and the 262144 candidate context), a fresh `approval_id` / `task_id`
/ `nonce`, and `issued_at` / `expires_at` (`expires_at ≤ min` snapshot expiry).

## Component: the routed-smoke harness pair

Two scripts in `F:\tmp\graphite-live-acceptance-harness`, following the rN
convention; **live-inference and mutating** (unlike the offline proof):

- `_prepare_openrouter_routed_smoke.py` — pins the two candidate specs (reusing
  the registration values), the live pool envelope (real cost ceiling + live
  budgets), the reused edit prompt/schema/scope and reference diff, the
  ApprovalAuthority key/quota locations (a fresh harness state dir), the store
  hash/commit pins and before/after store contracts, and computes `BUNDLE` +
  `BUNDLE_DIGEST`. The bundle documents the exact routed composition and the
  expected mutation (`approval_records` +1 consumed, telemetry 21→22, snapshots
  and bindings unchanged).
- `_execute_openrouter_routed_smoke.py --approved <digest>`:
  1. Preflight: approval-digest match, expiry, digest recompute,
     implementation-commit match, clean feature worktree, store hash pins,
     before-audit (12/12/21, `approval_records` 0), `OPENROUTER_API_KEY` present.
  2. Build the two `ApprovedRouteCandidate`s from the live edit snapshots +
     ACTIVE identities (as in registration, with the drift check), and the
     `ApprovedRoutePool` with the live envelope.
  3. Construct `ApprovalAuthority(store, key_path, quota_path, now)` (fresh key
     + quota in the harness state dir) and `issue(pool)` → signed approval.
  4. Define the live **runner**: for the selected candidate, `preflight_openrouter`
     (fresh pricing; re-check runtime identity digest == candidate's
     lifecycle_identity_digest), create a fresh task worktree off the fixture,
     `execute_openrouter` (`json_object`, one inference), `apply_whole_file_edit`,
     `collect_diff_evidence`, `run_validation`; assert `diff_sha256 == 005f1ae8…`,
     `changed_files == 2`, validation passed; return `RouteExecutionResult`
     (`output = diff_sha256`, real tokens/duration/cost). On adapter
     `capacity_unavailable` raise `RouteAttemptFailure`; on other adapter errors
     let the coordinator fail closed.
  5. `authority_loader` derives live `RouteAuthority`s via
     `ProviderLifecycleService.route_authority` (read-only). `evidence_sink`
     persists a routed `CliTelemetryRecord` for the succeeded attempt, enriched
     with the routed candidate's capability_snapshot_digest and the validated
     diff counts.
  6. Run `execute_approved_route_pool(...)`; assert the returned result is the
     primary, the reference diff matches, and `store.approval_status(pool.approval_id)
     == "consumed"`.
  7. Final audit: capability_snapshots 12 and lifecycle_snapshot_bindings 12
     **unchanged**; cli_telemetry_events 21→22; `approval_records` 1 (consumed);
     integrity ok; 0 FK. Print one sanitized JSON receipt.

## Store mutation and budget

This round **mutates the live store and spends real budget** — inherent to a
routed smoke, and distinct from the offline proof:

- Writes: `approval_records` +1 (issued → consumed), a machine-quota reservation
  (separate quota sqlite in the harness state dir), and one routed
  `cli_telemetry_events` row (21→22). Unchanged: capability_snapshots 12,
  lifecycle_snapshot_bindings 12 (no promotion, no new snapshot, no lifecycle
  transition). The post-run store hashes/counts become the new pinned baseline
  for future rounds.
- Budget: exactly one live `kimi-k2.7-code` `json_object` inference (r7
  reference: ~450 output tokens, ~7.5 s, ~7.5k microunits). The pool's
  `max_cost_microunits` is a hard ceiling (worst-case ×2); `_validate_result`
  rejects any result exceeding the remaining budget.

## Error handling (fail-closed, sanitized)

- Manifest/approval/expiry/digest/commit/worktree/store-hash/before-audit
  mismatch, or missing credential → standard harness failure codes, before any
  issue or spend.
- A candidate snapshot missing/unbound/expired/non-ACTIVE, or live-vs-pinned
  drift → the registration proof's sanitized categories
  (`pool_candidate_source_expired` / `pool_candidate_unbound` /
  `pool_candidate_inactive` / `pool_candidate_drift`), before any spend.
- `ApprovalError` from issue/consume (signature, expiry, quota) → surfaced
  verbatim.
- Adapter errors inside the runner: `preflight_openrouter` / `execute_openrouter`
  `AdapterError` codes mapped to sanitized categories; a real capacity failure
  raises `RouteAttemptFailure` and the coordinator advances once to `kimi-k2.6`
  (recorded, not forced).
- Reference-diff mismatch → `routed_diff_mismatch`; over-budget →
  `route_pool_budget_exhausted`; `route_*` coordinator codes surfaced verbatim.
- Any unexpected final-audit drift (e.g. a snapshot/binding count change) →
  fail closed.

Receipts carry digests, counts, tokens, cost, durations, outcome categories, and
booleans only — never prompts, file contents, credentials, diffs, or raw
provider output. `raw_provider_output_persistence: false`; the same
`forbidden_persistence` list as prior rounds.

## Governance

Full live-inference gate, identical in discipline to r7/r8: the complete manifest
bundle is displayed and the operator approves with
`Approved: graphite_openrouter_routed_smoke bundle <BUNDLE_DIGEST>`; the execute
script requires `--approved <digest>`. The operator runs the execute in-session
via the `!` prefix (the classifier blocks the agent's own shell from the live
execute). `OPENROUTER_API_KEY` is read only from the session environment at
execute time; it never appears in argv, bundle, or receipt. Never touches
`F:\Projects\graphite` (main); no merge, push, or deploy. Budgets are never
weakened; empty diffs are never accepted; quarantined stores are never
reactivated.

## Evidence

Append a routed-smoke section to
`docs/superpowers/implementation-notes/2026-07-19-provider-lifecycle-evidence.md`
with the sanitized receipt, the routed selection and outcome, the pre/post store
hashes and counts (documenting the intended `approval_records`/telemetry
mutation), and confirmation that snapshots/bindings were unchanged; commit.

## Testing / verification

- Offline dry check before requesting approval: `py_compile` both scripts; run
  the prepare script to confirm the digest computes; a monkeypatched dry run
  against a store **copy** with a **fake transport** (no network, no real spend)
  that feeds the r7/r8 reference response through the runner + coordinator,
  asserting the primary is selected, the reference diff is produced, the approval
  is consumed on the copy, and telemetry is written on the copy. This mirrors the
  r7/r8 dry-run discipline and the registration proof's copy-based control-flow
  check.
- The existing route-pool, profile, and executor tests must remain green (no
  source change expected; any minimal fix a live gap forces gets its own test).

## Success criteria

- `execute_approved_route_pool` consumes the signed approval and `select_route`
  selects the primary (`kimi-k2.7-code`) against its live ACTIVE `RouteAuthority`.
- The live routed edit produces `diff_sha256 005f1ae8…` (2 files, 1007 bytes)
  with deterministic validation passed, within the pool budget.
- `store.approval_status(pool.approval_id) == "consumed"`; one routed telemetry
  row persisted (cli_telemetry_events 21→22); capability_snapshots and
  lifecycle_snapshot_bindings unchanged (12/12).
- Sanitized receipt printed; evidence recorded and committed on
  `feat/claude-codex-router`.

## Out of scope (deferred)

- Forcing the live capacity-fallback path to `kimi-k2.6` (non-deterministic;
  its selectability is already proven offline).
- The read-only review / authorization pool (OpenRouter reviewing CLI-authored
  diffs).
- The three unverified models (`kimi-k3`, `glm-5.2`, `muse-spark-1.1`).
- Branch integration (push/merge of `feat/claude-codex-router`).
