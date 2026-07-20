# OpenRouter ISOLATED_CODE Edit-Pool Registration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove — offline, read-only, with zero store mutation — that the two edit-promoted OpenRouter models are loadable, correctly ordered `ApprovedRoutePool` candidates for the ISOLATED_CODE edit category and that `select_route` selects them in order against their live ACTIVE `RouteAuthority`s.

**Architecture:** A governed prepare/execute harness pair in `F:\tmp\graphite-live-acceptance-harness`, following the established rN convention but **non-inference and read-only**. `_prepare_openrouter_pool_registration.py` pins the exact ordered candidate composition and computes a `BUNDLE_DIGEST` for operator approval. `_execute_openrouter_pool_registration.py --approved <digest>` reads the live stores read-only, reconstructs both `ApprovedRouteCandidate`s and the `ApprovedRoutePool` from live records, asserts each live value equals the pinned expectation, derives live `RouteAuthority`s, runs `select_route` twice (empty attempts → primary; one synthesized capacity failure → fallback), asserts the stores are byte-unchanged, and prints one sanitized JSON receipt. No `graphite` source changes are expected.

**Tech Stack:** Python 3.14, `graphite.routing` (`route_pool`, `lifecycle_service`, `lifecycle_storage`, `storage`, `profiles`, `lifecycle`, `contracts`), SQLite (WAL), sha256 canonical-JSON digests.

## Global Constraints

- **Offline and read-only.** No network, no live inference, no CLI/Codex/Claude call. The only credential-bearing paths are untouched (this proof never reads `OPENROUTER_API_KEY`).
- **Zero store mutation.** The run must leave `events.sqlite3` and `provider-lifecycle.sqlite3` byte-identical. Empirically verified safe: both stores are already SQLite WAL (`write/read version = 2`), so the store's `PRAGMA journal_mode = WAL`-on-connect is a no-op on the main file, and read-only queries append no WAL frames — a hash dry-check confirmed both main files are byte-unchanged after the full read path with no sidecars left behind.
- **Do NOT call `ApprovalAuthority.issue`.** Spec step 3 framed approvability as an `issue()` check, but `ApprovalAuthority.issue` calls `store.save_approval_record(...)`, which **writes** to the routing store and would violate `mutated: false`. Resolution (approved in advance with the advisor; flag at handoff): prove approvability by successful `ApprovedRoutePool.__post_init__` construction — that is the check that enforces the full cross-candidate envelope (permission/risk/trust/capability/expiry/budget). `select_route` does not require the HMAC signature; the signed-approval path is exercised by existing `test_route_pool`/`approval` tests and by the (out-of-scope) live routed smoke.
- **No `forbidden_persistence`.** Receipts carry only digests, booleans, categories, counts, and expiries — never prompts, file contents, credentials, raw provider output, or stderr/stdout bodies. `raw_provider_output_persistence: false`.
- **Never touch `F:\Projects\graphite` (main).** No merge, no push, no deploy. Work only on `feat/claude-codex-router` in `F:\tmp\graphite-claude-codex-router` and in the harness dir.
- **Append-only stores are sacred.** `capability_snapshots` and `lifecycle_snapshot_bindings` carry append-only triggers; this proof writes nothing, so triggers are never engaged.
- **Expiry runway is finite.** The edit snapshots expire at `1784648044` (k2.7-code) / `1784654851` (k2.6). As read on 2026-07-20 that is ≈ 19.3 h / 21.2 h of runway. **The governed run (Task 3) must happen before the k2.7-code snapshot expires**, else `select_route` returns `route_snapshot_expired` and a governed re-promotion (out of scope) is required first.
- **The classifier blocks the agent's own shell from the governed execute.** Task 3's live-store run is executed by the operator in-session via the `!` bash prefix, with `PYTHONPATH="F:/tmp/graphite-claude-codex-router/src"`. Offline dry-runs (Tasks 1–2) are runnable by the agent directly.

---

## Component / File Structure

Two new harness scripts plus two offline dry-run checks and one evidence append. No `graphite` source or test changes are expected.

- Create: `F:\tmp\graphite-live-acceptance-harness\_prepare_openrouter_pool_registration.py` — pinned composition manifest + `BUNDLE_DIGEST`.
- Create: `F:\tmp\graphite-live-acceptance-harness\_execute_openrouter_pool_registration.py` — read-only proof + sanitized receipt.
- Create (scratchpad, not committed): `dryrun_pool_construct.py` — offline constructibility check of the pinned candidate/pool specs.
- Create (scratchpad, not committed): `dryrun_pool_registration.py` — offline end-to-end control-flow check against a store **copy**.
- Modify: `F:\tmp\graphite-claude-codex-router\docs\superpowers\implementation-notes\2026-07-19-provider-lifecycle-evidence.md` — append the pool-registration evidence section.

### Pinned facts (verified live 2026-07-20, read-only)

Store contract (routing `events.sqlite3` / lifecycle `provider-lifecycle.sqlite3`):

- Counts: 12 capability_snapshots / 12 lifecycle_snapshot_bindings / 21 cli_telemetry_events; `integrity ok`; `foreign_key_violations 0`; `schema_version 6`.
- Current file hashes: routing `21e73d3f8ac2e9780feaefbfeb1c6513f309d9e32ba75451b5fa9e23ce27ef49`; lifecycle `a99f7cc454e4b8ad000c1d0ba6b203b44cf6d12c3fef1b9c57d428b236bef890`.
- `FIXTURE_COMMIT = 05f01737326469bd951ec677a0cf73b68caf9fe1`; `FIXTURE = F:\tmp\graphite-production-live-fixture`.

Per-model boundary digest = `sha256(json.dumps({"fixture": FIXTURE_COMMIT, "model": slug, "provider": "openrouter", "runtime": "remote-https"}, sort_keys=True, separators=(",",":")))`:

| field | `candidate[0]` = primary | `candidate[1]` = capacity fallback |
|---|---|---|
| requested/effective model | `moonshotai/kimi-k2.7-code` | `moonshotai/kimi-k2.6` |
| boundary digest | `e28276006bea9e48641c28517b075843e9239150de2a8b9fa5169b03e5f083e5` | `1df7e44485e53f9bdf4e7ab08fa1d17fc1624b2a7d03f536dabefa49affa80be` |
| capability_snapshot_digest (edit) | `500cda19a907c53df9433dde2ec4cab58f0aa10e109420417fbbd3bac7ec9574` | `1b2a7c8e99452b1ff1132545b160e88f4f6abd7864154e05b413faa310f15936` |
| lifecycle_identity_digest (ACTIVE) | `c8cece35646deec30fa9538ba998722781074027a6bdd2dabafd1986359439ab` | `6333c4d577f8fcb111f57f23c4c2f6b3ab889cc3ba19edfb78cc082a63f6bade` |
| model_identity_digest | `939d3a5af17f6d5ec5ccfa05ac09134873d4258357865f59829f96b94a392836` | `e504400884ef6c43f66fc983060d8da6d3ea81fe189e491f09a25775e4b0b10b` |
| routing_policy_digest | `916df225733c25f6d9976d29edb6b7a2050f61457fa978e178544bcd53615b39` | `916df225733c25f6d9976d29edb6b7a2050f61457fa978e178544bcd53615b39` |
| capabilities | code, reasoning, thinking, vision | code, reasoning, tools, vision |
| context_window_tokens | 262144 | 262144 |
| risk_ceiling | high | high |
| permission_mode | workspace-write | workspace-write |
| supported_efforts | high | high |
| snapshot_expires_at | 1784648044 | 1784654851 |

