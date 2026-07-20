# OpenRouter Live Routed Edit Smoke Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route one real `kimi-k2.7-code` edit through `execute_approved_route_pool` against a signed, consumed two-candidate pool — proving the persisted routing path the offline registration left untested — and persist one routed telemetry row, with the applied diff matching the pinned reference `005f1ae8…`.

**Architecture:** A governed prepare/execute harness pair in `F:\tmp\graphite-live-acceptance-harness`, following the rN convention but **live-inference, mutating, and budget-spending**. `_prepare_openrouter_routed_smoke.py` reuses the registered two-candidate composition, adds a real cost ceiling + live budgets + fresh approval identifiers, reuses the r7/r8 tenant-auth edit target, and computes `BUNDLE_DIGEST`. `_execute_openrouter_routed_smoke.py --approved <digest>` reconstructs the candidates/pool from live records, issues a signed approval, and drives `execute_approved_route_pool` with a live `runner` (real `json_object` edit in an isolated worktree), an authority loader, and a telemetry-persisting evidence sink. No `graphite` source change is expected.

**Tech Stack:** Python 3.14; `graphite.routing` (`route_pool_execution`, `route_pool`, `approval`, `lifecycle_service`, `lifecycle_storage`, `storage`, `openrouter_executor`, `openrouter_probe`, `profiles`, `diff_policy`, `worktree`, `telemetry`, `contracts`, `lifecycle`); SQLite (WAL); OpenRouter `chat/completions` (`json_object`).

## Global Constraints

