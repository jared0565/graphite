# Patch Carry-Forward of Capability Authority Design

**Date:** 2026-07-24
**Status:** Approved for implementation planning
**Scope:** Lifecycle authority semantics for LOCAL_CLI providers (Claude Code,
Codex) on patch-level version changes

## 1. Objective

Stop patch-level CLI updates from making Claude Code and Codex unusable for
governed development routing. Both CLIs release updates frequently; under the
current rules every identity change drops the lifecycle boundary to
`verification_required`, so verified capability authority rarely survives long
enough to be used. After this change, a patch update within the supported
version family (for example 2.1.31 to 2.1.32) carries existing verified
authority forward automatically once a standard non-inference probe passes.

## 2. Operator decisions (2026-07-24)

1. **Inheritance scope: PATCH only.** A version-bump patch update within the
   minor family carries authority forward. `hash_only` changes (executable
   bytes changed while the version claims to be identical) remain fail-closed:
   that is a tampering signature, normal updates always bump the version, and
   excluding it costs nothing in availability. `minor` and `major` changes keep
   today's behavior.
2. **TTL unchanged.** Capability snapshots keep their 24-hour expiry.
   Carry-forward copies `verified_at` and `expires_at` verbatim and never
   extends snapshot life. The short TTL is the compensating control: carried
   authority can never outlive the day its verification ran in.
3. **Automatic on probe pass.** The carry-forward is recorded the moment the
   observed patch change passes the standard probe; no operator round is
   required for the carry itself. The per-run governance gate is untouched:
   nothing executes without a fresh manifest and explicit operator approval.
4. **Mechanism: append-only carry-forward event** (approach A). Exact-digest
   checks stay strict everywhere downstream; the store records an auditable
   old-identity to new-identity chain. Rejected: family-scoped comparisons at
   check time (weakens exact-match invariants and audit honesty); unattended
   auto-renewal (live inference without approval — a governance breach).

## 3. Non-goals

- Any change for `hash_only`, `minor`, `major`, `model_digest`, `endpoint`,
  `capability`, `routing_policy`, or `policy` identity changes
- Any change for remote providers (OpenRouter, z.ai) or any non-`LOCAL_CLI`
  runtime kind
- Extending or refreshing snapshot TTLs
- Automatic verification, inference, or credential use of any kind during
  carry-forward
- Resurrecting authority a boundary does not currently hold (carry-forward
  requires the boundary to be `active` at observation time)
- Weakening the per-run manifest + operator-approval execution gate

## 4. Supersession note

The 2026-07-19 provider-lifecycle design's acceptance criterion "Every
identity change invalidates old capability authority" is amended to: "Every
identity change invalidates old capability authority, except a `patch` change
on a `LOCAL_CLI` boundary in state `active` whose standard probe passes and
whose new version remains inside the compatibility policy range; that change
carries existing unexpired authority forward through an append-only, audited
re-binding." All other criteria of that design stand unchanged.

## 5. Lifecycle semantics

`assess_identity_change` gains exactly one new outcome branch. When all of the
following hold:

- the classified change is `IdentityChange.PATCH`,
- the boundary's runtime kind is `RuntimeKind.LOCAL_CLI`,
- the existing lifecycle state is `ProviderLifecycleState.ACTIVE`,
- the standard probe passed, and
- the compatibility policy still admits the new version (existing
  `policy.supports` and required-capability checks, evaluated before this
  branch exactly as today),

the assessment returns state `ACTIVE` with a new reason code
`LifecycleReasonCode.PATCH_CARRIED_FORWARD` and probe level `STANDARD`.

Every other path is behavior-identical to today. In particular:

- `patch` observed while the boundary is in any state other than `active`
  (including `verification_required`) still yields `verification_required`
  with reason `patch_changed` — carry-forward never resurrects authority.
- `hash_only` still yields `verification_required` with reason `hash_changed`.
- `patch` on a non-`LOCAL_CLI` boundary still yields `verification_required`.
- A failed standard probe still yields `incompatible` with `probe_failed`.
- `major` still yields `incompatible`; range or capability violations still
  yield `incompatible` with their existing reasons.

`_VALID_TRANSITIONS` admits `ACTIVE -> ACTIVE` solely for
`PATCH_CARRIED_FORWARD`; the transition validator rejects `ACTIVE -> ACTIVE`
under any other reason code.

## 6. Snapshot re-binding

Recording a carry-forward is one atomic store transaction containing:

1. the new current observation for the boundary (new identity, state
   `active`, reason `patch_carried_forward`), and