Derived pool envelope:

- Task category: ISOLATED_CODE; `permission_mode = WORKSPACE_WRITE`; `task_risk = LOW` (≤ both candidates' `high` ceiling).
- `required_capabilities = ("code", "reasoning", "vision")` (the intersection of both candidates' capabilities; subset of each).
- `allowed_fallback_reasons = ("capacity_unavailable",)`; `max_attempts = 2`; `allow_cross_provider = False` (both candidates are OpenRouter).
- `max_input_tokens = 2048`, `max_output_tokens = 1024` (sum 3072 ≤ 262144 context of each candidate).
- `max_duration_ms = 120000`; `max_cost_microunits = None` (offline; no spend modelled).
- `graph_fingerprint = 6f1633953ecd0b7cd7c4009595f6da57866b6eea1aeca44e65c66e98b318ac21` (fixture graph).
- `repository_commit = FIXTURE_COMMIT`.
- `expires_at = 1784648044` (= the tighter snapshot expiry, `k2.7-code`; satisfies `candidate.snapshot_expires_at >= pool.expires_at` for both); `issued_at` = a pinned value `< expires_at` and `< now` (use `1784578461`).
- `trust_policy_digest = sha256(json.dumps(TRUST_POLICY, sort_keys=True, separators=(",",":")))` where `TRUST_POLICY = {"category": "isolated-code", "permission_mode": "workspace-write", "policy": "openrouter-edit-pool", "version": "1.0.0"}` — identical on both candidates and the pool. (Free field; not cross-checked against the store.)
- `candidate_id`s: `"openrouter-kimi27-code-primary"`, `"openrouter-kimi26-fallback"`.
- `context_manifest_hash = sha256(json.dumps(CONTEXT_MANIFEST, sort_keys=True, separators=(",",":")))` where `CONTEXT_MANIFEST = {"category": "isolated-code", "fixture": FIXTURE_COMMIT, "manifest": "openrouter-edit-pool-registration"}`.
- `approval_id = "openrouter-pool-registration-1"`, `task_id = "openrouter-pool-registration"`, `decision_id = "openrouter-pool-registration-decision-1"`, `worktree_id = "openrouter-pool-registration"`, `policy_version = "1.0.0"`, `nonce = "openrouter-pool-registration-nonce-1"`.

### Reference: exact API shapes this plan consumes

```python
# graphite.routing.route_pool
ApprovedRouteCandidate(               # frozen; OpenRouter REQUIRES routing_policy_digest
    candidate_id, provider, runtime_kind, lifecycle_identity_digest,
    capability_snapshot_digest, model_identity_digest, routing_policy_digest,
    requested_model, effective_model, effort, permission_mode, risk_ceiling,
    trust_policy_digest, capabilities, context_window_tokens, snapshot_expires_at,
)  # .digest -> canonical sha256
ApprovedRoutePool(
    approval_id, task_id, decision_id, candidates, required_capabilities, task_risk,
    permission_mode, trust_policy_digest, graph_fingerprint, context_manifest_hash,
    repository_commit, worktree_id, allow_cross_provider, allowed_fallback_reasons,
    max_attempts, max_input_tokens, max_output_tokens, max_duration_ms,
    max_cost_microunits, policy_version, issued_at, expires_at, nonce,
)  # .digest -> canonical sha256
RouteAttemptEvidence(
    candidate_id, candidate_digest, attempt_ordinal, failure_category,
    accepted_output, side_effect_state, input_tokens, output_tokens, duration_ms,
    cost_microunits,
)
select_route(pool, authorities: tuple[RouteAuthority,...],
             attempts: tuple[RouteAttemptEvidence,...], *, now: int) -> RouteSelection
# RouteSelection.candidate is the selected ApprovedRouteCandidate.
# Fail-closed codes: route_inactive / route_identity_changed / route_snapshot_changed
#   / route_snapshot_expired / route_pool_expired / route_authority_invalid / ...

# graphite.routing.lifecycle_service.ProviderLifecycleService(lifecycle_store, routing_store)
#   .route_authority(boundary_digest, candidate) -> RouteAuthority
#     reads current_observation(boundary) + lifecycle_identity_binding(capability_snapshot,
#     candidate.capability_snapshot_digest); requires binding == current.identity.digest.

# graphite.routing.storage.RepositoryStore(root)         # root/.graphite/routing/events.sqlite3
#   .capability_snapshot_records(*, limit)  # 1<=limit<=64
#   .lifecycle_identity_binding(*, authority_kind="capability_snapshot", authority_id=digest)
# graphite.routing.lifecycle_storage.LifecycleStore(root) # root/.graphite/routing/provider-lifecycle.sqlite3
#   .current_observation(boundary_digest) -> CurrentLifecycleObservation | None
#     .identity: ProviderRuntimeIdentity(.digest, .model_identity_digest,
#                .routing_policy_digest, .provider, .runtime_kind, .capabilities)
#     .state: ProviderLifecycleState  (.ACTIVE)
# graphite.routing.profiles.load_verified_capability_snapshots(store, *, now) -> tuple[CapabilitySnapshot,...]
#   CapabilitySnapshot.digest / .expires_at / .profile.{capabilities, context_window_tokens,
#     risk_ceiling, effective_model, requested_model, permission_mode, supported_efforts}
```

---

### Task 1: Prepare manifest script (`_prepare_openrouter_pool_registration.py`)

**Files:**
- Create: `F:\tmp\graphite-live-acceptance-harness\_prepare_openrouter_pool_registration.py`
- Test (scratchpad, offline): `C:\Users\fbmac\AppData\Local\Temp\claude\F--Projects-graphite\4d7429e7-ce78-4970-93c8-fdcf8e95f1e8\scratchpad\dryrun_pool_construct.py`

**Interfaces:**
- Consumes: the pinned facts above; `graphite.routing.route_pool.{ApprovedRouteCandidate, ApprovedRoutePool}`, `graphite.routing.lifecycle.{LifecycleProviderId, RuntimeKind}`, `graphite.routing.contracts.{Effort, PermissionMode, RiskTier}`.
- Produces (module-level names the execute script imports): `FIXTURE`, `ROUTING_PATH`, `LIFECYCLE_PATH`, `FIXTURE_COMMIT`, `IMPLEMENTATION_COMMIT`, `ROUTING_STORE_SHA256`, `LIFECYCLE_STORE_SHA256`, `EXISTING_STORE_CONTRACT`, `ISSUED_AT`, `EXPIRES_AT`, `POOL_NOW`, `CANDIDATE_SPECS` (ordered list of dicts), `POOL_SPEC` (dict), `TRUST_POLICY_DIGEST`, `CONTEXT_MANIFEST_HASH`, `BUNDLE`, `BUNDLE_DIGEST`, and helpers `digest(value)`, `boundary(slug)`, `build_candidate(spec)`, `build_pool(candidates)`.

- [ ] **Step 1: Write the prepare script**

Create `_prepare_openrouter_pool_registration.py` with:

```python
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from graphite.routing.contracts import Effort, PermissionMode, RiskTier
from graphite.routing.lifecycle import LifecycleProviderId, RuntimeKind
from graphite.routing.route_pool import ApprovedRouteCandidate, ApprovedRoutePool

FIXTURE = Path(r"F:\tmp\graphite-production-live-fixture")
ROUTING_PATH = FIXTURE / ".graphite" / "routing" / "events.sqlite3"
LIFECYCLE_PATH = FIXTURE / ".graphite" / "routing" / "provider-lifecycle.sqlite3"

FIXTURE_COMMIT = "05f01737326469bd951ec677a0cf73b68caf9fe1"
FIXTURE_GRAPH = "6f1633953ecd0b7cd7c4009595f6da57866b6eea1aeca44e65c66e98b318ac21"
# No graphite source change is expected for this proof. Set IMPLEMENTATION_COMMIT to
# the CURRENT clean feature HEAD at bundle-prep time (Task 3, Step 1 pins it).
IMPLEMENTATION_COMMIT = "fe8882007adc154f6b8c8eca323667905477998b"

# Current on-disk hashes (post-r8). Re-pin in Task 3, Step 1 if the store advanced.
ROUTING_STORE_SHA256 = "21e73d3f8ac2e9780feaefbfeb1c6513f309d9e32ba75451b5fa9e23ce27ef49"
LIFECYCLE_STORE_SHA256 = "a99f7cc454e4b8ad000c1d0ba6b203b44cf6d12c3fef1b9c57d428b236bef890"

ISSUED_AT = 1784578461
EXPIRES_AT = 1784648044  # = min(edit snapshot expiry) = k2.7-code snapshot_expires_at
# POOL_NOW is the frozen `now` select_route is evaluated at. Freezing it is safe ONLY
# because EXPIRES_AT == min(snapshot expiry) AND the execute preflight fails closed on
# `time.time() >= EXPIRES_AT`: that interlock guarantees the proof cannot report a pass
# after either snapshot has expired. Do NOT bump POOL_NOW past a snapshot expiry to
# "fix" a run — re-promote the snapshot instead. (Real `int(time.time())` would be
# equally safe given the same preflight gate; frozen is chosen for a deterministic digest.)
POOL_NOW = 1784578461


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def boundary(slug: str) -> str:
    return digest(
        {"fixture": FIXTURE_COMMIT, "model": slug, "provider": "openrouter", "runtime": "remote-https"}
    )


TRUST_POLICY = {
    "category": "isolated-code",
    "permission_mode": "workspace-write",
    "policy": "openrouter-edit-pool",
    "version": "1.0.0",
}
TRUST_POLICY_DIGEST = digest(TRUST_POLICY)
CONTEXT_MANIFEST = {
    "category": "isolated-code",
    "fixture": FIXTURE_COMMIT,
    "manifest": "openrouter-edit-pool-registration",
}
CONTEXT_MANIFEST_HASH = digest(CONTEXT_MANIFEST)

EXISTING_STORE_CONTRACT = {
    "schema_version": "6",
    "capability_snapshots": 12,
    "lifecycle_snapshot_bindings": 12,
    "telemetry_events": 21,
    "foreign_key_violations": 0,
    "integrity": "ok",
}

CANDIDATE_SPECS = [
    {
        "candidate_id": "openrouter-kimi27-code-primary",
        "role": "primary",
        "slug": "moonshotai/kimi-k2.7-code",
        "boundary_digest": boundary("moonshotai/kimi-k2.7-code"),
        "capability_snapshot_digest": "500cda19a907c53df9433dde2ec4cab58f0aa10e109420417fbbd3bac7ec9574",
        "lifecycle_identity_digest": "c8cece35646deec30fa9538ba998722781074027a6bdd2dabafd1986359439ab",
        "model_identity_digest": "939d3a5af17f6d5ec5ccfa05ac09134873d4258357865f59829f96b94a392836",
        "routing_policy_digest": "916df225733c25f6d9976d29edb6b7a2050f61457fa978e178544bcd53615b39",
        "capabilities": ("code", "reasoning", "thinking", "vision"),
        "context_window_tokens": 262144,
        "risk_ceiling": "high",
        "permission_mode": "workspace-write",
        "effort": "high",
        "snapshot_expires_at": 1784648044,
    },
    {
        "candidate_id": "openrouter-kimi26-fallback",
        "role": "capacity_fallback",
        "slug": "moonshotai/kimi-k2.6",
        "boundary_digest": boundary("moonshotai/kimi-k2.6"),
        "capability_snapshot_digest": "1b2a7c8e99452b1ff1132545b160e88f4f6abd7864154e05b413faa310f15936",
        "lifecycle_identity_digest": "6333c4d577f8fcb111f57f23c4c2f6b3ab889cc3ba19edfb78cc082a63f6bade",
        "model_identity_digest": "e504400884ef6c43f66fc983060d8da6d3ea81fe189e491f09a25775e4b0b10b",
        "routing_policy_digest": "916df225733c25f6d9976d29edb6b7a2050f61457fa978e178544bcd53615b39",
        "capabilities": ("code", "reasoning", "tools", "vision"),
        "context_window_tokens": 262144,
        "risk_ceiling": "high",
        "permission_mode": "workspace-write",
        "effort": "high",
        "snapshot_expires_at": 1784654851,
    },
]

POOL_SPEC = {
    "approval_id": "openrouter-pool-registration-1",
    "task_id": "openrouter-pool-registration",
    "decision_id": "openrouter-pool-registration-decision-1",
    "required_capabilities": ("code", "reasoning", "vision"),
    "task_risk": "low",
    "permission_mode": "workspace-write",
    "trust_policy_digest": TRUST_POLICY_DIGEST,
    "graph_fingerprint": FIXTURE_GRAPH,
    "context_manifest_hash": CONTEXT_MANIFEST_HASH,
    "repository_commit": FIXTURE_COMMIT,
    "worktree_id": "openrouter-pool-registration",
    "allow_cross_provider": False,
    "allowed_fallback_reasons": ("capacity_unavailable",),
    "max_attempts": 2,
    "max_input_tokens": 2048,
    "max_output_tokens": 1024,
    "max_duration_ms": 120_000,
    "max_cost_microunits": None,
    "policy_version": "1.0.0",
    "issued_at": ISSUED_AT,
    "expires_at": EXPIRES_AT,
    "nonce": "openrouter-pool-registration-nonce-1",
}

_RISK = {"low": RiskTier.LOW, "medium": RiskTier.MEDIUM, "high": RiskTier.HIGH}
_PERM = {"workspace-write": PermissionMode.WORKSPACE_WRITE, "read-only": PermissionMode.READ_ONLY}
_EFFORT = {"high": Effort.HIGH}


def build_candidate(spec: dict) -> ApprovedRouteCandidate:
    return ApprovedRouteCandidate(
        candidate_id=spec["candidate_id"],
        provider=LifecycleProviderId.OPENROUTER,
        runtime_kind=RuntimeKind.REMOTE_HTTPS,
        lifecycle_identity_digest=spec["lifecycle_identity_digest"],
        capability_snapshot_digest=spec["capability_snapshot_digest"],
        model_identity_digest=spec["model_identity_digest"],
        routing_policy_digest=spec["routing_policy_digest"],
        requested_model=spec["slug"],
        effective_model=spec["slug"],
        effort=_EFFORT[spec["effort"]],
        permission_mode=_PERM[spec["permission_mode"]],
        risk_ceiling=_RISK[spec["risk_ceiling"]],
        trust_policy_digest=TRUST_POLICY_DIGEST,
        capabilities=spec["capabilities"],
        context_window_tokens=spec["context_window_tokens"],
        snapshot_expires_at=spec["snapshot_expires_at"],
    )


def build_pool(candidates: tuple[ApprovedRouteCandidate, ...]) -> ApprovedRoutePool:
    return ApprovedRoutePool(
        approval_id=POOL_SPEC["approval_id"],
        task_id=POOL_SPEC["task_id"],
        decision_id=POOL_SPEC["decision_id"],
        candidates=candidates,
        required_capabilities=POOL_SPEC["required_capabilities"],
        task_risk=_RISK[POOL_SPEC["task_risk"]],
        permission_mode=_PERM[POOL_SPEC["permission_mode"]],
        trust_policy_digest=POOL_SPEC["trust_policy_digest"],
        graph_fingerprint=POOL_SPEC["graph_fingerprint"],
        context_manifest_hash=POOL_SPEC["context_manifest_hash"],
        repository_commit=POOL_SPEC["repository_commit"],
        worktree_id=POOL_SPEC["worktree_id"],
        allow_cross_provider=POOL_SPEC["allow_cross_provider"],
        allowed_fallback_reasons=POOL_SPEC["allowed_fallback_reasons"],
        max_attempts=POOL_SPEC["max_attempts"],
        max_input_tokens=POOL_SPEC["max_input_tokens"],
        max_output_tokens=POOL_SPEC["max_output_tokens"],
        max_duration_ms=POOL_SPEC["max_duration_ms"],
        max_cost_microunits=POOL_SPEC["max_cost_microunits"],
        policy_version=POOL_SPEC["policy_version"],
        issued_at=POOL_SPEC["issued_at"],
        expires_at=POOL_SPEC["expires_at"],
        nonce=POOL_SPEC["nonce"],
    )


BUNDLE = {
    "schema_version": "1",
    "purpose": "graphite_openrouter_pool_registration",
    "issued_at": ISSUED_AT,
    "expires_at": EXPIRES_AT,
    "implementation_commit": IMPLEMENTATION_COMMIT,
    "fixture_commit": FIXTURE_COMMIT,
    "routing_store_sha256": ROUTING_STORE_SHA256,
    "lifecycle_store_sha256": LIFECYCLE_STORE_SHA256,
    "existing_store_contract": EXISTING_STORE_CONTRACT,
    "expected_final_store_contract": EXISTING_STORE_CONTRACT,  # read-only: identical
    "task_category": "isolated-code",
    "permission_mode": "workspace-write",
    "pool_now": POOL_NOW,
    "candidates": [
        {
            "position": index,
            "candidate_id": spec["candidate_id"],
            "role": spec["role"],
            "slug": spec["slug"],
            "boundary_digest": spec["boundary_digest"],
            "capability_snapshot_digest": spec["capability_snapshot_digest"],
            "lifecycle_identity_digest": spec["lifecycle_identity_digest"],
            "model_identity_digest": spec["model_identity_digest"],
            "routing_policy_digest": spec["routing_policy_digest"],
            "snapshot_expires_at": spec["snapshot_expires_at"],
        }
        for index, spec in enumerate(CANDIDATE_SPECS)
    ],
    "pool_candidate_digest": [
        build_candidate(spec).digest for spec in CANDIDATE_SPECS
    ],
    "pool_digest": build_pool(tuple(build_candidate(s) for s in CANDIDATE_SPECS)).digest,
    "trust_policy_digest": TRUST_POLICY_DIGEST,
    "context_manifest_hash": CONTEXT_MANIFEST_HASH,
    "live_inference": False,
    "network": False,
    "store_write": False,
    "calls_approval_issue": False,
    "credential_read": False,
    "forbidden_persistence": [
        "account_metadata", "credential_material", "diff_content",
        "executable_or_credential_paths", "prompt_body", "provider_diagnostics",
        "repository_source", "response_body", "stderr_body", "stdout_body",
    ],
    "raw_provider_output_persistence": False,
    "merge": False,
    "push": False,
    "deploy": False,
}
BUNDLE_DIGEST = digest(BUNDLE)


if __name__ == "__main__":
    print(json.dumps({"bundle": BUNDLE, "bundle_digest": BUNDLE_DIGEST}, sort_keys=True))
```

- [ ] **Step 2: Run the prepare script to confirm it computes**

Run:
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src" python "F:/tmp/graphite-live-acceptance-harness/_prepare_openrouter_pool_registration.py"
```
Expected: one JSON line containing `"bundle_digest": "<64-hex>"`, a two-element `pool_candidate_digest`, a `pool_digest`, and `"store_write": false`, `"calls_approval_issue": false`. No exception (proves every pinned candidate/pool value constructs — `ApprovedRouteCandidate.__post_init__` / `ApprovedRoutePool.__post_init__` accept them).

- [ ] **Step 3: Write the offline constructibility dry-run**

Create scratchpad `dryrun_pool_construct.py`:
```python
from __future__ import annotations

import _prepare_openrouter_pool_registration as prep

candidates = tuple(prep.build_candidate(s) for s in prep.CANDIDATE_SPECS)
pool = prep.build_pool(candidates)

assert len(candidates) == 2
assert [c.candidate_id for c in candidates] == [
    "openrouter-kimi27-code-primary", "openrouter-kimi26-fallback"
]
assert pool.candidates[0].requested_model == "moonshotai/kimi-k2.7-code"
assert pool.candidates[1].requested_model == "moonshotai/kimi-k2.6"
assert pool.max_attempts == 2
assert pool.allowed_fallback_reasons == ("capacity_unavailable",)
# required_capabilities must be a subset of BOTH candidates' capabilities
for c in candidates:
    assert set(pool.required_capabilities).issubset(c.capabilities), c.candidate_id
# trust policy is shared across candidates and pool
assert len({c.trust_policy_digest for c in candidates} | {pool.trust_policy_digest}) == 1
# each candidate snapshot must outlive the pool
for c in candidates:
    assert c.snapshot_expires_at >= pool.expires_at
assert len({c.digest for c in candidates}) == 2  # distinct candidate digests
print("DRYRUN_OK: pool + 2 candidates construct;",
      "pool_digest", pool.digest[:12], "cand", [c.digest[:8] for c in candidates])
```

- [ ] **Step 4: Run the constructibility dry-run**

Run:
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src;F:/tmp/graphite-live-acceptance-harness" python "C:/Users/fbmac/AppData/Local/Temp/claude/F--Projects-graphite/4d7429e7-ce78-4970-93c8-fdcf8e95f1e8/scratchpad/dryrun_pool_construct.py"
```
(On Windows `python`, use `;` as the `PYTHONPATH` separator, matching the harness `run_validation` convention.)
Expected: `DRYRUN_OK: pool + 2 candidates construct; ...`. If any assertion fails, the pinned facts are wrong — re-read the live values (see `scratchpad/pool_reg_values.py`) before proceeding.

- [ ] **Step 5: Commit**

```bash
git -C "F:/tmp/graphite-claude-codex-router" add -A  # NOTE: harness scripts live outside the repo; see below
```
The harness scripts live in `F:\tmp\graphite-live-acceptance-harness` (not tracked in the graphite repo). Commit only repo-tracked artifacts. For Task 1 there is **nothing to commit in the graphite repo yet** — the committed artifact for this feature is this plan (already staged separately) and the evidence doc (Task 4). Record completion of Task 1 by confirming both offline runs pass; the harness scripts are working files under `F:\tmp\graphite-live-acceptance-harness`. Do not attempt to `git add` the harness dir.

---

### Task 2: Execute proof script (`_execute_openrouter_pool_registration.py`)

**Files:**
- Create: `F:\tmp\graphite-live-acceptance-harness\_execute_openrouter_pool_registration.py`
- Test (scratchpad, offline): `C:\Users\fbmac\AppData\Local\Temp\claude\F--Projects-graphite\4d7429e7-ce78-4970-93c8-fdcf8e95f1e8\scratchpad\dryrun_pool_registration.py`

**Interfaces:**
- Consumes: `_prepare_openrouter_pool_registration` (all Task 1 names), `graphite.routing.storage.RepositoryStore`, `graphite.routing.lifecycle_storage.LifecycleStore`, `graphite.routing.lifecycle_service.ProviderLifecycleService`, `graphite.routing.route_pool.{RouteAttemptEvidence, SideEffectState, select_route, RoutePoolError}`, `graphite.routing.lifecycle.ProviderLifecycleState`, `graphite.routing.profiles.load_verified_capability_snapshots`.
- Produces: a single sanitized JSON receipt on stdout; exit 0 on pass, 1 on any fail-closed category. Overridable module globals for the dry-run: `FIXTURE`, `ROUTING_PATH`, `LIFECYCLE_PATH`.

- [ ] **Step 1: Write the execute script**

Create `_execute_openrouter_pool_registration.py`:

```python
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import _prepare_openrouter_pool_registration as prepared
from graphite.routing.lifecycle import ProviderLifecycleState
from graphite.routing.lifecycle_service import LifecycleServiceError, ProviderLifecycleService
from graphite.routing.lifecycle_storage import LifecycleStore
from graphite.routing.profiles import load_verified_capability_snapshots
from graphite.routing.route_pool import (
    RouteAttemptEvidence,
    RoutePoolError,
    SideEffectState,
    select_route,
)
from graphite.routing.storage import RepositoryStore

FIXTURE = prepared.FIXTURE
ROUTING_PATH = prepared.ROUTING_PATH
LIFECYCLE_PATH = prepared.LIFECYCLE_PATH


class HarnessFailure(RuntimeError):
    def __init__(self, code: str, **evidence: object) -> None:
        self.code = code
        self.evidence = evidence
        super().__init__(code)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def store_audit() -> dict[str, object]:
    connection = sqlite3.connect(ROUTING_PATH)
    try:
        return {
            "schema_version": connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0],
            "capability_snapshots": connection.execute(
                "SELECT COUNT(*) FROM capability_snapshots"
            ).fetchone()[0],
            "lifecycle_snapshot_bindings": connection.execute(
                "SELECT COUNT(*) FROM lifecycle_snapshot_bindings"
            ).fetchone()[0],
            "telemetry_events": connection.execute(
                "SELECT COUNT(*) FROM cli_telemetry_events"
            ).fetchone()[0],
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
            "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--approved", required=True)
    arguments = parser.parse_args()
    action = "local_preflight"
    try:
        # --- Preflight: approval, expiry, digest, store pins, before-audit ---
        if arguments.approved != prepared.BUNDLE_DIGEST:
            raise HarnessFailure("manifest_approval_mismatch")
        if int(time.time()) >= prepared.EXPIRES_AT:
            raise HarnessFailure("manifest_expired")
        if prepared.digest(prepared.BUNDLE) != prepared.BUNDLE_DIGEST:
            raise HarnessFailure("manifest_digest_mismatch")
        before_routing = file_sha256(ROUTING_PATH)
        before_lifecycle = file_sha256(LIFECYCLE_PATH)
        if (
            before_routing != prepared.ROUTING_STORE_SHA256
            or before_lifecycle != prepared.LIFECYCLE_STORE_SHA256
        ):
            raise HarnessFailure("source_store_changed")
        before_audit = store_audit()
        if before_audit != prepared.EXISTING_STORE_CONTRACT:
            raise HarnessFailure("source_store_audit_failed", **before_audit)

        # --- Read-only resolution: load edit snapshots + active identities ---
        action = "resolution"
        routing_store = RepositoryStore(FIXTURE)          # do NOT call initialize()
        lifecycle_store = LifecycleStore(FIXTURE)         # do NOT call initialize()
        service = ProviderLifecycleService(lifecycle_store, routing_store)
        # now=0 loads ALL snapshots regardless of expiry; the expiry gate is enforced
        # explicitly below so an expired snapshot yields a precise category.
        by_digest = {s.digest: s for s in load_verified_capability_snapshots(routing_store, now=0)}
        pool_now = prepared.POOL_NOW

        candidates = []
        authorities = []
        candidate_report = []
        for spec in prepared.CANDIDATE_SPECS:
            snapshot = by_digest.get(spec["capability_snapshot_digest"])
            if snapshot is None:
                raise HarnessFailure("pool_candidate_unbound", candidate_id=spec["candidate_id"])
            if snapshot.expires_at <= pool_now:
                raise HarnessFailure(
                    "pool_candidate_source_expired", candidate_id=spec["candidate_id"]
                )
            observation = lifecycle_store.current_observation(spec["boundary_digest"])
            if observation is None or observation.identity is None:
                raise HarnessFailure("pool_candidate_unbound", candidate_id=spec["candidate_id"])
            if observation.state is not ProviderLifecycleState.ACTIVE:
                raise HarnessFailure("pool_candidate_inactive", candidate_id=spec["candidate_id"])
            identity = observation.identity
            binding = routing_store.lifecycle_identity_binding(
                authority_kind="capability_snapshot",
                authority_id=snapshot.digest,
            )
            if binding != identity.digest:
                raise HarnessFailure("pool_candidate_unbound", candidate_id=spec["candidate_id"])

            # Assert live values equal the pinned expectations (fail closed on drift).
            drift = {}
            if identity.digest != spec["lifecycle_identity_digest"]:
                drift["lifecycle_identity_digest"] = identity.digest
            if identity.model_identity_digest != spec["model_identity_digest"]:
                drift["model_identity_digest"] = identity.model_identity_digest
            if identity.routing_policy_digest != spec["routing_policy_digest"]:
                drift["routing_policy_digest"] = identity.routing_policy_digest
            if snapshot.profile.effective_model != spec["slug"]:
                drift["effective_model"] = snapshot.profile.effective_model
            if snapshot.profile.context_window_tokens != spec["context_window_tokens"]:
                drift["context_window_tokens"] = snapshot.profile.context_window_tokens
            if snapshot.expires_at != spec["snapshot_expires_at"]:
                drift["snapshot_expires_at"] = snapshot.expires_at
            if set(spec["capabilities"]) - set(snapshot.profile.capabilities):
                drift["capabilities"] = list(snapshot.profile.capabilities)
            if drift:
                raise HarnessFailure("pool_candidate_drift", candidate_id=spec["candidate_id"])

            try:
                candidate = prepared.build_candidate(spec)
            except RoutePoolError as error:
                raise HarnessFailure("pool_candidate_invalid", code=error.code) from None
            try:
                authority = service.route_authority(spec["boundary_digest"], candidate)
            except LifecycleServiceError as error:
                raise HarnessFailure("pool_authority_unavailable", code=error.code) from None
            candidates.append(candidate)
            authorities.append(authority)
            candidate_report.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "slug": spec["slug"],
                    "role": spec["role"],
                    "capability_snapshot_digest": candidate.capability_snapshot_digest,
                    "lifecycle_identity_digest": candidate.lifecycle_identity_digest,
                    "candidate_digest": candidate.digest,
                    "authority_state": authority.state.value,
                    "loadable": True,
                }
            )

        # --- Construct the pool (this IS the approvability proof; no issue()) ---
        action = "pool_construction"
        try:
            pool = prepared.build_pool(tuple(candidates))
        except RoutePoolError as error:
            raise HarnessFailure("pool_invalid", code=error.code) from None
        authorities = tuple(authorities)

        # --- Selectability: primary with no attempts ---
        action = "select_primary"
        try:
            primary = select_route(pool, authorities, (), now=pool_now)
        except RoutePoolError as error:
            raise HarnessFailure("route_selection_failed", stage="primary", code=error.code) from None
        if primary.candidate.candidate_id != pool.candidates[0].candidate_id:
            raise HarnessFailure("route_selection_wrong", stage="primary")

        # --- Selectability: fallback after one synthesized capacity failure ---
        action = "select_fallback"
        capacity_failure = RouteAttemptEvidence(
            candidate_id=pool.candidates[0].candidate_id,
            candidate_digest=pool.candidates[0].digest,
            attempt_ordinal=1,
            failure_category="capacity_unavailable",
            accepted_output=False,
            side_effect_state=SideEffectState.NONE,
            input_tokens=0,
            output_tokens=0,
            duration_ms=0,
            cost_microunits=None,
        )
        try:
            fallback = select_route(pool, authorities, (capacity_failure,), now=pool_now)
        except RoutePoolError as error:
            raise HarnessFailure("route_selection_failed", stage="fallback", code=error.code) from None
        if fallback.candidate.candidate_id != pool.candidates[1].candidate_id:
            raise HarnessFailure("route_selection_wrong", stage="fallback")

        for entry in candidate_report:
            entry["selectable"] = True

        # --- Final byte-unchanged audit ---
        action = "final_audit"
        after_routing = file_sha256(ROUTING_PATH)
        after_lifecycle = file_sha256(LIFECYCLE_PATH)
        after_audit = store_audit()
        mutated = (
            after_routing != before_routing
            or after_lifecycle != before_lifecycle
            or after_audit != before_audit
        )
        if mutated:
            raise HarnessFailure("pool_registration_mutated_store")

        print(
            json.dumps(
                {
                    "status": "passed",
                    "bundle_digest": prepared.BUNDLE_DIGEST,
                    "purpose": prepared.BUNDLE["purpose"],
                    "task_category": "isolated-code",
                    "permission_mode": "workspace-write",
                    "pool_digest": pool.digest,
                    "candidates": candidate_report,
                    "selection": {
                        "primary": primary.candidate.candidate_id,
                        "fallback_after_capacity_unavailable": fallback.candidate.candidate_id,
                    },
                    "diagnostic": "edit_snapshots_directly_selectable",
                    "mutated": False,
                    "audit": after_audit,
                    "calls_approval_issue": False,
                    "live_inference": False,
                    "merge": False,
                    "push": False,
                    "deploy": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except HarnessFailure as failure:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "action": action,
                    "failure_category": failure.code,
                    "bundle_digest": prepared.BUNDLE_DIGEST,
                    **failure.evidence,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "action": action,
                    "failure_category": "harness_failed",
                    "bundle_digest": prepared.BUNDLE_DIGEST,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Write the offline end-to-end dry-run (against a store copy)**

This dry-run proves the full control flow **without** the live store and without operator approval, by pointing the execute module's paths at a byte copy. It must pass before Task 3.

Note a deliberate method difference from the real execute: this dry-run runs `PRAGMA wal_checkpoint(TRUNCATE)` before its byte-comparison, whereas `_execute_openrouter_pool_registration.py` hashes `after_routing`/`after_lifecycle` with no checkpoint. Both correctly land on byte-unchanged because read-only WAL access appends zero frames to the main file (already proven by the standalone `pool_reg_hash_drycheck.py`: `routing_unchanged: true`, no sidecars). The extra checkpoint here only makes the copy comparison robust to leaked connections in-process; it does not paper over a mutation.

Create scratchpad `dryrun_pool_registration.py`:
```python
from __future__ import annotations

import gc
import hashlib
import shutil
from pathlib import Path

import _execute_openrouter_pool_registration as ex
import _prepare_openrouter_pool_registration as prep

LIVE = prep.FIXTURE
WORKDIR = Path(r"F:\tmp\graphite-pool-reg-dryrun")
ROUTING_REL = Path(".graphite") / "routing" / "events.sqlite3"
LIFECYCLE_REL = Path(".graphite") / "routing" / "provider-lifecycle.sqlite3"


def file_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


shutil.rmtree(WORKDIR, ignore_errors=True)
(WORKDIR / ROUTING_REL).parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(LIVE / ROUTING_REL, WORKDIR / ROUTING_REL)
shutil.copy2(LIVE / LIFECYCLE_REL, WORKDIR / LIFECYCLE_REL)

# Point the execute module at the COPY and pin the copy's hashes so preflight passes.
ex.FIXTURE = WORKDIR
ex.ROUTING_PATH = WORKDIR / ROUTING_REL
ex.LIFECYCLE_PATH = WORKDIR / LIFECYCLE_REL
prep.ROUTING_STORE_SHA256 = file_sha(WORKDIR / ROUTING_REL)
prep.LIFECYCLE_STORE_SHA256 = file_sha(WORKDIR / LIFECYCLE_REL)
prep.BUNDLE["routing_store_sha256"] = prep.ROUTING_STORE_SHA256
prep.BUNDLE["lifecycle_store_sha256"] = prep.LIFECYCLE_STORE_SHA256
prep.BUNDLE_DIGEST = prep.digest(prep.BUNDLE)

before = (file_sha(WORKDIR / ROUTING_REL), file_sha(WORKDIR / LIFECYCLE_REL))
import sys as _sys
_argv = _sys.argv
_sys.argv = ["dryrun", "--approved", prep.BUNDLE_DIGEST]
try:
    code = ex.main()
finally:
    _sys.argv = _argv
gc.collect()
# Checkpoint WAL like a clean exit, then confirm byte-unchanged.
import sqlite3
for path in (WORKDIR / ROUTING_REL, WORKDIR / LIFECYCLE_REL):
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
after = (file_sha(WORKDIR / ROUTING_REL), file_sha(WORKDIR / LIFECYCLE_REL))
assert code == 0, f"execute returned {code}"
assert before == after, "store copy mutated by the read-only proof"
shutil.rmtree(WORKDIR, ignore_errors=True)
print("DRYRUN_OK: execute main() returned 0; copy byte-unchanged")
```

- [ ] **Step 3: Run the end-to-end dry-run**

Run:
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src;F:/tmp/graphite-live-acceptance-harness" python "C:/Users/fbmac/AppData/Local/Temp/claude/F--Projects-graphite/4d7429e7-ce78-4970-93c8-fdcf8e95f1e8/scratchpad/dryrun_pool_registration.py"
```
Expected: the execute script prints its `"status":"passed"` receipt with `selection.primary == "openrouter-kimi27-code-primary"` and `selection.fallback_after_capacity_unavailable == "openrouter-kimi26-fallback"`, `"diagnostic":"edit_snapshots_directly_selectable"`, `"mutated":false`, then `DRYRUN_OK: execute main() returned 0; copy byte-unchanged`.
If instead the receipt shows `route_selection_failed` with `code` `route_snapshot_changed`/`route_inactive`, the diagnostic is NEGATIVE — a clean, zero-cost finding that a governed re-activation to the edit snapshot is a prerequisite (a separate step, out of scope here). Record it and stop; do not weaken any check to force a pass.

- [ ] **Step 4: Confirm the graphite test suite is unaffected**

No `graphite` source changed, so the suite must be untouched. Sanity-run the route-pool and profile tests:
```bash
python -B -m pytest -q -p no:cacheprovider --basetemp "F:/tmp/graphite-pool-reg-suite" tests/test_route_pool.py tests/test_routing_profiles.py
```
Run from `F:\tmp\graphite-claude-codex-router`. Expected: all pass. (The custom `--basetemp` dodges the Wondershare `CreatorTemp` permission issue.)

- [ ] **Step 5: Commit (evidence-doc only — see Task 4)**

As in Task 1, the harness scripts are working files outside the graphite repo and are not committed. There is nothing to commit for Task 2 in the repo; completion is proven by Steps 3–4 passing.

---

### Task 3: Governed run against the live stores (operator-executed)

**Files:** none created. Runs `_execute_openrouter_pool_registration.py` against the live fixture stores.

- [ ] **Step 1: Re-pin the manifest to current live state**

Immediately before requesting approval, re-verify and, if needed, update three pinned values in `_prepare_openrouter_pool_registration.py`:
```bash
git -C "F:/tmp/graphite-claude-codex-router" rev-parse HEAD          # -> IMPLEMENTATION_COMMIT
git -C "F:/tmp/graphite-claude-codex-router" status --porcelain=v1   # must be empty (clean)
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src" python -c "import hashlib,pathlib; r=pathlib.Path(r'F:\tmp\graphite-production-live-fixture\.graphite\routing'); print('routing', hashlib.sha256((r/'events.sqlite3').read_bytes()).hexdigest()); print('lifecycle', hashlib.sha256((r/'provider-lifecycle.sqlite3').read_bytes()).hexdigest())"
```
Set `IMPLEMENTATION_COMMIT` to the current clean feature HEAD, and `ROUTING_STORE_SHA256`/`LIFECYCLE_STORE_SHA256` to the printed hashes (they should already match `21e73d3f…`/`a99f7cc4…` if nothing wrote to the store since 2026-07-20). Confirm `now < 1784648044` (runway remains). Re-run Task 1 Step 2 and Task 2 Step 3 after any edit so the `BUNDLE_DIGEST` and dry-run stay green.

**Mandatory if anything wrote to the live store since the 2026-07-20 read** (any other rN run, promotion, or activation): the store hashes and/or the ACTIVE identity digests will have moved. Re-pinning the hashes is required, and the execute script's per-candidate `pool_candidate_drift` check will fail closed if a live identity/model/routing digest no longer matches the pinned `CANDIDATE_SPECS`. In that case, re-read the live values (`scratchpad/pool_reg_values.py`), update `CANDIDATE_SPECS`, and re-run both dry-runs before requesting approval — this is not optional. The drift firing is correct fail-closed behavior, not a harness bug.

- [ ] **Step 2: Display the composition manifest and request approval**

Print the bundle and show the operator the exact composition:
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src" python "F:/tmp/graphite-live-acceptance-harness/_prepare_openrouter_pool_registration.py"
```
Present to the operator: purpose `graphite_openrouter_pool_registration`; ordered candidates `[openrouter-kimi27-code-primary (kimi-k2.7-code, snapshot 500cda19…), openrouter-kimi26-fallback (kimi-k2.6, snapshot 1b2a7c8e…)]`; category ISOLATED_CODE / WORKSPACE_WRITE; `store_write:false`, `calls_approval_issue:false`, `live_inference:false`; and the `BUNDLE_DIGEST`. State plainly: **this run is offline and read-only; approval authorizes the registered composition, not any spend.** Then wait for the operator message `Approved: graphite_openrouter_pool_registration bundle <BUNDLE_DIGEST>`.

- [ ] **Step 3: Operator runs the proof in-session**

The operator runs (via the `!` bash prefix, because the classifier blocks the agent's own shell from the governed execute path):
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src;F:/tmp/graphite-live-acceptance-harness" python "F:/tmp/graphite-live-acceptance-harness/_execute_openrouter_pool_registration.py" --approved <BUNDLE_DIGEST>
```
Expected receipt: `status:passed`, `selection.primary = openrouter-kimi27-code-primary`, `selection.fallback_after_capacity_unavailable = openrouter-kimi26-fallback`, `diagnostic: edit_snapshots_directly_selectable`, `mutated: false`, `audit` = 12/12/21 integrity ok. Capture the exact JSON line.

- [ ] **Step 4: Post-run store confirmation**

Re-hash both live stores and confirm byte-identical to Step 1:
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src" python -c "import hashlib,pathlib; r=pathlib.Path(r'F:\tmp\graphite-production-live-fixture\.graphite\routing'); print(hashlib.sha256((r/'events.sqlite3').read_bytes()).hexdigest()); print(hashlib.sha256((r/'provider-lifecycle.sqlite3').read_bytes()).hexdigest())"
```
Expected: `21e73d3f…` and `a99f7cc4…`, unchanged. If a transient `-wal`/`-shm` sidecar exists, note it is ephemeral SQLite scratch; the pinned main files are the authority and must be unchanged.

---

### Task 4: Record evidence and commit

**Files:**
- Modify: `F:\tmp\graphite-claude-codex-router\docs\superpowers\implementation-notes\2026-07-19-provider-lifecycle-evidence.md`

- [ ] **Step 1: Append the pool-registration evidence section**

Append a new dated section titled `## OpenRouter ISOLATED_CODE edit-pool registration (offline loadability + selectability proof)` containing: the sanitized receipt JSON from Task 3 Step 3; the pre/post store hashes from Task 3 Steps 1 and 4 (byte-unchanged); the positive diagnostic (`edit_snapshots_directly_selectable`) with the one-line rationale (edit snapshots are bound to the current ACTIVE identity, which the edit path left untouched); the explicit statement that the proof called neither `execute_openrouter` nor `ApprovalAuthority.issue`, made no network call, and mutated no store; and the resolution note that spec step 3's `issue()` was intentionally dropped because it writes to the store. No prompts, file contents, credentials, or raw provider output.

- [ ] **Step 2: Commit the evidence**

```bash
git -C "F:/tmp/graphite-claude-codex-router" add docs/superpowers/implementation-notes/2026-07-19-provider-lifecycle-evidence.md
git -C "F:/tmp/graphite-claude-codex-router" commit -m "docs: record OpenRouter edit-pool registration loadability+selectability proof"
```
(End the commit message with the required `Co-Authored-By` trailer per repo convention.) Confirm the feature worktree is clean afterward. Do not push, merge, or touch `F:\Projects\graphite`.

---

## Testing / Verification Summary

- **Task 1:** prepare script runs and prints a `BUNDLE_DIGEST`; `dryrun_pool_construct.py` proves both candidates and the pool construct from pinned values (constructibility = approvability).
- **Task 2:** `dryrun_pool_registration.py` proves, against a store **copy**, that `select_route` picks primary then fallback and the copy is byte-unchanged; `test_route_pool.py` + `test_routing_profiles.py` remain green (no source change).
- **Task 3:** the governed, operator-approved run against the **live** stores yields the same selection outcome and `mutated:false`; live store hashes unchanged before/after.
- **Task 4:** sanitized evidence committed.
- If Task 2/3 surfaces a genuine loader gap (e.g. a `route_*` fail-closed code), that is a clean finding: stop, record it, and — only if a minimal `graphite` fix is warranted — give that fix its own failing test under `tests/` before implementing (TDD), per the spec.

## Success Criteria

- Both edit-promoted OpenRouter models load as eligible snapshots and construct as valid `ApprovedRouteCandidate`s for ISOLATED_CODE, in the order `[kimi-k2.7-code primary, kimi-k2.6 capacity fallback]`.
- `select_route` selects `candidate[0]` (kimi-k2.7-code) with `attempts=()` and `candidate[1]` (kimi-k2.6) after one synthesized `capacity_unavailable` attempt, each against its live ACTIVE `RouteAuthority`, all digests matching.
- The live stores are byte-unchanged after the run (`mutated: false`; hashes `21e73d3f…` / `a99f7cc4…`).
- No `graphite` source change; no `ApprovalAuthority.issue`; no network; no live inference.
- Evidence recorded and committed on `feat/claude-codex-router`.

## Out of Scope (deferred)

- The read-only review / authorization pool (OpenRouter reviewing CLI-authored diffs).
- A live routed smoke through `execute_approved_route_pool` (executing a real routed OpenRouter action through the signed pool — the path that would legitimately call `ApprovalAuthority.issue`/`.consume`).
- The three unverified models (`kimi-k3`, `glm-5.2`, `muse-spark-1.1`).
- Any lifecycle re-activation (only performed if the proof shows the NEGATIVE diagnostic, and then as a separate governed step).
- Branch integration (push/merge of `feat/claude-codex-router`).