- **Live inference, one call.** Exactly one bounded `execute_openrouter` inference against `moonshotai/kimi-k2.7-code` (json_object). The API key is read only from `OPENROUTER_API_KEY` in the session environment at execute time; it never appears in argv, bundle, or receipt.
- **This round mutates the live store and spends budget** (inherent to a routed smoke). Expected writes: an `approval_records` row (issued → **consumed**), a machine-quota reservation (a separate quota sqlite in a harness state dir), and one routed `cli_telemetry_events` row (**21→22**). Must NOT change capability_snapshots (12) or lifecycle_snapshot_bindings (12) — no promotion, no lifecycle transition. The post-run store hashes/counts become the new pinned baseline.
- **Cost ceiling `128000` microunits** (worst-case k2.7-code `64000` ×2), enforced by both the pool (`max_cost_microunits`) and the runner's `execute_openrouter(max_cost_microunits=…)`; `_validate_result` rejects any result over the remaining budget.
- **Reference diff is the success oracle:** the applied edit must produce `diff_sha256 005f1ae8ae072d35b003b3804cb9b3c0dee49058f4e488eddb3cb3031702b93e`, `changed_files == 2`, `changed_bytes == 1007`; a mismatch fails closed as `routed_diff_mismatch`. Empty diffs are never accepted.
- **Expiry runway.** The run selects the k2.7-code **edit** snapshot (`500cda19…`); it must run before that snapshot expires (`1784648044`). If expired, a governed re-promotion is a prerequisite (separate step).
- **Full manifest/approval gate.** Displayed bundle → operator `Approved: graphite_openrouter_routed_smoke bundle <digest>` → operator runs the execute via the `!` prefix (the classifier blocks the agent's own shell from the live execute). Offline dry-runs (Tasks 1–2) are agent-runnable.
- **Never touch `F:\Projects\graphite` (main).** No merge, no push, no deploy. Work only on `feat/claude-codex-router` in `F:\tmp\graphite-claude-codex-router` and the harness dir. Quarantined stores are never reactivated.
- **Sanitized receipts only:** digests, counts, tokens, cost, durations, outcome categories, booleans. Never prompts, file contents, diffs, credentials, or raw provider output. `raw_provider_output_persistence: false`.

## Component / File Structure

- Create: `F:\tmp\graphite-live-acceptance-harness\_prepare_openrouter_routed_smoke.py` — pinned pool + reused edit target + bundle.
- Create: `F:\tmp\graphite-live-acceptance-harness\_execute_openrouter_routed_smoke.py` — issue + routed execution + telemetry + receipt.
- Create (scratchpad, not committed): `dryrun_routed_smoke_construct.py` — offline pool/approval constructibility.
- Create (scratchpad, not committed): `dryrun_routed_smoke.py` — offline end-to-end coordinator/approval/telemetry check against a store **copy**, with the edit stubbed.
- Modify: `F:\tmp\graphite-claude-codex-router\docs\superpowers\implementation-notes\2026-07-19-provider-lifecycle-evidence.md` — append routed-smoke evidence.

### Pinned facts (verified live 2026-07-20)

- Store: 12 capability_snapshots / 12 lifecycle_snapshot_bindings / **21** cli_telemetry_events; integrity ok; 0 FK; schema_version 6; `approval_status(<new id>)` is `None` (no prior approval). Hashes: routing `21e73d3f8ac2e9780feaefbfeb1c6513f309d9e32ba75451b5fa9e23ce27ef49`, lifecycle `a99f7cc454e4b8ad000c1d0ba6b203b44cf6d12c3fef1b9c57d428b236bef890`.
- `FIXTURE = F:\tmp\graphite-production-live-fixture`; `FIXTURE_COMMIT = 05f01737326469bd951ec677a0cf73b68caf9fe1`; `WORKTREES = F:\tmp\graphite-production-live-worktrees`.
- Candidate specs are **identical to the registration plan** (`_prepare_openrouter_pool_registration.CANDIDATE_SPECS`): primary k2.7-code (snapshot `500cda19…`, identity `c8cece35…`, model `939d3a5a…`, routing `916df225…`, snapshot_expires_at `1784648044`); fallback k2.6 (snapshot `1b2a7c8e…`, identity `6333c4d5…`, model `e5044008…`, snapshot_expires_at `1784654851`). Candidate digests `da0e80ea…` / `6f6c3b63…` (unchanged; candidates carry no cost).
- Reused edit target (r7/r8): `EDIT_SCOPE = ("src/access.py", "tests/test_access.py")`; `EDIT_SCHEMA_SHA256 a17a4abe333e9315d6f247cedc81b82925b04d7b488f085d1e3caeba5befde17`; `EDIT_PROMPT_SHA256 a57e0b8bf4a0a137b1989aff0d8a366b66ca9a099fec0b510bb6cb53a2ba5dd2`; `MAX_INPUT_TOKENS 65536`; `MAX_OUTPUT_TOKENS 16384`; `MAX_TOTAL_EDIT_BYTES 8192`; `TIMEOUT_SECONDS 180.0`; `ROUTING_POLICY {"allow_fallbacks": False}`; `POLICY_VERSION "1.0.0"`; reference `diff_sha256 005f1ae8…`, `changed_files 2`, `changed_bytes 1007`.
- Cost ceiling: `MAX_COST_MICROUNITS = 128000` (worst-case `64000` ×2).
- Pool envelope (differs from registration only here): `max_cost_microunits 128000`; `max_input_tokens 65536`; `max_output_tokens 16384` (≤ 32768 pool limit; sum 81920 ≤ 262144 context); `max_duration_ms 180000`; `task_risk LOW`; `permission_mode WORKSPACE_WRITE`; `required_capabilities ("code","reasoning","vision")`; `allowed_fallback_reasons ("capacity_unavailable",)`; `max_attempts 2`; `allow_cross_provider False`; `graph_fingerprint 6f163395…`; `repository_commit FIXTURE_COMMIT`; `expires_at 1784648044` (= min snapshot expiry); `issued_at 1784578461`; fresh ids `approval_id "openrouter-routed-smoke-1"`, `task_id "openrouter-routed-smoke"`, `decision_id "openrouter-routed-smoke-decision-1"`, `worktree_id "openrouter-routed-smoke"`, `nonce "openrouter-routed-smoke-nonce-1"`; `policy_version "1.0.0"`; `trust_policy_digest` / `context_manifest_hash` reused from registration.
- Quota: `REPOSITORY_QUOTA_TOKENS = MACHINE_QUOTA_TOKENS = 262144` (≥ reserved 81920).

### Reference: exact API shapes this plan consumes

```python
# graphite.routing.route_pool_execution
execute_approved_route_pool(*, pool, signed_approval, approval_authority,
    authority_loader, runner, evidence_sink, repository_quota_tokens,
    machine_quota_tokens, now) -> RouteExecutionResult
RouteExecutionResult(candidate_id, candidate_digest, attempt_ordinal, output,
    input_tokens, output_tokens, duration_ms, cost_microunits)  # output non-empty, <=1MB
RouteAttemptFailure(evidence: RouteAttemptEvidence)             # raise from runner -> fallback
RouteExecutionEvidence(candidate_id, candidate_digest, attempt_ordinal,
    outcome_category, input_tokens, output_tokens, duration_ms, cost_microunits)  # -> evidence_sink

# graphite.routing.approval.ApprovalAuthority(store, *, key_path, quota_path, now)
#   .issue(pool) -> SignedApproval          # writes approval_records row
#   .consume(signed, pool, *, repository_quota_tokens, machine_quota_tokens)  # marks consumed (called inside coordinator)
# graphite.routing.storage.RepositoryStore.approval_status(approval_id) -> str | None  # "consumed"

# graphite.routing.openrouter_executor
preflight_openrouter(*, api_key, model_id, routing_policy, observed_at,
    policy_version, transport=run_http_probe) -> OpenRouterPreflight  # .runtime(.digest), .pricing
execute_openrouter(*, api_key, prompt, requested_model, expected_effective_model,
    effort, output_schema, output_schema_sha256, pricing, max_output_tokens,
    max_cost_microunits, timeout_seconds, response_format_type, transport=run_http_probe)
    -> OpenRouterExecutionResult  # .message, .effective_model, .input_tokens, .output_tokens,
                                  #   .cost_microunits, .request_sha256, .response_sha256
apply_whole_file_edit(*, workspace, payload, edit_scope, max_total_bytes) -> tuple[str,...]

# graphite.routing.diff_policy.collect_diff_evidence(worktree, *, max_files, max_bytes)
#   -> .diff_sha256, .changed_files, .changed_bytes
# graphite.routing.worktree.create_task_worktree(*, source_root, state_root, task_id, approved_commit) -> .root
# graphite.routing.telemetry.CliTelemetryRecord(...); record_cli_telemetry(store, record) -> bool
# graphite.routing.lifecycle_service.ProviderLifecycleService(lifecycle_store, routing_store).route_authority(boundary, candidate)
```

---

### Task 1: Prepare manifest script (`_prepare_openrouter_routed_smoke.py`)

**Files:**
- Create: `F:\tmp\graphite-live-acceptance-harness\_prepare_openrouter_routed_smoke.py`
- Test (scratchpad): `…\scratchpad\dryrun_routed_smoke_construct.py`

**Interfaces:**
- Consumes: `_prepare_openrouter_pool_registration` (candidate specs + builders + pins), `_prepare_openrouter_edit_smoke` / `_prepare_openrouter_edit_smoke_r2` (edit target), `graphite.routing.route_pool.{ApprovedRouteCandidate, ApprovedRoutePool}`.
- Produces (names the execute imports): `FIXTURE`, `ROUTING_PATH`, `LIFECYCLE_PATH`, `WORKTREES`, `FIXTURE_COMMIT`, `IMPLEMENTATION_COMMIT`, `ROUTING_STORE_SHA256`, `LIFECYCLE_STORE_SHA256`, `EXISTING_STORE_CONTRACT`, `EXPECTED_FINAL_STORE_CONTRACT`, `ISSUED_AT`, `EXPIRES_AT`, `POOL_NOW`, `CANDIDATE_SPECS`, `build_candidate`, `build_pool`, `digest`, `EDIT_PROMPT`, `EDIT_SCHEMA`, `EDIT_SCHEMA_SHA256`, `EDIT_SCOPE`, `MAX_INPUT_TOKENS`, `MAX_OUTPUT_TOKENS`, `MAX_TOTAL_EDIT_BYTES`, `TIMEOUT_SECONDS`, `MAX_COST_MICROUNITS`, `ROUTING_POLICY`, `POLICY_VERSION`, `REFERENCE_DIFF_SHA256`, `EXPECTED_CHANGED_FILES`, `EXPECTED_CHANGED_BYTES`, `REPOSITORY_QUOTA_TOKENS`, `MACHINE_QUOTA_TOKENS`, `APPROVAL_KEY_PATH`, `APPROVAL_QUOTA_PATH`, `BUNDLE`, `BUNDLE_DIGEST`.

- [ ] **Step 1: Write the prepare script**

```python
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import _prepare_openrouter_pool_registration as reg
import _prepare_openrouter_edit_smoke as round_one
import _prepare_openrouter_edit_smoke_r2 as round_two
from graphite.routing.route_pool import ApprovedRouteCandidate, ApprovedRoutePool

FIXTURE = reg.FIXTURE
ROUTING_PATH = reg.ROUTING_PATH
LIFECYCLE_PATH = reg.LIFECYCLE_PATH
WORKTREES = Path(r"F:\tmp\graphite-production-live-worktrees")
FIXTURE_COMMIT = reg.FIXTURE_COMMIT
# No graphite source change; pin the CURRENT clean feature HEAD at run time (Task 3, Step 1).
IMPLEMENTATION_COMMIT = "REPLACE_WITH_CURRENT_HEAD_AT_TASK3_STEP1"
ROUTING_STORE_SHA256 = reg.ROUTING_STORE_SHA256          # 21e73d3f...
LIFECYCLE_STORE_SHA256 = reg.LIFECYCLE_STORE_SHA256      # a99f7cc4...

digest = reg.digest
CANDIDATE_SPECS = reg.CANDIDATE_SPECS                    # byte-identical to registration
build_candidate = reg.build_candidate

ISSUED_AT = 1784578461
EXPIRES_AT = 1784648044                                 # = min(edit snapshot expiry)
# POOL_NOW frozen; safe ONLY because EXPIRES_AT == min snapshot expiry and the execute
# preflight fails closed on time.time() >= EXPIRES_AT (see registration plan note).
POOL_NOW = 1784578461

# Reused edit target (r7/r8), byte-identical.
EDIT_PROMPT = round_two.EDIT_PROMPT
EDIT_SCHEMA = round_one.EDIT_SCHEMA
EDIT_SCHEMA_SHA256 = round_one.EDIT_SCHEMA_SHA256        # a17a4abe...
EDIT_PROMPT_SHA256 = round_two.EDIT_PROMPT_SHA256        # a57e0b8b...
EDIT_SCOPE = round_one.EDIT_SCOPE                        # src/access.py, tests/test_access.py
MAX_INPUT_TOKENS = round_one.MAX_INPUT_TOKENS            # 65536
MAX_OUTPUT_TOKENS = round_one.MAX_OUTPUT_TOKENS          # 16384
MAX_TOTAL_EDIT_BYTES = round_one.MAX_TOTAL_EDIT_BYTES    # 8192
TIMEOUT_SECONDS = round_one.TIMEOUT_SECONDS              # 180.0
ROUTING_POLICY = round_one.ROUTING_POLICY                # {"allow_fallbacks": False}
POLICY_VERSION = round_one.POLICY_VERSION                # "1.0.0"
MAX_COST_MICROUNITS = 128_000                            # worst-case 64000 x2 (r7 ceiling)

REFERENCE_DIFF_SHA256 = "005f1ae8ae072d35b003b3804cb9b3c0dee49058f4e488eddb3cb3031702b93e"
EXPECTED_CHANGED_FILES = 2
EXPECTED_CHANGED_BYTES = 1007

REPOSITORY_QUOTA_TOKENS = 262_144
MACHINE_QUOTA_TOKENS = 262_144
_STATE_DIR = Path(r"F:\tmp\graphite-live-acceptance-harness") / ".routed-smoke-state"
APPROVAL_KEY_PATH = _STATE_DIR / "approval.key"
APPROVAL_QUOTA_PATH = _STATE_DIR / "quota.sqlite3"

EXISTING_STORE_CONTRACT = reg.EXISTING_STORE_CONTRACT    # 12/12/21
EXPECTED_FINAL_STORE_CONTRACT = {
    **EXISTING_STORE_CONTRACT,
    "telemetry_events": 22,                              # +1 routed telemetry
}

POOL_SPEC = {
    "approval_id": "openrouter-routed-smoke-1",
    "task_id": "openrouter-routed-smoke",
    "decision_id": "openrouter-routed-smoke-decision-1",
    "required_capabilities": ("code", "reasoning", "vision"),
    "task_risk": "low",
    "permission_mode": "workspace-write",
    "trust_policy_digest": reg.TRUST_POLICY_DIGEST,
    "graph_fingerprint": reg.FIXTURE_GRAPH,
    "context_manifest_hash": reg.CONTEXT_MANIFEST_HASH,
    "repository_commit": FIXTURE_COMMIT,
    "worktree_id": "openrouter-routed-smoke",
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
    "nonce": "openrouter-routed-smoke-nonce-1",
}


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


BUNDLE = {
    "schema_version": "1",
    "purpose": "graphite_openrouter_routed_smoke",
    "issued_at": ISSUED_AT,
    "expires_at": EXPIRES_AT,
    "implementation_commit": IMPLEMENTATION_COMMIT,
    "fixture_commit": FIXTURE_COMMIT,
    "routing_store_sha256": ROUTING_STORE_SHA256,
    "lifecycle_store_sha256": LIFECYCLE_STORE_SHA256,
    "existing_store_contract": EXISTING_STORE_CONTRACT,
    "expected_final_store_contract": EXPECTED_FINAL_STORE_CONTRACT,
    "task_category": "isolated-code",
    "permission_mode": "workspace-write",
    "pool_now": POOL_NOW,
    "candidates": [
        {"position": i, "candidate_id": s["candidate_id"], "role": s["role"], "slug": s["slug"],
         "capability_snapshot_digest": s["capability_snapshot_digest"],
         "lifecycle_identity_digest": s["lifecycle_identity_digest"]}
        for i, s in enumerate(CANDIDATE_SPECS)
    ],
    "pool_candidate_digest": [build_candidate(s).digest for s in CANDIDATE_SPECS],
    "pool_digest": build_pool(tuple(build_candidate(s) for s in CANDIDATE_SPECS)).digest,
    "edit_target": {
        "reused_from": "r7/r8 tenant-authorization edit",
        "edit_scope": list(EDIT_SCOPE),
        "prompt_contract_hash": EDIT_PROMPT_SHA256,
        "response_contract_hash": EDIT_SCHEMA_SHA256,
        "reference_diff_sha256": REFERENCE_DIFF_SHA256,
        "expected_changed_files": EXPECTED_CHANGED_FILES,
        "expected_changed_bytes": EXPECTED_CHANGED_BYTES,
        "response_format_type": "json_object",
    },
    "maximum_cost_microunits": MAX_COST_MICROUNITS,
    "live_inference": True,
    "network": True,
    "store_write": True,
    "expected_mutation": {
        "approval_records": "0 -> 1 (consumed)",
        "cli_telemetry_events": "21 -> 22",
        "capability_snapshots": "12 (unchanged)",
        "lifecycle_snapshot_bindings": "12 (unchanged)",
    },
    "credential_source": "session_environment:OPENROUTER_API_KEY",
    "credential_in_argv": False,
    "credential_persisted": False,
    "forbidden_persistence": [
        "account_metadata", "credential_material", "diff_content",
        "executable_or_credential_paths", "prompt_body", "provider_diagnostics",
        "repository_source", "response_body", "stderr_body", "stdout_body",
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

- [ ] **Step 2: Run the prepare script**

Run:
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src;F:/tmp/graphite-live-acceptance-harness" python "F:/tmp/graphite-live-acceptance-harness/_prepare_openrouter_routed_smoke.py"
```
Expected: one JSON line with a 64-hex `bundle_digest`, a two-element `pool_candidate_digest` (`da0e80ea…`, `6f6c3b63…`), a `pool_digest`, `live_inference:true`, `store_write:true`, and `expected_final_store_contract.telemetry_events == 22`. No exception (candidates + pool construct).

- [ ] **Step 3: Write + run the constructibility dry-run**

Create scratchpad `dryrun_routed_smoke_construct.py`:
```python
from __future__ import annotations

import _prepare_openrouter_routed_smoke as prep

candidates = tuple(prep.build_candidate(s) for s in prep.CANDIDATE_SPECS)
pool = prep.build_pool(candidates)
assert [c.candidate_id for c in candidates] == [
    "openrouter-kimi27-code-primary", "openrouter-kimi26-fallback"]
assert pool.max_cost_microunits == 128_000
assert pool.max_output_tokens == 16_384 and pool.max_input_tokens == 65_536
assert pool.max_attempts == 2 and pool.allowed_fallback_reasons == ("capacity_unavailable",)
for c in candidates:
    assert set(pool.required_capabilities).issubset(c.capabilities)
    assert c.snapshot_expires_at >= pool.expires_at
assert pool.expires_at > pool.issued_at
assert prep.EXPECTED_FINAL_STORE_CONTRACT["telemetry_events"] == 22
print("DRYRUN_OK: routed pool constructs; pool_digest", pool.digest[:12])
```
Run:
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src;F:/tmp/graphite-live-acceptance-harness" python "…/scratchpad/dryrun_routed_smoke_construct.py"
```
Expected: `DRYRUN_OK: routed pool constructs; pool_digest …`.

- [ ] **Step 4: No repo commit for Task 1**

Harness scripts live outside the graphite repo (as in prior rounds); nothing to commit here. Completion = both offline runs pass.

---

### Task 2: Execute script (`_execute_openrouter_routed_smoke.py`)

**Files:**
- Create: `F:\tmp\graphite-live-acceptance-harness\_execute_openrouter_routed_smoke.py`
- Test (scratchpad): `…\scratchpad\dryrun_routed_smoke.py`

**Interfaces:**
- Consumes: `_prepare_openrouter_routed_smoke` (all Task 1 names); `graphite.routing` modules per the Reference block. Overridable module globals for the dry-run: `FIXTURE`, `ROUTING_PATH`, `LIFECYCLE_PATH`, `WORKTREES`, `EXECUTE_TRANSPORT`, `APPROVAL_KEY_PATH`, `APPROVAL_QUOTA_PATH`, and the injectable `perform_routed_edit`.
- Produces: one sanitized JSON receipt; exit 0 on pass, 1 on any fail-closed category.

- [ ] **Step 1: Write the execute script**

```python
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import _prepare_openrouter_routed_smoke as prepared
from graphite.routing.claude_executor import AdapterError
from graphite.routing.contracts import (
    Effort, EvidenceProvenance, PermissionMode, ProviderId, RiskTier, TaskCategory,
)
from graphite.routing.diff_policy import DiffPolicyError, collect_diff_evidence
from graphite.routing.lifecycle import ProviderLifecycleState
from graphite.routing.lifecycle_service import LifecycleServiceError, ProviderLifecycleService
from graphite.routing.lifecycle_storage import LifecycleStore
from graphite.routing.openrouter_executor import (
    apply_whole_file_edit, execute_openrouter, preflight_openrouter,
)
from graphite.routing.profiles import load_verified_capability_snapshots
from graphite.routing.probe_runner import run_http_probe
from graphite.routing.route_pool import RoutePoolError
from graphite.routing.route_pool_execution import (
    RouteExecutionResult, execute_approved_route_pool,
)
from graphite.routing.approval import ApprovalAuthority, ApprovalError
from graphite.routing.storage import RepositoryStore
from graphite.routing.telemetry import CliTelemetryRecord, record_cli_telemetry
from graphite.routing.worktree import WorktreeError, create_task_worktree

FIXTURE = prepared.FIXTURE
ROUTING_PATH = prepared.ROUTING_PATH
LIFECYCLE_PATH = prepared.LIFECYCLE_PATH
WORKTREES = prepared.WORKTREES
APPROVAL_KEY_PATH = prepared.APPROVAL_KEY_PATH
APPROVAL_QUOTA_PATH = prepared.APPROVAL_QUOTA_PATH
EXECUTE_TRANSPORT = run_http_probe          # dry-run overrides with a fake transport


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


# --- the real edit work; the dry-run monkeypatches this to skip network + git worktree ---
def perform_routed_edit(selection, api_key, spec, run_context):
    """Run one live json_object edit for the selected candidate; return result fields."""
    slug = spec["slug"]
    preflight = preflight_openrouter(
        api_key=api_key, model_id=slug, routing_policy=prepared.ROUTING_POLICY,
        observed_at=int(time.time()), policy_version=prepared.POLICY_VERSION,
    )
    if preflight.runtime.digest != spec["lifecycle_identity_digest"]:
        raise HarnessFailure("routed_identity_changed", candidate_id=spec["candidate_id"])
    worktree = create_task_worktree(
        source_root=FIXTURE, state_root=WORKTREES,
        task_id="openrouter-routed-smoke", approved_commit=prepared.FIXTURE_COMMIT,
    )
    started = time.monotonic()
    result = execute_openrouter(
        api_key=api_key, prompt=prepared.EDIT_PROMPT, requested_model=slug,
        expected_effective_model=slug, effort=Effort.HIGH,
        output_schema=prepared.EDIT_SCHEMA, output_schema_sha256=prepared.EDIT_SCHEMA_SHA256,
        pricing=preflight.pricing, max_output_tokens=prepared.MAX_OUTPUT_TOKENS,
        max_cost_microunits=prepared.MAX_COST_MICROUNITS, timeout_seconds=prepared.TIMEOUT_SECONDS,
        response_format_type="json_object", transport=EXECUTE_TRANSPORT,
    )
    duration_ms = round((time.monotonic() - started) * 1_000)
    payload = json.loads(result.message)
    applied = apply_whole_file_edit(
        workspace=worktree.root, payload=payload,
        edit_scope=tuple(prepared.EDIT_SCOPE), max_total_bytes=prepared.MAX_TOTAL_EDIT_BYTES,
    )
    if set(applied) != set(prepared.EDIT_SCOPE):
        raise HarnessFailure("routed_edit_scope_violation")
    diff = collect_diff_evidence(worktree, max_files=2, max_bytes=prepared.MAX_TOTAL_EDIT_BYTES)
    if (diff.diff_sha256 != prepared.REFERENCE_DIFF_SHA256
            or diff.changed_files != prepared.EXPECTED_CHANGED_FILES):
        raise HarnessFailure("routed_diff_mismatch", diff_sha256=diff.diff_sha256)
    from _execute_live_batch import run_validation  # reuse the r7/r8 deterministic validation
    run_validation(worktree.root, "openrouter-routed-smoke")
    run_context.update({
        "snapshot_digest": spec["capability_snapshot_digest"],
        "requested_model": slug, "effective_model": result.effective_model,
        "diff_sha256": diff.diff_sha256, "changed_files": diff.changed_files,
        "changed_bytes": diff.changed_bytes,
    })
    return {
        "output": diff.diff_sha256, "input_tokens": result.input_tokens,
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
        import os
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
        prepared.APPROVAL_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        approval_authority = ApprovalAuthority(
            routing_store, key_path=APPROVAL_KEY_PATH, quota_path=APPROVAL_QUOTA_PATH,
            now=lambda: int(time.time()),
        )
        try:
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
                fields = perform_routed_edit(selection, api_key, spec, run_context)
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
                return  # only the succeeded routed attempt is persisted as telemetry
            record = CliTelemetryRecord(
                provider=ProviderId.OPENROUTER,
                requested_model=run_context["requested_model"],
                effective_model=run_context["effective_model"],
                effort=Effort.HIGH,
                capability_snapshot_digest=run_context["snapshot_digest"],
                category=TaskCategory.ISOLATED_CODE, risk=RiskTier.LOW,
                latency_ms=evidence.duration_ms,
                input_tokens=evidence.input_tokens, output_tokens=evidence.output_tokens,
                changed_file_count=run_context["changed_files"],
                changed_byte_count=run_context["changed_bytes"],
                validation_outcome="passed", review_defect_classes=(), rework_count=0,
                human_verdict=None, provenance=EvidenceProvenance.MACHINE_VERIFIED,
                observed_at=int(time.time()), diff_sha256=run_context["diff_sha256"],
            )
            if not record_cli_telemetry(routing_store, record):
                raise RoutePoolError("routed_telemetry_persistence_failed")

        action = "routed_execution"
        try:
            result = execute_approved_route_pool(
                pool=pool, signed_approval=signed, approval_authority=approval_authority,
                authority_loader=authority_loader, runner=runner, evidence_sink=evidence_sink,
                repository_quota_tokens=prepared.REPOSITORY_QUOTA_TOKENS,
                machine_quota_tokens=prepared.MACHINE_QUOTA_TOKENS,
                now=lambda: int(time.time()),
            )
        except (ApprovalError, RoutePoolError, WorktreeError) as error:
            raise HarnessFailure(
                getattr(error, "code", "route_execution_failed"),
                runner_failure=runner_failure or None) from None

        if result.candidate_id != pool.candidates[0].candidate_id:
            raise HarnessFailure("routed_wrong_candidate", selected=result.candidate_id)
        if result.output != prepared.REFERENCE_DIFF_SHA256:
            raise HarnessFailure("routed_diff_mismatch", output=result.output)
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
            "diff_sha256": result.output, "reference_match": True,
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

- [ ] **Step 2: Write the offline end-to-end dry-run (store copy; edit stubbed)**

The real edit mechanics (json_object parse + apply + reference diff) are already proven live (r7/r8) and offline (`dryrun_r8.py`). This dry-run isolates the **novel** wiring — issue → consume → select primary → evidence sink → consumed — against a store **copy**, stubbing `perform_routed_edit` so no network and no git worktree are needed.

Create scratchpad `dryrun_routed_smoke.py`:
```python
from __future__ import annotations

import gc, hashlib, shutil, sqlite3, sys
from pathlib import Path

import _execute_openrouter_routed_smoke as ex
import _prepare_openrouter_routed_smoke as prep

LIVE = prep.FIXTURE
WORKDIR = Path(r"F:\tmp\graphite-routed-smoke-dryrun")
RR = Path(".graphite") / "routing" / "events.sqlite3"
LR = Path(".graphite") / "routing" / "provider-lifecycle.sqlite3"


def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()


shutil.rmtree(WORKDIR, ignore_errors=True)
(WORKDIR / RR).parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(LIVE / RR, WORKDIR / RR)
shutil.copy2(LIVE / LR, WORKDIR / LR)

# Point store paths at the COPY; approval key/quota into the copy dir.
ex.FIXTURE = WORKDIR
ex.ROUTING_PATH = WORKDIR / RR
ex.LIFECYCLE_PATH = WORKDIR / LR
ex.APPROVAL_KEY_PATH = WORKDIR / "approval.key"
ex.APPROVAL_QUOTA_PATH = WORKDIR / "quota.sqlite3"
prep.APPROVAL_KEY_PATH = ex.APPROVAL_KEY_PATH
prep.APPROVAL_QUOTA_PATH = ex.APPROVAL_QUOTA_PATH
prep.ROUTING_STORE_SHA256 = sha(WORKDIR / RR)
prep.LIFECYCLE_STORE_SHA256 = sha(WORKDIR / LR)
prep.BUNDLE["routing_store_sha256"] = prep.ROUTING_STORE_SHA256
prep.BUNDLE["lifecycle_store_sha256"] = prep.LIFECYCLE_STORE_SHA256
prep.BUNDLE_DIGEST = prep.digest(prep.BUNDLE)

# Stub the real edit: return the reference fields, no network / no git worktree.
def fake_edit(selection, api_key, spec, run_context):
    run_context.update({
        "snapshot_digest": spec["capability_snapshot_digest"],
        "requested_model": spec["slug"], "effective_model": spec["slug"],
        "diff_sha256": prep.REFERENCE_DIFF_SHA256,
        "changed_files": prep.EXPECTED_CHANGED_FILES,
        "changed_bytes": prep.EXPECTED_CHANGED_BYTES,
    })
    return {"output": prep.REFERENCE_DIFF_SHA256, "input_tokens": 451,
            "output_tokens": 451, "duration_ms": 7547, "cost_microunits": 7547}

ex.perform_routed_edit = fake_edit

sys.argv = ["dryrun", "--approved", prep.BUNDLE_DIGEST]
code = ex.main()
gc.collect()
for path in (WORKDIR / RR, WORKDIR / LR):
    c = sqlite3.connect(path)
    try: c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally: c.close()
# Confirm the copy recorded exactly the intended mutation.
con = sqlite3.connect(WORKDIR / RR)
tel = con.execute("SELECT COUNT(*) FROM cli_telemetry_events").fetchone()[0]
con.close()
assert code == 0, f"execute returned {code}"
assert tel == 22, f"telemetry {tel} != 22"
shutil.rmtree(WORKDIR, ignore_errors=True)
print("DRYRUN_OK: routed coordinator consumed approval + persisted telemetry (21->22) on copy")
```
Run:
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src;F:/tmp/graphite-live-acceptance-harness" python "…/scratchpad/dryrun_routed_smoke.py"
```
Expected: the execute prints `"status":"passed"` with `selected_candidate":"openrouter-kimi27-code-primary"`, `reference_match:true`, `approval_status:"consumed"`, `telemetry_after:22`; then `DRYRUN_OK: …`. A `route_*`/`approval_*` failure here is a clean finding — record it and stop; never weaken a check to force a pass.

- [ ] **Step 3: Run the dry-run** (command above).

- [ ] **Step 4: Suite sanity**

From `F:\tmp\graphite-claude-codex-router`:
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src" python -B -m pytest -q -p no:cacheprovider --basetemp "F:/tmp/graphite-routed-smoke-suite" tests/test_route_pool.py tests/test_routing_openrouter_executor.py tests/test_routing_profiles.py
```
Expected: all pass (no source change).

- [ ] **Step 5: No repo commit for Task 2** (harness scripts live outside the repo).

---

### Task 3: Governed live run (operator-executed)

- [ ] **Step 1: Re-pin the manifest.** Set `IMPLEMENTATION_COMMIT` to the current clean feature HEAD; confirm `git status --porcelain` is empty; re-hash both live stores and confirm they still equal `21e73d3f…` / `a99f7cc4…` (update the pins if any intervening store activity — then the `pool_candidate_drift` check must still pass, re-read live values if not). Confirm `now < 1784648044`. Re-run Task 1 Step 2 and Task 2 Step 3 after any edit.

- [ ] **Step 2: Display the manifest + request approval.** Run the prepare script; present purpose `graphite_openrouter_routed_smoke`, the ordered candidates, `live_inference:true`, `store_write:true`, `expected_mutation` (approval_records 0→1, telemetry 21→22, snapshots/bindings unchanged), the cost ceiling `128000`, and the `BUNDLE_DIGEST`. State plainly: **this is a live inference that mutates the store and spends budget.** Wait for `Approved: graphite_openrouter_routed_smoke bundle <BUNDLE_DIGEST>`.

- [ ] **Step 3: Operator runs the proof.** With `OPENROUTER_API_KEY` in the session env, the operator runs (via `!`):
```bash
OPENROUTER_API_KEY=… PYTHONPATH="F:/tmp/graphite-claude-codex-router/src;F:/tmp/graphite-live-acceptance-harness" python "F:/tmp/graphite-live-acceptance-harness/_execute_openrouter_routed_smoke.py" --approved <BUNDLE_DIGEST>
```
Expected receipt: `status:passed`, `selected_candidate: openrouter-kimi27-code-primary`, `diff_sha256 005f1ae8…`, `reference_match:true`, `approval_status:consumed`, `telemetry_before:21`, `telemetry_after:22`, `snapshots_unchanged:true`, `bindings_unchanged:true`. Capture the JSON line.

- [ ] **Step 4: Post-run confirmation.** Re-hash both stores (they WILL differ now — expected), and confirm via a read-only audit: capability_snapshots 12, lifecycle_snapshot_bindings 12, cli_telemetry_events 22, integrity ok, `approval_status("openrouter-routed-smoke-1") == "consumed"`. Record the new post-run hashes as the baseline for future rounds.

---

### Task 4: Record evidence and commit

**Files:** Modify `docs/superpowers/implementation-notes/2026-07-19-provider-lifecycle-evidence.md`.

- [ ] **Step 1: Append the routed-smoke section** with the sanitized receipt; the routed selection (primary k2.7-code) and reference-diff match; the intended mutation (approval issued→consumed, telemetry 21→22) and the unchanged snapshots/bindings (12/12); the tokens/cost/duration; the pre-run and new post-run store hashes; and the statement that exactly one live inference ran, no promotion occurred, and main was untouched.

- [ ] **Step 2: Commit** — `docs: record OpenRouter live routed edit smoke evidence` (with the `Co-Authored-By` trailer). Confirm the feature worktree is clean. No push/merge.

---

## Self-Review Notes

- **Spec coverage:** goal/flow → Task 2; pool composition + cost ceiling → Task 1; runner + telemetry sink → Task 2 Step 1; store mutation (approval + 21→22) → contracts + audit; error handling → the fail-closed categories; governance → Task 3; evidence → Task 4; success criteria → the receipt assertions.
- **Placeholder scan:** `IMPLEMENTATION_COMMIT` is an explicit run-time pin (Task 3 Step 1); `<BUNDLE_DIGEST>` and the API key are genuinely run-time values. No `TODO`/`TBD`.
- **Type consistency:** `perform_routed_edit`/`runner`/`evidence_sink`/`authority_loader`/`build_pool`/`build_candidate` names consistent across tasks; `RouteExecutionResult`, `CliTelemetryRecord`, `execute_approved_route_pool`, `ApprovalAuthority.issue`, `store.approval_status` kwargs match the Reference block.
- If a live run surfaces a genuine gap (e.g. a `route_*`/`approval_*` code), stop and record it; any minimal `graphite` fix gets its own failing test first (TDD), per the spec.

## Success Criteria

- `execute_approved_route_pool` consumes the signed approval; `select_route` picks the primary against its live ACTIVE authority; the live routed edit yields `diff_sha256 005f1ae8…` (2 files, 1007 bytes) with validation passed, within budget.
- `approval_status("openrouter-routed-smoke-1") == "consumed"`; one routed telemetry row persisted (21→22); capability_snapshots and lifecycle_snapshot_bindings unchanged (12/12).
- Sanitized receipt printed; evidence committed on `feat/claude-codex-router`.

## Out of Scope (deferred)

- Forcing the live capacity-fallback path to k2.6 (non-deterministic; proven selectable offline).
- The read-only review / authorization pool; the three unverified models; branch integration.
