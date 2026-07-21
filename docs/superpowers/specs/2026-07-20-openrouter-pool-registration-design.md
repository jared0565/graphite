# OpenRouter ISOLATED_CODE Edit-Pool Registration — Loadability & Selectability Proof

**Status:** approved design, 2026-07-20.
**Parent spec:** `2026-07-20-openrouter-development-participation-design.md` (this
realizes that spec's "Live acceptance order" step 4, pool-registration, for the
isolated-code edit category).

## Goal

Prove that the two edit-promoted OpenRouter models are loadable, correctly
ordered pool candidates for the isolated-code edit category and are selected by
the existing route-pool machinery — the parent spec's success criterion
"verified models are loadable pool candidates for their categories" — with **no
live inference call and no store mutation**.

This is an *offline* acceptance artifact. The edit *execution* path is already
proven live end-to-end (edit smokes r7 for `kimi-k2.7-code`, r8 for
`kimi-k2.6`, both PASSED, both producing the identical diff `005f1ae8…`). What
remains unproven is the **pool coordinator layer** — `select_route` picking an
OpenRouter candidate against its live `RouteAuthority` — which r7/r8 bypassed by
calling `execute_openrouter` directly. This proof closes that gap by
construction, without spending budget.

## Background (mechanism, already built and tested)

- **`ApprovedRoutePool`** and **`ApprovedRouteCandidate`** (`src/graphite/routing/route_pool.py`):
  a frozen, single-use authority holding an ordered tuple of at most two
  candidates (`MAX_ROUTE_CANDIDATES` = 2; the second is a capacity fallback
  only), plus a task-category / risk / permission / budget / expiry envelope.
  A candidate carries `provider`, `runtime_kind`, `lifecycle_identity_digest`,
  `capability_snapshot_digest`, `model_identity_digest`, `routing_policy_digest`,
  `requested_model` / `effective_model`, `capabilities`,
  `context_window_tokens`, and `snapshot_expires_at`. The frozen `__post_init__`
  *requires* `routing_policy_digest` for OpenRouter and forbids it for the CLI
  providers.
- **`select_route(pool, authorities, attempts, *, now)`** — deterministic
  positional selection (`candidates[len(attempts)]`), never discovery or
  substitution. It validates the live `RouteAuthority` is ACTIVE and matches the
  candidate's `lifecycle_identity_digest` and `capability_snapshot_digest`
  exactly, and that `snapshot_expires_at > now`; otherwise it fails closed
  (`route_inactive` / `route_identity_changed` / `route_snapshot_changed` /
  `route_snapshot_expired`). The pool also requires each candidate's
  `snapshot_expires_at >= pool.expires_at`.
- **`RouteAuthority`** comes from `ProviderLifecycleService.route_authority`,
  which reads `current_observations` (the lifecycle store) and the
  `lifecycle_snapshot_bindings` (the routing store) and only emits an authority
  when the two agree on the current identity.
- **`ApprovalAuthority.issue` / `.consume`** sign and single-use-consume the
  exact ordered candidate tuple.
- Route-pool OpenRouter parity is already covered by tests
  (`tests/test_route_pool.py::test_every_provider_can_be_an_exact_preapproved_candidate`
  is parametrized over OpenRouter; `tests/test_routing_profiles.py` proves the
  storage round-trip and the read-only→workspace-write promotion). Those tests
  use synthetic digests; this proof uses the *real* promoted snapshots.

## Live state (confirmed 2026-07-20, read-only)

- Routing store `events.sqlite3`: 12 capability_snapshots / 12
  lifecycle_snapshot_bindings / 21 cli_telemetry_events.
- Both target models have an ACTIVE OpenRouter lifecycle identity in
  `provider-lifecycle.sqlite3` (activated at verification); the three unverified
  models remain `verification_required`. All OpenRouter observations share
  `routing_policy_digest 916df225…`.
- Edit-promoted snapshots: `kimi-k2.7-code` → `500cda19…`, `kimi-k2.6` →
  `1b2a7c8e…` (verification snapshots `99db4a4a…` / `6816edaf…` respectively).

## Pool composition being registered

One pool, task category ISOLATED_CODE, permission mode WORKSPACE_WRITE:

| position | model | capability snapshot | role |
|---|---|---|---|
| `candidate[0]` | `moonshotai/kimi-k2.7-code` | `500cda19…` (edit) | primary |
| `candidate[1]` | `moonshotai/kimi-k2.6` | `1b2a7c8e…` (edit) | capacity fallback |

Both carry their ACTIVE `lifecycle_identity_digest`, `model_identity_digest`,
and the shared `routing_policy_digest 916df225…`. Primary is the
coding-specialized `kimi-k2.7-code`; `kimi-k2.6` is the capacity fallback (used
only after a `capacity_unavailable` failure of the primary).

## Component: the offline proof harness

Two scripts in `F:\tmp\graphite-live-acceptance-harness`, following the rN
conventions but **non-inference and read-only** (no network, no store write):

- `_prepare_openrouter_pool_registration.py` — assembles a bundle dict
  declaring the exact ordered candidates (the two edit-snapshot digests, the
  ACTIVE identity digests, model/routing-policy digests, category, permission
  mode, expiry), computes `BUNDLE_DIGEST`, and prints the bundle for operator
  display. Because the run is offline, the bundle documents *what is registered*
  rather than authorizing a spend.
