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
   Carry-forward never alters or extends snapshot life — expiry always
   reads from the original snapshot row. The short TTL is the compensating
   control: carried authority can never outlive the day its verification
   ran in.
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

Patch-level version downgrades (for example 2.1.32 back to 2.1.31) classify
as `patch` and carry on the same terms — the policy minimum version and the
standard probe still gate them; this deliberately supports rolling back a bad
CLI update without a re-verification round.

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

Recording a carry-forward is two single-store transactions in a fixed,
fail-closed order (the bindings live in the routing store and the
observation in the lifecycle store — separate SQLite databases, so one
cross-store transaction is not available):

1. one routing-store transaction inserting one row into the new
   append-only `lifecycle_binding_carries` table per currently-bound,
   unexpired capability snapshot of the boundary, re-pointing its
   effective binding to the new identity digest (the original
   `lifecycle_snapshot_bindings` row is immutable and is superseded at
   read time by the latest carry row), followed by invalidation of
   approvals still pinned to the previous identity; then
2. one lifecycle-store write recording the `active` → `active`
   observation with reason `patch_carried_forward`.

A failure between the two leaves route authority denied (bindings name
the new identity while the current observation still names the old one)
and the next observation of the same binary retries and completes the
carry.

Each carry row records the previous effective identity digest, the new
identity digest, the event id of the `patch_carried_forward` lifecycle
event that justified it, and the carry time. Expiry is never copied or
altered — it always reads from the immutable snapshot row itself. Expired
snapshots are not re-bound. Snapshot rows are never modified: a snapshot's
embedded identity remains the binary it was actually verified on, and the
binding chain is the record of inheritance.

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
carry rows referencing the immediate predecessor, so the audit trail names
every binary in the chain. The chain is bounded by the snapshot TTL — carried
authority dies at the original `expires_at` no matter how many patches landed.
No separate chain-length cap is added (YAGNI; the TTL is the bound). A chain
cannot silently cross a minor boundary: classification always compares against
the current identity, so any minor-version bump classifies as `minor` and
fail-closes.

## 9. Storage and audit

The routing store gains the append-only `lifecycle_binding_carries` table
(schema v7 → v8, with a pre-migration backup and a rollback fixture); the
approval-binding insert guard is recreated carry-aware.
`verified_at`/`expires_at` are never copied or altered — expiry always reads
from the immutable snapshot row itself. Append-only integrity rules are
preserved; no existing rows are updated or deleted. The carry audit trail is
the `patch_carried_forward` lifecycle event (provider, runtime kind, old and
new identity digests, states, policy version, occurred-at) plus one carry row
per re-bound snapshot referencing that event's id — no secrets, prompts,
source, or raw provider diagnostics anywhere.

## 10. Error handling

- Probe failure on the new binary: `incompatible` / `probe_failed`, no carry.
- Version outside policy range: `incompatible` / `policy_range_unsupported`.
- Store failure mid-carry: each store's transaction is atomic; a failure
  between the two stores leaves carry rows written but the observation
  unrecorded — route authority stays denied and the next observation
  retries and completes the carry. A heal at a later observation time
  records its own event; carry rows written before the failure keep the
  originally computed event id — they still name both identities in full, so
  the audit chain remains reconstructible.
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
- Re-binding: unexpired snapshots re-bound via new
  `lifecycle_binding_carries` rows, with expiry always read from the
  immutable snapshot row (never copied); expired snapshots excluded; each
  carry row records the previous and new identity digests, the justifying
  event id, and the carry time; snapshot rows byte-unchanged;
  store-failure fail-closed behavior (induced failure between the two
  stores leaves carry rows written but the observation unrecorded, healed
  on the next observation).
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