2. one new binding row per currently-bound, unexpired capability snapshot of
   that boundary, re-pointing the snapshot to the new identity digest.

Each new binding row records: the predecessor binding it supersedes, a
carried-forward marker, and the digest of the probe receipt that justified the
carry. `verified_at` and `expires_at` are copied verbatim from the predecessor
binding's snapshot. Expired snapshots are not re-bound. Snapshot rows are
never modified: a snapshot's embedded identity remains the binary it was
actually verified on, and the binding chain is the record of inheritance.
If the transaction aborts, nothing is recorded; the old observation remains
current and the next observation cycle retries.

A boundary with no unexpired snapshots still carries forward (the observation
is recorded and the boundary stays `active` with zero re-bound snapshots);
routing then requires a renewal round exactly as after TTL expiry today.

## 7. Downstream invariants preserved

- `_validate_authority` in `route_pool.py` keeps its exact-digest checks
  unchanged. Fresh pool manifests pin the post-carry digests naturally; a
  stale pool authored before the update still fails `route_identity_changed`.
- Execution-time identity rechecks, budgets, cost ceilings, risk ceilings,
  worktree isolation, and the manifest + approval flow are untouched.
- Carry-forward performs no inference, reads no credentials beyond the
  existing standard probe's auth-health check, and writes no secrets.
- Canonical graph isolation is unaffected (routing-only change).

## 8. Chaining and bounds

Consecutive patch updates chain: each step writes its own observation and
binding rows referencing the immediate predecessor, so the audit trail names
every binary in the chain. The chain is bounded by the snapshot TTL — carried
authority dies at the original `expires_at` no matter how many patches landed.
No separate chain-length cap is added (YAGNI; the TTL is the bound). A chain
cannot silently cross a minor boundary: classification always compares against
the current identity, so any minor-version bump classifies as `minor` and
fail-closes.

## 9. Storage and audit

The lifecycle store's binding table gains the predecessor reference, the
carried-forward marker, and the probe-receipt digest, under the store's
standard versioned-migration mechanism with a rollback fixture (the
implementation plan pins the exact schema version numbers after reading
`lifecycle_storage.py`). Append-only integrity rules are preserved; no
existing rows are updated or deleted. The carry-forward lifecycle event
carries: provider, runtime kind, old identity digest, new identity digest,
old and new versions, probe level, and the count of re-bound snapshots — no
secrets, prompts, source, or raw provider diagnostics.

## 10. Error handling

- Probe failure on the new binary: `incompatible` / `probe_failed`, no carry.
- Version outside policy range: `incompatible` / `policy_range_unsupported`.
- Store transaction failure: no partial state; old observation stays current;
  retried on the next observation.
- Malformed or unparsable version output: existing probe error paths
  (`probe_version_invalid`), unchanged.

## 11. Testing

All offline, deterministic fake CLI executables, no subscription or network
use:

- Assessment: the new branch returns `ACTIVE` / `PATCH_CARRIED_FORWARD`; and
  each negative gate — `hash_only` still drops, `patch` on a remote/non-CLI
  boundary still drops, `patch` from `verification_required` does not
  resurrect, failed probe yields `incompatible`, out-of-range version yields
  `incompatible`.
- Transitions: `ACTIVE -> ACTIVE` valid only with `PATCH_CARRIED_FORWARD`;
  rejected under any other reason.
- Re-binding: unexpired snapshots re-bound with `verified_at`/`expires_at`
  copied verbatim; expired snapshots excluded; predecessor chain and probe
  receipt digest recorded; snapshot rows byte-unchanged; transaction
  atomicity (induced failure leaves the store at the prior state).
- Chaining: two consecutive patches produce a two-link audit chain; a
  subsequent minor bump fail-closes.
- Storage: schema migration forward and rollback fixtures; append-only
  triggers still enforced.
- Route pool: carried-forward authority selects under a freshly-pinned pool;
  a pool pinning pre-update digests still fails `route_identity_changed`.

Live acceptance (observing a real patch update on the live fixture, then a
routed smoke on carried authority) is a separate, operator-gated decision and
is out of scope for the offline implementation.

## 12. Acceptance criteria

- A `LOCAL_CLI` patch update on an `active` boundary with a passing standard
  probe keeps the boundary `active` and re-binds all unexpired snapshots to
  the new identity, atomically and append-only.
- No other identity-change class, runtime kind, or starting state changes
  behavior.
- Carried authority expires at the original snapshot `expires_at`.
- The audit chain names every inherited-from and inherited-to identity with
  probe evidence.
- The full offline suite passes with no live provider contact; canonical
  graph outputs are byte-identical before and after the change.
