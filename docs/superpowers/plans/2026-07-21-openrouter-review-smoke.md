# OpenRouter Live Routed Review Smoke Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route one real `kimi-k2.7-code` **review** through `execute_approved_route_pool` against a signed, consumed two-candidate **READ_ONLY / AUTHORIZATION** pool built from the verification capability snapshots — proving the routed review role the edit track never exercised — where the model reviews a known-correct, pytest-validated tenant-authorization diff embedded in the prompt and returns `{verdict:"pass", findings:[]}`, and persist one routed AUTHORIZATION telemetry row.

**Architecture:** A governed prepare/execute harness pair in `F:\tmp\graphite-live-acceptance-harness`, a READ_ONLY sibling of the routed edit smoke — **live-inference, mutating, budget-spending**. `_prepare_openrouter_review_smoke.py` pins two READ_ONLY verification-snapshot candidates, a READ_ONLY AUTHORIZATION pool envelope, a review `output_schema` (`{verdict, findings}`), and a review prompt embedding a **frozen** golden diff (captured once into a sidecar file, sha-pinned), then computes `BUNDLE_DIGEST`. `_execute_openrouter_review_smoke.py --approved <digest>` reconstructs the candidates/pool from live records, issues a signed approval, and drives `execute_approved_route_pool` with a live **stateless review runner** (one `execute_openrouter` json_object call — no worktree, no apply, no diff, no validation subprocess), an authority loader, and a telemetry-persisting evidence sink. No `graphite` source change is expected.

**Tech Stack:** Python 3.14; `graphite.routing` (`route_pool_execution`, `route_pool`, `approval`, `lifecycle_service`, `lifecycle_storage`, `storage`, `openrouter_executor`, `probe_runner`, `profiles`, `contracts`, `lifecycle`, `telemetry`); SQLite (WAL); OpenRouter `chat/completions` (`json_object`).

## Global Constraints