- `_execute_openrouter_pool_registration.py --approved <digest>`:
  1. Manifest preflight (approval-digest match, expiry, digest recompute,
     implementation-commit match, clean feature worktree, store hash pins).
  2. Read-only resolution: for each model, load its edit capability snapshot,
     its `lifecycle_snapshot_bindings` entry, and its `current_observations`
     row; assert the binding maps to an ACTIVE identity and read
     `model_identity_digest` / `snapshot_expires_at` / capabilities /
     `context_window_tokens` from the stored records.
  3. Construct the two `ApprovedRouteCandidate`s and the `ApprovedRoutePool`,
     and confirm it issues via `ApprovalAuthority.issue` — a
     constructibility/approvability check. (`select_route` itself operates on
     the pool plus live authorities and does not require the signature; the
     signed approval is only consumed by `execute_approved_route_pool` on the
     live routed path, which is out of scope here.)
  4. Derive live `RouteAuthority`s from the stores and run `select_route`:
     - `attempts=[]` → must select `candidate[0]` (k2.7-code), ACTIVE, digests
       matching;
     - after a synthesized `capacity_unavailable` attempt on `candidate[0]` →
       must select `candidate[1]` (k2.6).
  5. Final read-only audit: assert the store is byte-unchanged (12/12/21, same
     file hashes) — the proof mutates nothing.
  6. Print one sanitized JSON receipt: per-candidate `{loadable, selectable}`
     booleans, the selected provider/model at each position, candidate digests,
     pool/candidate expiry, and `mutated: false`. No prompts, file contents,
     credentials, or raw provider data.

## The proof's decisive diagnostic

`route_authority` validates a candidate's `capability_snapshot_digest` through
its `lifecycle_snapshot_bindings` entry against the **current** ACTIVE identity.
The ACTIVE identities were activated during *verification* (bound then to the
verification snapshots); edit-promotion re-bound the *edit* snapshots to the
same identities but did not re-activate. Because the check is
binding-based and the edit snapshots are bound to the active identities, the
edit-snapshot candidates are **expected to be directly selectable**. The proof
confirms this empirically and, if instead it surfaces `route_snapshot_changed` /
`route_inactive`, that is a clean, zero-cost finding indicating a governed
re-activation to the edit snapshot is a prerequisite — a separate follow-on
step, not a failure of this artifact.

## Error handling (fail-closed, sanitized)

- Manifest/approval/expiry/digest/commit/worktree/store-hash mismatch → the
  standard harness failure codes, before any resolution.
- A target snapshot missing, unbound, expired, or bound to a non-ACTIVE
  identity → a sanitized failure category (`pool_candidate_source_expired` /
  `pool_candidate_unbound` / `pool_candidate_inactive`), no selection attempted.
- `ApprovedRouteCandidate` / `ApprovedRoutePool` `__post_init__` rejection
  (e.g. missing `routing_policy_digest`) → surfaced as `pool_candidate_invalid`.
- `select_route` fail-closed code (`route_inactive` / `route_identity_changed` /
  `route_snapshot_changed` / `route_snapshot_expired`) → recorded verbatim as
  the diagnostic outcome.
- Any post-run store drift → `pool_registration_mutated_store` (must never
  happen; the run is read-only).

Receipts carry digests, booleans, categories, and expiries only — never raw
provider output, prompts, credentials, or file contents.

## Governance

Offline and read-only, so **no live-inference approval is required**. But
because the parent spec frames pool registration as an approval-gated manifest
step, the composition manifest (the exact ordered candidates and their digests)
is displayed for explicit operator approval before the proof runs. The operator
is approving *what is registered*, not a spend. The `--approved <digest>` gate
is retained for provenance and to bind the exact candidate set.

## Evidence

Append a pool-registration section to
`docs/superpowers/implementation-notes/2026-07-19-provider-lifecycle-evidence.md`
with the sanitized receipt and the selectability outcome; commit.

## Testing / verification

- Offline dry check (before requesting approval): `python -m py_compile` both
  scripts; run the prepare script to confirm the digest computes; a monkeypatched
  dry run that feeds a synthetic store view through the candidate construction +
  `select_route` control flow to confirm the harness logic (mirrors the r7/r8
  dry-run discipline).
- The existing route-pool and profile tests must remain green (no code change is
  expected; if the harness surfaces a genuine loader gap, any minimal fix gets
  its own test).

## Success criteria

- Both edit-promoted OpenRouter models load as eligible snapshots and construct
  as valid `ApprovedRouteCandidate`s for ISOLATED_CODE.
- `select_route` selects `candidate[0]` (k2.7-code) with `attempts=[]` and
  `candidate[1]` (k2.6) after a capacity failure, each against its live ACTIVE
  `RouteAuthority`, all digests matching.
- The store is byte-unchanged after the run (`mutated: false`).
- Evidence recorded and committed.

## Out of scope (deferred)

- The read-only review / authorization pool (OpenRouter reviewing CLI-authored
  diffs).
- A live routed smoke through `execute_approved_route_pool` (executing a real
  routed OpenRouter action through the signed pool).
- The three unverified models (`kimi-k3`, `glm-5.2`, `muse-spark-1.1`).
- Any lifecycle re-activation (only performed if the proof shows it is a
  prerequisite, and then as a separate governed step).