- **Live inference, one call.** Exactly one bounded `execute_openrouter` review inference against `moonshotai/kimi-k2.7-code` (json_object). The API key is read only from `OPENROUTER_API_KEY` in the session environment at execute time; it never appears in argv, bundle, or receipt.
- **Run the live execute with NO inline `OPENROUTER_API_KEY`.** An inline `OPENROUTER_API_KEY=...` prefix shadows the operator's real ambient key and returns `auth_required` at preflight; because `execute_approved_route_pool` consumes the approval **before** the runner, that burns the single-use approval and forces a fresh governed round. Present the bare command; the key is read from the ambient session env (this is how the r7/r8 and r3-routed smokes succeeded).
- **This round mutates the live store and spends budget** (inherent to a routed smoke). Expected writes: an `approval_records` row (issued → **consumed**), a machine-quota reservation (a separate quota sqlite in a harness state dir), and one routed AUTHORIZATION `cli_telemetry_events` row (**22→23**). Must NOT change capability_snapshots (12) or lifecycle_snapshot_bindings (12) — no promotion, no lifecycle transition. Post-run store hashes/counts become the new pinned baseline.
- **Review oracle is the success gate:** the parsed response must be a dict with exactly keys `{verdict, findings}`, `verdict == "pass"`, and `findings == []`. Any deviation fails closed (`review_response_invalid` / `review_not_accepted`) recording only the verdict string and finding **count** — never finding text, diff content, prompt, or raw provider output. A non-`pass` verdict is never rewritten to force acceptance.
- **Cost ceiling `64000` microunits** (worst-case ≈ 22.5k for 4096 output + 8192 input at kimi-k2.7 pricing; ~2.8× headroom), enforced by both the pool (`max_cost_microunits`) and the runner's `execute_openrouter(max_cost_microunits=…)`.
- **Expiry runway (binding).** The run selects the `kimi-k2.7-code` **verification** snapshot `99db4a4a…`, which expires at epoch **`1784623000`** (≈ 7.7 h from the 2026-07-21 read — sooner than the edit snapshots). The smoke must complete before then; otherwise re-verification is a prerequisite (a separate governed step). The execute preflight fails closed on `time.time() >= EXPIRES_AT`.
- **Diff-under-review substitution (disclosed).** The approved spec names the r7/r8 tenant-authorization diff, but the r7/r8 diff text was never persisted (disposable worktrees). This plan instead embeds the **`expected-codex` golden worktree diff** — the same tenant-authorization `can_read_record` change and denial tests, CLI-agent-authored and pytest-validated, and the exact diff the CLI-review precedent (`_execute_live_batch`) already had a live review model return `pass`/`[]` on. Substance matches the spec's oracle criteria; the provenance is codex-golden rather than r7/r8. Surface this when presenting the plan for approval.
- **Full manifest/approval gate.** Displayed bundle → operator `Approved: graphite_openrouter_review_smoke bundle <digest>` → operator runs the execute via the `!` prefix (the classifier blocks the agent's own shell from the live execute). Offline dry-runs (Tasks 1–2) are agent-runnable.
- **Never touch `F:\Projects\graphite` (main).** No merge, no push, no deploy. Work only on `feat/claude-codex-router` in `F:\tmp\graphite-claude-codex-router` and the harness dir. Quarantined stores are never reactivated.
- **Sanitized receipts only:** digests, counts, tokens, cost, durations, outcome categories, verdict string, finding count, booleans. Never prompts, diffs, finding text, credentials, or raw provider output. `raw_provider_output_persistence: false`.

## Component / File Structure

- Create: `F:\tmp\graphite-live-acceptance-harness\openrouter-review-diff.txt` — the frozen golden diff bytes (captured once; sha-pinned).
- Create: `F:\tmp\graphite-live-acceptance-harness\_prepare_openrouter_review_smoke.py` — READ_ONLY verification-snapshot pool + review schema/prompt + frozen diff + bundle.
- Create: `F:\tmp\graphite-live-acceptance-harness\_execute_openrouter_review_smoke.py` — issue + routed review + telemetry + receipt.
- Create (scratchpad, not committed): `dryrun_review_smoke_construct.py` — offline pool/candidate constructibility.
- Create (scratchpad, not committed): `dryrun_review_smoke.py` — offline end-to-end against store **copies**, exercising the **real** `perform_review` with a **fake transport**, both the pass and reject paths.
- Modify: `F:\tmp\graphite-claude-codex-router\docs\superpowers\implementation-notes\2026-07-19-provider-lifecycle-evidence.md` — append review-smoke evidence.

### Pinned facts (verified live 2026-07-21, read-only)

- Store: 12 capability_snapshots / 12 lifecycle_snapshot_bindings / **22** cli_telemetry_events; integrity ok; 0 FK; schema_version 6; `approval_status("openrouter-review-smoke-1")` is `None`. Hashes: routing `aef018b0b6ea7243330aa48995e43e9babb6e70b0cadb5347f2154d59daab815`, lifecycle `a99f7cc454e4b8ad000c1d0ba6b203b44cf6d12c3fef1b9c57d428b236bef890`.
- `FIXTURE = F:\tmp\graphite-production-live-fixture`; `FIXTURE_COMMIT = 05f01737326469bd951ec677a0cf73b68caf9fe1`; `FIXTURE_GRAPH = 6f1633953ecd0b7cd7c4009595f6da57866b6eea1aeca44e65c66e98b318ac21`.
- **Review (verification) candidate specs** — both READ_ONLY / HIGH / ACTIVE-bound, identity/model/routing digests identical to the edit candidates (identity is per-model), differing only in `capability_snapshot_digest`, `permission_mode`, and `snapshot_expires_at`:
  - primary `kimi-k2.7-code`: snapshot `99db4a4a53aac5a97aae36d1d2ace5de3b194c63a8605c59dbd4837446a205f5`; identity `c8cece35646deec30fa9538ba998722781074027a6bdd2dabafd1986359439ab`; model `939d3a5af17f6d5ec5ccfa05ac09134873d4258357865f59829f96b94a392836`; routing `916df225733c25f6d9976d29edb6b7a2050f61457fa978e178544bcd53615b39`; boundary `e28276006bea9e48641c28517b075843e9239150de2a8b9fa5169b03e5f083e5`; caps `("code","reasoning","thinking","vision")`; `context_window 262144`; `snapshot_expires_at 1784623000`.
  - fallback `kimi-k2.6`: snapshot `6816edafc45f164fa0dfec5009121afc6862c26c72c5963798177358f463b26d`; identity `6333c4d577f8fcb111f57f23c4c2f6b3ab889cc3ba19edfb78cc082a63f6bade`; model `e504400884ef6c43f66fc983060d8da6d3ea81fe189e491f09a25775e4b0b10b`; routing `916df225…`; boundary `1df7e44485e53f9bdf4e7ab08fa1d17fc1624b2a7d03f536dabefa49affa80be`; caps `("code","reasoning","tools","vision")`; `context_window 262144`; `snapshot_expires_at 1784628600`.
- Frozen golden diff `openrouter-review-diff.txt`: **999 bytes**, sha256 `7a6fc7ed5a1da4a912fefc58d52b9ae93972eaf65e630fe83436eb4f07f23ee2` (LF line endings). Generated once from `F:\tmp\graphite-production-live-worktrees\expected-codex` via `git diff FIXTURE_COMMIT`. Content: `can_read_record` returns `bool(actor_tenant) and actor_tenant == record_tenant` (was `True`), plus appended `test_cross_tenant_cannot_read_record` and `test_empty_tenant_cannot_read_record`.
- Pool envelope: `permission_mode READ_ONLY`; `task_risk HIGH`; `required_capabilities ("code","reasoning","vision")` (subset of both candidates' caps); `max_cost_microunits 64000`; `max_input_tokens 8192`; `max_output_tokens 4096` (≤ 32768 pool limit; sum 12288 ≤ 262144 context); `max_duration_ms 180000`; `allowed_fallback_reasons ("capacity_unavailable",)`; `max_attempts 2`; `allow_cross_provider False`; `graph_fingerprint 6f163395…`; `repository_commit FIXTURE_COMMIT`; `expires_at 1784623000` (= min verification snapshot expiry); `issued_at 1784578461`; fresh ids `approval_id "openrouter-review-smoke-1"`, `task_id "openrouter-review-smoke"`, `decision_id "openrouter-review-smoke-decision-1"`, `worktree_id "openrouter-review-smoke"`, `nonce "openrouter-review-smoke-nonce-1"`; `policy_version "1.0.0"`; review-specific `trust_policy_digest` / `context_manifest_hash`.
- Quota: `REPOSITORY_QUOTA_TOKENS = MACHINE_QUOTA_TOKENS = 262144` (≥ reserved 12288).

### Reference: exact API shapes this plan consumes

```python
# graphite.routing.route_pool_execution
execute_approved_route_pool(*, pool, signed_approval, approval_authority,
    authority_loader, runner, evidence_sink, repository_quota_tokens,
    machine_quota_tokens, now) -> RouteExecutionResult
RouteExecutionResult(candidate_id, candidate_digest, attempt_ordinal, output,
    input_tokens, output_tokens, duration_ms, cost_microunits)  # output non-empty, <=1MB
# RouteExecutionEvidence(candidate_id, candidate_digest, attempt_ordinal,
#     outcome_category, input_tokens, output_tokens, duration_ms, cost_microunits) -> evidence_sink

# graphite.routing.route_pool
ApprovedRouteCandidate(candidate_id, provider, runtime_kind, lifecycle_identity_digest,
    capability_snapshot_digest, model_identity_digest, routing_policy_digest, requested_model,
    effective_model, effort, permission_mode, risk_ceiling, trust_policy_digest, capabilities,
    context_window_tokens, snapshot_expires_at)  # .digest
ApprovedRoutePool(approval_id, task_id, decision_id, candidates, required_capabilities, task_risk,
    permission_mode, trust_policy_digest, graph_fingerprint, context_manifest_hash, repository_commit,
    worktree_id, allow_cross_provider, allowed_fallback_reasons, max_attempts, max_input_tokens,
    max_output_tokens, max_duration_ms, max_cost_microunits, policy_version, issued_at, expires_at,
    nonce)  # .digest, .candidates, .approval_id ; raises RoutePoolError

# graphite.routing.approval.ApprovalAuthority(store, *, key_path, quota_path, now)
#   .issue(pool) -> SignedApproval          # writes approval_records row
#   .consume(...) called inside coordinator ; raises ApprovalError
# graphite.routing.storage.RepositoryStore.approval_status(approval_id) -> str | None  # "consumed"

# graphite.routing.openrouter_executor
preflight_openrouter(*, api_key, model_id, routing_policy, observed_at, policy_version,
    transport=run_http_probe) -> OpenRouterPreflight  # .runtime(.digest), .pricing
execute_openrouter(*, api_key, prompt, requested_model, expected_effective_model, effort,
    output_schema, output_schema_sha256, pricing, max_output_tokens, max_cost_microunits,
    timeout_seconds, response_format_type, transport=run_http_probe)
    -> OpenRouterExecutionResult  # .message, .effective_model, .input_tokens, .output_tokens, .cost_microunits

# graphite.routing.probe_runner.run_http_probe ; HttpProbeResult(status_code, body_bytes, body_sha256, duration_seconds)
# graphite.routing.profiles.load_verified_capability_snapshots(store, *, now) -> tuple[...]  # .digest, .expires_at, .profile
# graphite.routing.lifecycle_storage.LifecycleStore(fixture).current_observation(boundary) -> obs(.state, .identity)
# graphite.routing.lifecycle.ProviderLifecycleState.ACTIVE ; LifecycleProviderId.OPENROUTER ; RuntimeKind.REMOTE_HTTPS
# graphite.routing.lifecycle_service.ProviderLifecycleService(lifecycle_store, routing_store).route_authority(boundary, candidate)
# graphite.routing.telemetry.CliTelemetryRecord(...); record_cli_telemetry(store, record) -> bool
# graphite.routing.contracts: Effort, EvidenceProvenance, ProviderId, RiskTier, TaskCategory, PermissionMode
```

---

### Task 1: Prepare manifest script + frozen golden diff (`_prepare_openrouter_review_smoke.py`)

**Files:**
- Create: `F:\tmp\graphite-live-acceptance-harness\openrouter-review-diff.txt`
- Create: `F:\tmp\graphite-live-acceptance-harness\_prepare_openrouter_review_smoke.py`
- Test (scratchpad): `…\scratchpad\dryrun_review_smoke_construct.py`

**Interfaces:**
- Consumes: `_prepare_openrouter_pool_registration` (as `reg`: `FIXTURE`, `ROUTING_PATH`, `LIFECYCLE_PATH`, `FIXTURE_COMMIT`, `FIXTURE_GRAPH`, `digest`, `boundary`, `_RISK`, `_PERM`, `_EFFORT`); `graphite.routing.route_pool.{ApprovedRouteCandidate, ApprovedRoutePool}`; `graphite.routing.lifecycle.{LifecycleProviderId, RuntimeKind}`.
- Produces (names the execute imports): `FIXTURE`, `ROUTING_PATH`, `LIFECYCLE_PATH`, `FIXTURE_COMMIT`, `IMPLEMENTATION_COMMIT`, `ROUTING_STORE_SHA256`, `LIFECYCLE_STORE_SHA256`, `EXISTING_STORE_CONTRACT`, `EXPECTED_FINAL_STORE_CONTRACT`, `ISSUED_AT`, `EXPIRES_AT`, `POOL_NOW`, `CANDIDATE_SPECS`, `build_candidate`, `build_pool`, `POOL_SPEC`, `digest`, `REVIEW_PROMPT`, `REVIEW_SCHEMA`, `REVIEW_SCHEMA_SHA256`, `REVIEW_DIFF_SHA256`, `MAX_INPUT_TOKENS`, `MAX_OUTPUT_TOKENS`, `TIMEOUT_SECONDS`, `MAX_COST_MICROUNITS`, `ROUTING_POLICY`, `POLICY_VERSION`, `REPOSITORY_QUOTA_TOKENS`, `MACHINE_QUOTA_TOKENS`, `APPROVAL_KEY_PATH`, `APPROVAL_QUOTA_PATH`, `BUNDLE`, `BUNDLE_DIGEST`.

- [ ] **Step 1: Freeze the golden diff into a sidecar file**

Capture the known-correct tenant-authorization diff once (deterministic bytes, LF), then verify its size and hash:
```bash
GD='F:/tmp/graphite-production-live-worktrees/expected-codex'
FC='05f01737326469bd951ec677a0cf73b68caf9fe1'
OUT='F:/tmp/graphite-live-acceptance-harness/openrouter-review-diff.txt'
git -c safe.directory="$GD" -C "$GD" diff --no-ext-diff --no-color --full-index --no-renames "$FC" -- > "$OUT"
python -c "import hashlib; d=open(r'$OUT','rb').read(); print('bytes',len(d),'sha256',hashlib.sha256(d).hexdigest(),'crlf',b'\r\n' in d)"
```
Expected: `bytes 999 sha256 7a6fc7ed5a1da4a912fefc58d52b9ae93972eaf65e630fe83436eb4f07f23ee2 crlf False`. If the bytes/sha differ (git line-ending normalization), STOP — do not proceed with a different diff; re-capture until it matches (the sha is pinned in the bundle). This file is frozen: never regenerated at prepare/execute time, only read.

- [ ] **Step 2: Write the prepare script**

```python
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import _prepare_openrouter_pool_registration as reg
from graphite.routing.route_pool import ApprovedRouteCandidate, ApprovedRoutePool
from graphite.routing.lifecycle import LifecycleProviderId, RuntimeKind

FIXTURE = reg.FIXTURE
ROUTING_PATH = reg.ROUTING_PATH
LIFECYCLE_PATH = reg.LIFECYCLE_PATH
FIXTURE_COMMIT = reg.FIXTURE_COMMIT
# No graphite source change; pin the CURRENT clean feature HEAD at run time (Task 3, Step 1).
IMPLEMENTATION_COMMIT = "REPLACE_WITH_CURRENT_HEAD_AT_TASK3_STEP1"
ROUTING_STORE_SHA256 = "aef018b0b6ea7243330aa48995e43e9babb6e70b0cadb5347f2154d59daab815"
LIFECYCLE_STORE_SHA256 = "a99f7cc454e4b8ad000c1d0ba6b203b44cf6d12c3fef1b9c57d428b236bef890"

digest = reg.digest

ISSUED_AT = 1784578461
EXPIRES_AT = 1784623000          # = min(verification snapshot expiry) = k2.7-code snapshot_expires_at
# POOL_NOW frozen; safe ONLY because EXPIRES_AT == min snapshot expiry AND the execute preflight
# fails closed on time.time() >= EXPIRES_AT. Do NOT bump POOL_NOW past a snapshot expiry.
POOL_NOW = 1784578461

ROUTING_POLICY = {"allow_fallbacks": False}
POLICY_VERSION = "1.0.0"

MAX_INPUT_TOKENS = 8_192
MAX_OUTPUT_TOKENS = 4_096
TIMEOUT_SECONDS = 180.0
MAX_COST_MICROUNITS = 64_000     # worst-case ~22.5k (4096 out + 8192 in @ k2.7 pricing); ~2.8x headroom

REPOSITORY_QUOTA_TOKENS = 262_144
MACHINE_QUOTA_TOKENS = 262_144
# ApprovalAuthority REJECTS a key/quota path inside the store's repository root
# (approval_key_path_invalid / quota_path_invalid). This state dir is outside FIXTURE.
_STATE_DIR = Path(r"F:\tmp\graphite-live-acceptance-harness") / ".review-smoke-state"
APPROVAL_KEY_PATH = _STATE_DIR / "approval.key"
APPROVAL_QUOTA_PATH = _STATE_DIR / "quota.sqlite3"

# --- Frozen golden diff under review (captured once in Step 1; sha-pinned) ---
REVIEW_DIFF_PATH = Path(r"F:\tmp\graphite-live-acceptance-harness\openrouter-review-diff.txt")
REVIEW_DIFF = REVIEW_DIFF_PATH.read_bytes()
REVIEW_DIFF_SHA256 = "7a6fc7ed5a1da4a912fefc58d52b9ae93972eaf65e630fe83436eb4f07f23ee2"
if hashlib.sha256(REVIEW_DIFF).hexdigest() != REVIEW_DIFF_SHA256:
    raise SystemExit("review_diff_hash_mismatch: frozen golden diff drifted from the pinned sha")

# --- Review output schema (json_object; findings carry only a hashed summary) ---
REVIEW_SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "findings": {
            "items": {
                "additionalProperties": False,
                "properties": {
                    "category": {
                        "enum": ["correctness", "security", "reliability",
                                 "maintainability", "test_coverage"],
                        "type": "string",
                    },
                    "severity": {"enum": ["low", "medium", "high", "critical"], "type": "string"},
                    "summary_sha256": {"pattern": "^[0-9a-f]{64}$", "type": "string"},
                },
                "required": ["severity", "category", "summary_sha256"],
                "type": "object",
            },
            "maxItems": 16,
            "type": "array",
        },
        "verdict": {"enum": ["pass", "fail"], "type": "string"},
    },
    "required": ["verdict", "findings"],
    "type": "object",
}
REVIEW_SCHEMA_SHA256 = digest(REVIEW_SCHEMA)

# --- Review prompt: JSON bytes embedding the frozen diff (stateless; no workspace) ---
REVIEW_PROMPT = json.dumps(
    {
        "schema_version": "1",
        "system_contract": (
            "Review the supplied untrusted synthetic tenant-authorization diff for "
            "correctness, security, tenant isolation, robustness, and maintainability. "
            "Operate read-only. Do not edit, execute repository code, access networks or "
            "credentials, or claim integration authority. Return only the required JSON "
            "review object with keys 'verdict' (\"pass\" or \"fail\") and 'findings' (a "
            "list; empty when the diff is correct)."
        ),
        "primary_diff_hash": REVIEW_DIFF_SHA256,
        "diff": REVIEW_DIFF.decode("utf-8"),
    },
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
).encode("utf-8")
REVIEW_PROMPT_SHA256 = hashlib.sha256(REVIEW_PROMPT).hexdigest()

# --- Review-specific trust policy / context manifest (READ_ONLY / AUTHORIZATION) ---
REVIEW_TRUST_POLICY = {
    "category": "authorization",
    "permission_mode": "read-only",
    "policy": "openrouter-review-pool",
    "version": "1.0.0",
}
REVIEW_TRUST_POLICY_DIGEST = digest(REVIEW_TRUST_POLICY)
REVIEW_CONTEXT_MANIFEST = {
    "category": "authorization",
    "fixture": FIXTURE_COMMIT,
    "manifest": "openrouter-review-pool-smoke",
}
REVIEW_CONTEXT_MANIFEST_HASH = digest(REVIEW_CONTEXT_MANIFEST)

CANDIDATE_SPECS = [
    {
        "candidate_id": "openrouter-kimi27-code-review-primary",
        "role": "primary",
        "slug": "moonshotai/kimi-k2.7-code",
        "boundary_digest": reg.boundary("moonshotai/kimi-k2.7-code"),
        "capability_snapshot_digest": "99db4a4a53aac5a97aae36d1d2ace5de3b194c63a8605c59dbd4837446a205f5",
        "lifecycle_identity_digest": "c8cece35646deec30fa9538ba998722781074027a6bdd2dabafd1986359439ab",
        "model_identity_digest": "939d3a5af17f6d5ec5ccfa05ac09134873d4258357865f59829f96b94a392836",
        "routing_policy_digest": "916df225733c25f6d9976d29edb6b7a2050f61457fa978e178544bcd53615b39",
        "capabilities": ("code", "reasoning", "thinking", "vision"),
        "context_window_tokens": 262144,
        "risk_ceiling": "high",
        "permission_mode": "read-only",
        "effort": "high",
        "snapshot_expires_at": 1784623000,
    },
    {
        "candidate_id": "openrouter-kimi26-review-fallback",
        "role": "capacity_fallback",
        "slug": "moonshotai/kimi-k2.6",
        "boundary_digest": reg.boundary("moonshotai/kimi-k2.6"),
        "capability_snapshot_digest": "6816edafc45f164fa0dfec5009121afc6862c26c72c5963798177358f463b26d",
        "lifecycle_identity_digest": "6333c4d577f8fcb111f57f23c4c2f6b3ab889cc3ba19edfb78cc082a63f6bade",
        "model_identity_digest": "e504400884ef6c43f66fc983060d8da6d3ea81fe189e491f09a25775e4b0b10b",
        "routing_policy_digest": "916df225733c25f6d9976d29edb6b7a2050f61457fa978e178544bcd53615b39",
        "capabilities": ("code", "reasoning", "tools", "vision"),
        "context_window_tokens": 262144,
        "risk_ceiling": "high",
        "permission_mode": "read-only",
        "effort": "high",
        "snapshot_expires_at": 1784628600,
    },
]

POOL_SPEC = {
    "approval_id": "openrouter-review-smoke-1",
    "task_id": "openrouter-review-smoke",
    "decision_id": "openrouter-review-smoke-decision-1",
    "required_capabilities": ("code", "reasoning", "vision"),
    "task_risk": "high",
    "permission_mode": "read-only",
    "trust_policy_digest": REVIEW_TRUST_POLICY_DIGEST,
    "graph_fingerprint": reg.FIXTURE_GRAPH,
    "context_manifest_hash": REVIEW_CONTEXT_MANIFEST_HASH,
    "repository_commit": FIXTURE_COMMIT,
    "worktree_id": "openrouter-review-smoke",
    "allow_cross_provider": False,
    "allowed_fallback_reasons": ("capacity_unavailable",),
    "max_attempts": 2,
    "max_input_tokens": MAX_INPUT_TOKENS,
    "max_output_tokens": MAX_OUTPUT_TOKENS,
    "max_duration_ms": 180_000,
    "max_cost_microunits": MAX_COST_MICROUNITS,
    "policy_version": "1.0.0",
    "issued_at": ISSUED_AT,
    "expires_at": EXPIRES_AT,
    "nonce": "openrouter-review-smoke-nonce-1",
}


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
        effort=reg._EFFORT[spec["effort"]],
        permission_mode=reg._PERM[spec["permission_mode"]],
        risk_ceiling=reg._RISK[spec["risk_ceiling"]],
        trust_policy_digest=REVIEW_TRUST_POLICY_DIGEST,
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
        task_risk=reg._RISK[POOL_SPEC["task_risk"]],
        permission_mode=reg._PERM[POOL_SPEC["permission_mode"]],
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


EXISTING_STORE_CONTRACT = {
    "schema_version": "6",
    "capability_snapshots": 12,
    "lifecycle_snapshot_bindings": 12,
    "telemetry_events": 22,
    "foreign_key_violations": 0,
    "integrity": "ok",
}
EXPECTED_FINAL_STORE_CONTRACT = {
    **EXISTING_STORE_CONTRACT,
    "telemetry_events": 23,          # +1 routed AUTHORIZATION review telemetry
}

BUNDLE = {
    "schema_version": "1",
    "purpose": "graphite_openrouter_review_smoke",
    "issued_at": ISSUED_AT,
    "expires_at": EXPIRES_AT,
    "implementation_commit": IMPLEMENTATION_COMMIT,
    "fixture_commit": FIXTURE_COMMIT,
    "routing_store_sha256": ROUTING_STORE_SHA256,
    "lifecycle_store_sha256": LIFECYCLE_STORE_SHA256,
    "existing_store_contract": EXISTING_STORE_CONTRACT,
    "expected_final_store_contract": EXPECTED_FINAL_STORE_CONTRACT,
    "task_category": "authorization",
    "permission_mode": "read-only",
    "pool_now": POOL_NOW,
    "candidates": [
        {"position": i, "candidate_id": s["candidate_id"], "role": s["role"], "slug": s["slug"],
         "capability_snapshot_digest": s["capability_snapshot_digest"],
         "lifecycle_identity_digest": s["lifecycle_identity_digest"],
         "snapshot_expires_at": s["snapshot_expires_at"], "permission_mode": s["permission_mode"]}
        for i, s in enumerate(CANDIDATE_SPECS)
    ],
    "pool_candidate_digest": [build_candidate(s).digest for s in CANDIDATE_SPECS],
    "pool_digest": build_pool(tuple(build_candidate(s) for s in CANDIDATE_SPECS)).digest,
    "review_target": {
        "reviewed_from": ("expected-codex golden tenant-authorization diff "
                          "(codex-authored, pytest-validated; substitutes for r7/r8 per plan note)"),
        "diff_sha256": REVIEW_DIFF_SHA256,
        "diff_bytes": len(REVIEW_DIFF),
        "prompt_contract_hash": REVIEW_PROMPT_SHA256,
        "response_contract_hash": REVIEW_SCHEMA_SHA256,
        "response_format_type": "json_object",
        "oracle": "verdict == 'pass' and findings == []",
    },
    "maximum_cost_microunits": MAX_COST_MICROUNITS,
    "live_inference": True,
    "network": True,
    "store_write": True,
    "expected_mutation": {
        "approval_records": "0 -> 1 (consumed)",
        "cli_telemetry_events": "22 -> 23",
        "capability_snapshots": "12 (unchanged)",
        "lifecycle_snapshot_bindings": "12 (unchanged)",
    },
    "credential_source": "session_environment:OPENROUTER_API_KEY",
    "credential_in_argv": False,
    "credential_persisted": False,
    "forbidden_persistence": [
        "account_metadata", "credential_material", "diff_content",
        "executable_or_credential_paths", "finding_text", "prompt_body",
        "provider_diagnostics", "repository_source", "response_body",
        "stderr_body", "stdout_body",
    ],
    "raw_provider_output_persistence": False,
    "retry": False,
    "resume": False,
    "merge": False,
    "push": False,
    "deploy": False,
}
BUNDLE_DIGEST = digest(BUNDLE)


if __name__ == "__main__":
    print(json.dumps({"bundle": BUNDLE, "bundle_digest": BUNDLE_DIGEST}, sort_keys=True))
```

- [ ] **Step 3: Run the prepare script**

Run:
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src;F:/tmp/graphite-live-acceptance-harness" python "F:/tmp/graphite-live-acceptance-harness/_prepare_openrouter_review_smoke.py"
```
Expected: one JSON line with a 64-hex `bundle_digest`, a two-element `pool_candidate_digest`, a `pool_digest`, `task_category:"authorization"`, `permission_mode:"read-only"`, `live_inference:true`, `store_write:true`, `review_target.diff_sha256 == 7a6fc7ed…`, and `expected_final_store_contract.telemetry_events == 23`. No exception (frozen diff hash matches; candidates + READ_ONLY pool construct).

- [ ] **Step 4: Write + run the constructibility dry-run**

Create scratchpad `dryrun_review_smoke_construct.py`:
```python
from __future__ import annotations

import _prepare_openrouter_review_smoke as prep

candidates = tuple(prep.build_candidate(s) for s in prep.CANDIDATE_SPECS)
pool = prep.build_pool(candidates)
assert [c.candidate_id for c in candidates] == [
    "openrouter-kimi27-code-review-primary", "openrouter-kimi26-review-fallback"]
assert pool.permission_mode is prep.reg._PERM["read-only"]
assert pool.task_risk is prep.reg._RISK["high"]
assert pool.max_cost_microunits == 64_000
assert pool.max_output_tokens == 4_096 and pool.max_input_tokens == 8_192
assert pool.max_attempts == 2 and pool.allowed_fallback_reasons == ("capacity_unavailable",)
for c in candidates:
    assert c.permission_mode is prep.reg._PERM["read-only"]
    assert set(pool.required_capabilities).issubset(c.capabilities)
    assert c.snapshot_expires_at >= pool.expires_at
    assert c.trust_policy_digest == pool.trust_policy_digest
assert pool.expires_at > pool.issued_at
assert prep.EXPECTED_FINAL_STORE_CONTRACT["telemetry_events"] == 23
assert set(prep.REVIEW_SCHEMA["required"]) == {"verdict", "findings"}
print("DRYRUN_OK: review pool constructs (READ_ONLY/HIGH); pool_digest", pool.digest[:12])
```
Run:
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src;F:/tmp/graphite-live-acceptance-harness" python "…/scratchpad/dryrun_review_smoke_construct.py"
```
Expected: `DRYRUN_OK: review pool constructs (READ_ONLY/HIGH); pool_digest …`. A `RoutePoolError` here is a clean finding — record it and stop; never weaken a check to force construction.

- [ ] **Step 5: No repo commit for Task 1** — harness scripts + the frozen diff live outside the graphite repo. Completion = the three offline runs (Step 1 hash, Step 3 prepare, Step 4 construct) pass.

---

### Task 2: Execute script (`_execute_openrouter_review_smoke.py`)

**Files:**
- Create: `F:\tmp\graphite-live-acceptance-harness\_execute_openrouter_review_smoke.py`
- Test (scratchpad): `…\scratchpad\dryrun_review_smoke.py`

**Interfaces:**
- Consumes: `_prepare_openrouter_review_smoke` (all Task 1 names); `graphite.routing` modules per the Reference block. Overridable module globals for the dry-run: `FIXTURE`, `ROUTING_PATH`, `LIFECYCLE_PATH`, `APPROVAL_KEY_PATH`, `APPROVAL_QUOTA_PATH`, `PREFLIGHT_OPENROUTER` (function alias), `EXECUTE_TRANSPORT`.
- Produces: one sanitized JSON receipt; exit 0 on pass, 1 on any fail-closed category.

- [ ] **Step 1: Write the execute script**

```python
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import _prepare_openrouter_review_smoke as prepared
from graphite.routing.claude_executor import AdapterError
from graphite.routing.contracts import (
    Effort, EvidenceProvenance, ProviderId, RiskTier, TaskCategory,
)
from graphite.routing.lifecycle import ProviderLifecycleState
from graphite.routing.lifecycle_service import LifecycleServiceError, ProviderLifecycleService
from graphite.routing.lifecycle_storage import LifecycleStore
from graphite.routing.openrouter_executor import execute_openrouter, preflight_openrouter
from graphite.routing.profiles import load_verified_capability_snapshots
from graphite.routing.probe_runner import run_http_probe
from graphite.routing.route_pool import RoutePoolError
from graphite.routing.route_pool_execution import (
    RouteExecutionResult, execute_approved_route_pool,
)
from graphite.routing.approval import ApprovalAuthority, ApprovalError
from graphite.routing.storage import RepositoryStore
from graphite.routing.telemetry import CliTelemetryRecord, record_cli_telemetry

FIXTURE = prepared.FIXTURE
ROUTING_PATH = prepared.ROUTING_PATH
LIFECYCLE_PATH = prepared.LIFECYCLE_PATH
APPROVAL_KEY_PATH = prepared.APPROVAL_KEY_PATH
APPROVAL_QUOTA_PATH = prepared.APPROVAL_QUOTA_PATH
PREFLIGHT_OPENROUTER = preflight_openrouter   # dry-run replaces with a stub (proven; identity-hard to fake)
EXECUTE_TRANSPORT = run_http_probe            # dry-run replaces with a fake transport


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
                "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0],
            "capability_snapshots": connection.execute(
                "SELECT COUNT(*) FROM capability_snapshots").fetchone()[0],
            "lifecycle_snapshot_bindings": connection.execute(
                "SELECT COUNT(*) FROM lifecycle_snapshot_bindings").fetchone()[0],
            "telemetry_events": connection.execute(
                "SELECT COUNT(*) FROM cli_telemetry_events").fetchone()[0],
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()),
            "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        }
    finally:
        connection.close()


# --- the real review work; NO worktree, NO apply, NO diff, NO validation subprocess ---
def perform_review(selection, api_key, spec, run_context):
    """Run one live json_object review for the selected candidate; return result fields."""
    slug = spec["slug"]
    preflight = PREFLIGHT_OPENROUTER(
        api_key=api_key, model_id=slug, routing_policy=prepared.ROUTING_POLICY,
        observed_at=int(time.time()), policy_version=prepared.POLICY_VERSION,
    )
    if preflight.runtime.digest != spec["lifecycle_identity_digest"]:
        raise HarnessFailure("review_identity_changed", candidate_id=spec["candidate_id"])
    started = time.monotonic()
    result = execute_openrouter(
        api_key=api_key, prompt=prepared.REVIEW_PROMPT, requested_model=slug,
        expected_effective_model=slug, effort=Effort.HIGH,
        output_schema=prepared.REVIEW_SCHEMA, output_schema_sha256=prepared.REVIEW_SCHEMA_SHA256,
        pricing=preflight.pricing, max_output_tokens=prepared.MAX_OUTPUT_TOKENS,
        max_cost_microunits=prepared.MAX_COST_MICROUNITS, timeout_seconds=prepared.TIMEOUT_SECONDS,
        response_format_type="json_object", transport=EXECUTE_TRANSPORT,
    )
    duration_ms = round((time.monotonic() - started) * 1_000)
    try:
        payload = json.loads(result.message)
    except (json.JSONDecodeError, RecursionError):
        raise HarnessFailure("review_response_invalid") from None
    if (not isinstance(payload, dict)
            or set(payload) != {"verdict", "findings"}
            or not isinstance(payload["findings"], list)):
        raise HarnessFailure("review_response_invalid")
    verdict = payload["verdict"]
    findings = payload["findings"]
    if verdict != "pass" or findings != []:
        raise HarnessFailure(
            "review_not_accepted", verdict=str(verdict)[:16], finding_count=len(findings))
    run_context.update({
        "snapshot_digest": spec["capability_snapshot_digest"],
        "requested_model": slug, "effective_model": result.effective_model,
        "verdict": verdict, "finding_count": len(findings),
        "reviewed_diff_sha256": prepared.REVIEW_DIFF_SHA256,
    })
    output = json.dumps(
        {"verdict": verdict, "finding_count": len(findings)},
        sort_keys=True, separators=(",", ":"),
    )
    return {
        "output": output, "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens, "duration_ms": duration_ms,
        "cost_microunits": result.cost_microunits,
    }


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--approved", required=True)
    arguments = parser.parse_args()
    action = "local_preflight"
    runner_failure: dict[str, object] = {}
    try:
        if arguments.approved != prepared.BUNDLE_DIGEST:
            raise HarnessFailure("manifest_approval_mismatch")
        if int(time.time()) >= prepared.EXPIRES_AT:
            raise HarnessFailure("manifest_expired")
        if prepared.digest(prepared.BUNDLE) != prepared.BUNDLE_DIGEST:
            raise HarnessFailure("manifest_digest_mismatch")
        if (file_sha256(ROUTING_PATH) != prepared.ROUTING_STORE_SHA256
                or file_sha256(LIFECYCLE_PATH) != prepared.LIFECYCLE_STORE_SHA256):
            raise HarnessFailure("source_store_changed")
        before_audit = store_audit()
        if before_audit != prepared.EXISTING_STORE_CONTRACT:
            raise HarnessFailure("source_store_audit_failed", **before_audit)
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            raise HarnessFailure("credential_missing")

        action = "resolution"
        routing_store = RepositoryStore(FIXTURE)
        lifecycle_store = LifecycleStore(FIXTURE)
        service = ProviderLifecycleService(lifecycle_store, routing_store)
        if routing_store.approval_status(prepared.POOL_SPEC["approval_id"]) is not None:
            raise HarnessFailure("approval_id_reused")
        by_digest = {s.digest: s for s in load_verified_capability_snapshots(routing_store, now=0)}
        pool_now = prepared.POOL_NOW
        candidates, specs = [], []
        for spec in prepared.CANDIDATE_SPECS:
            snapshot = by_digest.get(spec["capability_snapshot_digest"])
            if snapshot is None:
                raise HarnessFailure("pool_candidate_unbound", candidate_id=spec["candidate_id"])
            if snapshot.expires_at <= pool_now:
                raise HarnessFailure("pool_candidate_source_expired", candidate_id=spec["candidate_id"])
            observation = lifecycle_store.current_observation(spec["boundary_digest"])
            if observation is None or observation.identity is None:
                raise HarnessFailure("pool_candidate_unbound", candidate_id=spec["candidate_id"])
            if observation.state is not ProviderLifecycleState.ACTIVE:
                raise HarnessFailure("pool_candidate_inactive", candidate_id=spec["candidate_id"])
            identity = observation.identity
            binding = routing_store.lifecycle_identity_binding(
                authority_kind="capability_snapshot", authority_id=snapshot.digest)
            if (binding != identity.digest
                    or identity.digest != spec["lifecycle_identity_digest"]
                    or identity.model_identity_digest != spec["model_identity_digest"]
                    or identity.routing_policy_digest != spec["routing_policy_digest"]
                    or snapshot.profile.effective_model != spec["slug"]
                    or snapshot.profile.permission_mode.value != spec["permission_mode"]
                    or snapshot.expires_at != spec["snapshot_expires_at"]
                    or set(spec["capabilities"]) - set(snapshot.profile.capabilities)):
                raise HarnessFailure("pool_candidate_drift", candidate_id=spec["candidate_id"])
            try:
                candidates.append(prepared.build_candidate(spec))
            except RoutePoolError as error:
                raise HarnessFailure("pool_candidate_invalid", code=error.code) from None
            specs.append(spec)

        action = "pool_construction"
        try:
            pool = prepared.build_pool(tuple(candidates))
        except RoutePoolError as error:
            raise HarnessFailure("pool_invalid", code=error.code) from None
        spec_by_candidate = {c.candidate_id: s for c, s in zip(candidates, specs)}

        action = "issue"
        APPROVAL_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:  # ApprovalAuthority(...) itself raises ApprovalError on a bad key/quota path
            approval_authority = ApprovalAuthority(
                routing_store, key_path=APPROVAL_KEY_PATH, quota_path=APPROVAL_QUOTA_PATH,
                now=lambda: int(time.time()),
            )
            signed = approval_authority.issue(pool)
        except ApprovalError as error:
            raise HarnessFailure("approval_issue_failed", code=error.code) from None

        run_context: dict[str, object] = {}

        def authority_loader():
            return tuple(
                service.route_authority(spec_by_candidate[c.candidate_id]["boundary_digest"], c)
                for c in pool.candidates
            )

        def runner(selection):
            spec = spec_by_candidate[selection.candidate.candidate_id]
            try:
                fields = perform_review(selection, api_key, spec, run_context)
            except AdapterError as error:
                runner_failure["code"] = error.code
                raise
            except HarnessFailure as failure:
                runner_failure.update({"code": failure.code, **failure.evidence})
                raise
            return RouteExecutionResult(
                candidate_id=selection.candidate.candidate_id,
                candidate_digest=selection.candidate.digest,
                attempt_ordinal=selection.attempt_ordinal,
                output=fields["output"], input_tokens=fields["input_tokens"],
                output_tokens=fields["output_tokens"], duration_ms=fields["duration_ms"],
                cost_microunits=fields["cost_microunits"],
            )

        def evidence_sink(evidence):
            if evidence.outcome_category != "succeeded":
                return  # only the succeeded routed review is persisted as telemetry
            record = CliTelemetryRecord(
                provider=ProviderId.OPENROUTER,
                requested_model=run_context["requested_model"],
                effective_model=run_context["effective_model"],
                effort=Effort.HIGH,
                capability_snapshot_digest=run_context["snapshot_digest"],
                category=TaskCategory.AUTHORIZATION, risk=RiskTier.HIGH,
                latency_ms=evidence.duration_ms,
                input_tokens=evidence.input_tokens, output_tokens=evidence.output_tokens,
                changed_file_count=0, changed_byte_count=0,
                validation_outcome="passed", review_defect_classes=(), rework_count=0,
                human_verdict=None, provenance=EvidenceProvenance.MACHINE_VERIFIED,
                observed_at=int(time.time()), diff_sha256=run_context["reviewed_diff_sha256"],
            )
            if not record_cli_telemetry(routing_store, record):
                raise RoutePoolError("review_telemetry_persistence_failed")

        action = "routed_execution"
        try:
            result = execute_approved_route_pool(
                pool=pool, signed_approval=signed, approval_authority=approval_authority,
                authority_loader=authority_loader, runner=runner, evidence_sink=evidence_sink,
                repository_quota_tokens=prepared.REPOSITORY_QUOTA_TOKENS,
                machine_quota_tokens=prepared.MACHINE_QUOTA_TOKENS,
                now=lambda: int(time.time()),
            )
        except (ApprovalError, RoutePoolError, LifecycleServiceError) as error:
            raise HarnessFailure(
                getattr(error, "code", "route_execution_failed"),
                runner_failure=runner_failure or None) from None

        if result.candidate_id != pool.candidates[0].candidate_id:
            raise HarnessFailure("review_wrong_candidate", selected=result.candidate_id)
        if run_context.get("verdict") != "pass" or run_context.get("finding_count") != 0:
            raise HarnessFailure("review_not_accepted", **{
                k: run_context.get(k) for k in ("verdict", "finding_count")})
        if routing_store.approval_status(pool.approval_id) != "consumed":
            raise HarnessFailure("approval_not_consumed")

        action = "final_audit"
        after_audit = store_audit()
        if after_audit != prepared.EXPECTED_FINAL_STORE_CONTRACT:
            raise HarnessFailure("final_audit_failed", **after_audit)
        print(json.dumps({
            "status": "passed", "bundle_digest": prepared.BUNDLE_DIGEST,
            "purpose": prepared.BUNDLE["purpose"],
            "selected_candidate": result.candidate_id, "attempt_ordinal": result.attempt_ordinal,
            "verdict": run_context["verdict"], "finding_count": run_context["finding_count"],
            "reviewed_diff_sha256": run_context["reviewed_diff_sha256"],
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "duration_ms": result.duration_ms, "cost_microunits": result.cost_microunits,
            "approval_status": "consumed",
            "telemetry_before": before_audit["telemetry_events"],
            "telemetry_after": after_audit["telemetry_events"],
            "snapshots_unchanged": after_audit["capability_snapshots"] == 12,
            "bindings_unchanged": after_audit["lifecycle_snapshot_bindings"] == 12,
            "audit": after_audit, "merge": False, "push": False, "deploy": False,
        }, sort_keys=True, separators=(",", ":")))
        return 0
    except HarnessFailure as failure:
        print(json.dumps({
            "status": "failed", "action": action, "failure_category": failure.code,
            "bundle_digest": prepared.BUNDLE_DIGEST, **failure.evidence,
        }, sort_keys=True, separators=(",", ":")))
        return 1
    except Exception:
        print(json.dumps({
            "status": "failed", "action": action, "failure_category": "harness_failed",
            "bundle_digest": prepared.BUNDLE_DIGEST, "runner_failure": runner_failure or None,
        }, sort_keys=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Write the offline end-to-end dry-run (store copies; REAL runner, fake transport, both paths)**

Unlike the routed-smoke dry-run (which stubbed the whole edit because the edit mechanics were already proven live), the review parse + oracle + `execute_openrouter`-review-shape are **new** for the OpenRouter path (the precedent proved them through `execute_claude`). So this dry-run exercises the **real** `perform_review` with a **fake transport** (only the proven, identity-hard-to-fake `preflight` is stubbed). It covers both the accept and reject paths on separate fresh copies.

Create scratchpad `dryrun_review_smoke.py`:
```python
from __future__ import annotations

import contextlib
import gc, hashlib, io, json, shutil, sqlite3, sys
from pathlib import Path

import _execute_openrouter_review_smoke as ex
import _prepare_openrouter_review_smoke as prep
from graphite.routing.openrouter_probe import OpenRouterPricing
from graphite.routing.probe_runner import HttpProbeResult

LIVE = prep.FIXTURE
RR = Path(".graphite") / "routing" / "events.sqlite3"
LR = Path(".graphite") / "routing" / "provider-lifecycle.sqlite3"
PRICING = OpenRouterPricing(prompt="0.00000085", completion="0.0000038")
SPEC_BY_SLUG = {s["slug"]: s for s in prep.CANDIDATE_SPECS}


def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()


class _Runtime:
    def __init__(self, d): self.digest = d


class _Preflight:
    def __init__(self, d, pricing): self.runtime = _Runtime(d); self.pricing = pricing


def fake_preflight(*, api_key, model_id, routing_policy, observed_at, policy_version):
    return _Preflight(SPEC_BY_SLUG[model_id]["lifecycle_identity_digest"], PRICING)


def make_transport(content_obj):
    def transport(**kwargs):
        body = json.loads(kwargs["request_body"])
        assert body["response_format"] == {"type": "json_object"}, body["response_format"]
        content = json.dumps(content_obj)
        envelope = json.dumps({
            "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 420, "completion_tokens": 12},
        }).encode()
        return HttpProbeResult(200, envelope, hashlib.sha256(envelope).hexdigest(), 0.05)
    return transport


def run_case(content_obj, state_suffix):
    workdir = Path(rf"F:\tmp\graphite-review-smoke-dryrun-{state_suffix}")
    state = Path(rf"F:\tmp\graphite-review-smoke-dryrun-state-{state_suffix}")
    shutil.rmtree(workdir, ignore_errors=True); shutil.rmtree(state, ignore_errors=True)
    (workdir / RR).parent.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LIVE / RR, workdir / RR)
    shutil.copy2(LIVE / LR, workdir / LR)
    ex.FIXTURE = workdir
    ex.ROUTING_PATH = workdir / RR
    ex.LIFECYCLE_PATH = workdir / LR
    ex.APPROVAL_KEY_PATH = state / "approval.key"
    ex.APPROVAL_QUOTA_PATH = state / "quota.sqlite3"
    ex.PREFLIGHT_OPENROUTER = fake_preflight
    ex.EXECUTE_TRANSPORT = make_transport(content_obj)
    prep.ROUTING_STORE_SHA256 = sha(workdir / RR)
    prep.LIFECYCLE_STORE_SHA256 = sha(workdir / LR)
    prep.BUNDLE["routing_store_sha256"] = prep.ROUTING_STORE_SHA256
    prep.BUNDLE["lifecycle_store_sha256"] = prep.LIFECYCLE_STORE_SHA256
    prep.BUNDLE_DIGEST = prep.digest(prep.BUNDLE)
    sys.argv = ["dryrun", "--approved", prep.BUNDLE_DIGEST]
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = ex.main()
    gc.collect()
    for path in (workdir / RR, workdir / LR):
        c = sqlite3.connect(path)
        try: c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally: c.close()
    con = sqlite3.connect(workdir / RR)
    tel = con.execute("SELECT COUNT(*) FROM cli_telemetry_events").fetchone()[0]
    con.close()
    out = buffer.getvalue()
    shutil.rmtree(workdir, ignore_errors=True); shutil.rmtree(state, ignore_errors=True)
    return code, tel, out


# Accept path: correct diff -> verdict pass, empty findings -> telemetry 22->23.
code, tel, out = run_case({"verdict": "pass", "findings": []}, "pass")
assert code == 0, f"accept case returned {code}: {out}"
assert tel == 23, f"accept telemetry {tel} != 23: {out}"
assert '"selected_candidate":"openrouter-kimi27-code-review-primary"' in out, out
assert '"approval_status":"consumed"' in out, out
print("DRYRUN_OK[accept]: primary review pass consumed approval + telemetry 22->23 on copy")

# Reject path: a finding -> review_not_accepted, approval consumed but NO telemetry (stays 22).
reject_finding = {"category": "correctness", "severity": "high", "summary_sha256": "a" * 64}
code, tel, out = run_case({"verdict": "fail", "findings": [reject_finding]}, "reject")
assert code == 1, f"reject case returned {code}: {out}"
assert tel == 22, f"reject telemetry {tel} != 22 (must not persist): {out}"
assert "review_not_accepted" in out, out
print("DRYRUN_OK[reject]: non-pass verdict fails closed, no telemetry written on copy")
```
Run:
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src;F:/tmp/graphite-live-acceptance-harness" python "…/scratchpad/dryrun_review_smoke.py"
```
Expected: `DRYRUN_OK[accept]: …` then `DRYRUN_OK[reject]: …`. The accept case must print `"status":"passed"` with `selected_candidate":"openrouter-kimi27-code-review-primary"`, `verdict":"pass"`, `finding_count":0`, `approval_status":"consumed"`, `telemetry_after":23`. A `route_*`/`approval_*` failure in the accept case is a clean finding — record it and stop; never weaken a check to force a pass.

- [ ] **Step 3: Run the dry-run** (command above).

- [ ] **Step 4: Suite sanity**

From `F:\tmp\graphite-claude-codex-router`:
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src" python -B -m pytest -q -p no:cacheprovider --basetemp "F:/tmp/graphite-review-smoke-suite" tests/test_route_pool.py tests/test_routing_openrouter_executor.py tests/test_routing_profiles.py
```
Expected: all pass (no source change).

- [ ] **Step 5: No repo commit for Task 2** (harness scripts live outside the repo).

---

### Task 3: Governed live run (operator-executed)

- [ ] **Step 1: Re-pin the manifest.** Set `IMPLEMENTATION_COMMIT` to the current clean feature HEAD; confirm `git -C F:\tmp\graphite-claude-codex-router status --porcelain` is empty; re-hash both live stores and confirm they still equal `aef018b0…` / `a99f7cc4…` (if any intervening store activity, update the pins and re-run the store audit — it must still read `12/12/22`, and the `pool_candidate_drift` check must still pass; if a snapshot expiry changed, re-read the live verification specs). Confirm `now < 1784623000` (the k2.7-code verification snapshot expiry — the binding runway). Re-run Task 1 Step 3 and Task 2 Step 3 after any edit.

- [ ] **Step 2: Display the manifest + request approval.** Run the prepare script; present purpose `graphite_openrouter_review_smoke`, `task_category:authorization`, `permission_mode:read-only`, the ordered candidates (verification snapshots `99db4a4a…` / `6816edaf…`), the `review_target` (frozen golden diff `7a6fc7ed…`, oracle `verdict==pass and findings==[]`), `live_inference:true`, `store_write:true`, `expected_mutation` (approval_records 0→1, telemetry 22→23, snapshots/bindings unchanged), the cost ceiling `64000`, and the `BUNDLE_DIGEST`. State plainly: **this is a live inference that mutates the store and spends budget**, and **the diff-under-review is the expected-codex golden tenant-auth diff substituting for r7/r8** (see the constraint note). Wait for `Approved: graphite_openrouter_review_smoke bundle <BUNDLE_DIGEST>`.

- [ ] **Step 3: Operator runs the proof.** With `OPENROUTER_API_KEY` in the session env, the operator runs (via `!`), **with NO inline `OPENROUTER_API_KEY` prefix** (the key is read from the ambient env):
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src;F:/tmp/graphite-live-acceptance-harness" python "F:/tmp/graphite-live-acceptance-harness/_execute_openrouter_review_smoke.py" --approved <BUNDLE_DIGEST>
```
Expected receipt: `status:passed`, `selected_candidate: openrouter-kimi27-code-review-primary`, `verdict:"pass"`, `finding_count:0`, `reviewed_diff_sha256 7a6fc7ed…`, `approval_status:consumed`, `telemetry_before:22`, `telemetry_after:23`, `snapshots_unchanged:true`, `bindings_unchanged:true`. Capture the JSON line. If the receipt is `credential_missing`, and only then, retry with `OPENROUTER_API_KEY="<real-key>"` prefixed (real key substituted). If it is `review_not_accepted` (the model returned a finding), the approval is spent — do NOT weaken the oracle; a retry needs a fresh governed round (new approval_id/nonce, re-pinned hash, re-approval), mindful of the runway.

- [ ] **Step 4: Post-run confirmation.** Re-hash both stores (they WILL differ now — expected), and confirm via a read-only audit (on a copy): capability_snapshots 12, lifecycle_snapshot_bindings 12, cli_telemetry_events 23, integrity ok, `approval_status("openrouter-review-smoke-1") == "consumed"`. Record the new post-run hashes as the baseline for future rounds.

---

### Task 4: Record evidence and commit

**Files:** Modify `docs/superpowers/implementation-notes/2026-07-19-provider-lifecycle-evidence.md`.

- [ ] **Step 1: Append the review-smoke section** with the sanitized receipt; the routed selection (primary k2.7-code) and the AUTHORIZATION / READ_ONLY category; the review verdict (`pass`) and finding count (`0`); the diff-under-review sha (`7a6fc7ed…`) and the disclosed r7/r8→codex-golden substitution; the intended mutation (approval issued→consumed, telemetry 22→23) and the unchanged snapshots/bindings (12/12); the tokens/cost/duration; the pre-run and new post-run store hashes; and the statement that exactly one live review inference ran, READ_ONLY, no promotion, no lifecycle transition, and main was untouched.

- [ ] **Step 2: Commit** — `docs: record OpenRouter live routed review smoke evidence` (with the `Co-Authored-By` trailer). Confirm the feature worktree is clean. No push/merge.

---

## Self-Review Notes

- **Spec coverage:** goal/flow → Task 2; READ_ONLY/AUTHORIZATION pool from verification snapshots → Task 1 (`CANDIDATE_SPECS`, `build_candidate`, `build_pool`); review runner (stateless `execute_openrouter` + `{verdict,findings}` parse + oracle) → Task 2 `perform_review`; telemetry (AUTHORIZATION/HIGH, 22→23, changed 0/0) → `evidence_sink` + contracts; error handling → the fail-closed categories (`review_response_invalid`, `review_not_accepted`, `review_identity_changed`, `pool_candidate_*`, `route_*`); governance (no inline key, manifest gate, runway) → Global Constraints + Task 3; evidence → Task 4; success criteria → the receipt assertions. Diff-under-review substitution is disclosed in Global Constraints and Task 3 Step 2.
- **Placeholder scan:** `IMPLEMENTATION_COMMIT` is an explicit run-time pin (Task 3 Step 1); `<BUNDLE_DIGEST>` and the API key are genuinely run-time values. The frozen diff sha (`7a6fc7ed…`), store hashes, snapshot/identity digests, and expiry epochs are concrete. No `TODO`/`TBD`.
- **Type consistency:** `perform_review`/`runner`/`evidence_sink`/`authority_loader`/`build_pool`/`build_candidate` names consistent across tasks; `RouteExecutionResult`, `CliTelemetryRecord` (fields per `telemetry.py`: `validation_outcome="passed"` ∈ allowed set; `diff_sha256` 64-hex; `changed_*=0`; `review_defect_classes=()`), `execute_approved_route_pool`, `ApprovalAuthority.issue`, `store.approval_status`, `preflight_openrouter`/`execute_openrouter` kwargs match the Reference block. `PREFLIGHT_OPENROUTER`/`EXECUTE_TRANSPORT` module globals are the dry-run's only injection points into the real runner.
- If a live run surfaces a genuine gap (e.g. a `route_*`/`approval_*` code, or `execute_openrouter` rejecting a valid `{verdict,findings}` under json_object), stop and record it; any minimal `graphite` fix gets its own failing test first (TDD), per the spec.

## Success Criteria

- `execute_approved_route_pool` consumes the signed approval; `select_route` picks the primary (`kimi-k2.7-code`) against its live ACTIVE READ_ONLY AUTHORIZATION authority.
- The live review returns a schema-valid `{verdict:"pass", findings:[]}` within budget; `approval_status("openrouter-review-smoke-1") == "consumed"`; one routed AUTHORIZATION telemetry row persisted (22→23); capability_snapshots and lifecycle_snapshot_bindings unchanged (12/12).
- Sanitized receipt printed; evidence committed on `feat/claude-codex-router`.

## Out of Scope (deferred)

- Forcing the live capacity-fallback path to `kimi-k2.6` (non-deterministic; proven selectable offline).
- Reviewing an intentionally-flawed diff (discriminative review — a stronger but separate smoke).
- The three unverified models (`kimi-k3`, `glm-5.2`, `muse-spark-1.1`); branch integration (push/merge of `feat/claude-codex-router`).
