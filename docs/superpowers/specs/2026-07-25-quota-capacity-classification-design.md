# Quota-as-Capacity Classification Design

**Date:** 2026-07-25
**Status:** Implemented — merged to main `9704c1e` 2026-07-25; §9 live acceptance executed and PASSED same day (bundle `71c87c76…`)
**Depends on:** 2026-07-24-patch-carry-forward-design.md (schema v8, merged `8ff6f4c`); route-pool capacity fallback (route_pool.py / route_pool_execution.py)

## 1. Problem

CLI quota/rate-limit exhaustion — the one failure class the ApprovedRoutePool
capacity fallback was designed for — does not classify as
`capacity_unavailable` on the claude/codex executor paths, so the fallback is
denied exactly when it is needed.

Observed live (2026-07-25, codex renewal r2): ChatGPT Codex subscription
quota exhausted. The codex CLI exited 1 with structured JSONL error events on
stdout ("You've hit your usage limit… try again at Jul 29th, 2026 9:40 AM";
`turn.failed` / usage-limit `error` event). The transport produced
`CliProcessFailureDiagnostics.failure_category = "provider_process_failure"`,
because `_classify_nonzero_failure` (process_runner.py) only matches one
exact capacity byte pattern per provider. A 2-candidate route pool
(`allowed_fallback_reasons` hard-pinned to `("capacity_unavailable",)`,
route_pool.py:307) would raise `fallback_reason_denied` on that evidence
(route_pool.py:592).

The gap is two-sided:

- **Nonzero-exit path (fired live):** the stdout bytes holding the quota
  error event exist only inside `run_cli_process`; `CliProcessError` carries
  sanitized diagnostics by construction, never provider output. The adapter
  cannot re-classify after the fact. Classification must happen in the
  transport.
- **Exit-0 path (latent):** both adapters already detect quota in parsed
  error events — codex `_failure_code` (codex_executor.py) and claude
  `_parse_result` (claude_executor.py) — but raise `AdapterError("quota")`,
  a code nothing maps to `capacity_unavailable`. Additionally, the codex
  marker set (`"quota"`, `"rate_limit"`, `"rate limit"`) would have missed
  the observed real event text, which says "usage limit" and contains none
  of those markers.

## 2. Goal

Quota/rate-limit exhaustion classifies as `capacity_unavailable` on every
path that produces route-attempt failure evidence, for both claude-code and
codex, so a 2-candidate pool advances to its fallback candidate. The
sanitization invariant is preserved: no provider output text is persisted,
logged, or attached to any error — only the allowlisted category string
crosses the classification boundary.

## 3. Operator decisions (2026-07-25 design round)

1. **Mechanism: structured event parse.** Quota detection in the transport
   parses stdout lines as JSON and inspects only error-shaped events. No raw
   substring matching over arbitrary bytes.
2. **Scope: both paths.** Fix the nonzero-exit transport classifier AND
   define one canonical AdapterError-code → failure-category mapping for the
   exit-0 path and future route runners, with a shared marker vocabulary.
3. **Semantics: reuse `capacity_unavailable`.** No new failure category. The
   category's operational meaning — no-fault provider outage, no accepted
   output, zero side effects, advancing is safe — covers quota exhaustion.
   The frozen category sets (process_runner.py, route_pool.py,
   route_pool_execution.py) and the route-pool reason pin stay untouched.
4. **Acceptance: store-copy live fallback smoke** during the genuine codex
   quota outage (window closes 2026-07-29 ~09:40) — first live firing of the
   advancement path. Live stores are never touched.

## 4. Design — shared quota marker vocabulary

New public constant in `process_runner.py` (transport is already imported by
both executors; no import cycle):

```python
QUOTA_MARKERS: Final = ("quota", "rate_limit", "rate limit", "usage_limit", "usage limit")
```

Matching is always against **lowercased** text. `"usage_limit"` /
`"usage limit"` are included because the observed real codex quota event
contains neither `"quota"` nor `"rate limit"`.

Consumers:

- `_classify_nonzero_failure` (transport, new structured phase — §5)
- codex `_failure_code` (exit-0 path): its inline marker set is replaced by
  `QUOTA_MARKERS`. Behavior change: the observed usage-limit event now maps
  to `AdapterError("quota")` instead of `AdapterError("unavailable")` on the
  exit-0 path. The auth-marker check that precedes it is unchanged.
- claude `_parse_result` (exit-0 path): **unchanged.** Its subtype markers
  (`"quota"`, `"rate"`, `"limit"`) test a short provider-authored enum-like
  field, are already broader than `QUOTA_MARKERS`, and already match
  usage-limit-shaped subtypes. Unifying a free-text vocabulary onto an enum
  field would change semantics for no gain.

## 5. Design — transport structured classification (nonzero exit)

`_classify_nonzero_failure(provider, stdout, stderr)` in
`process_runner.py` gains a second phase. Phase order:

1. **Exact-line capacity match (existing, unchanged):** stdout and stderr
   lines against `_CAPACITY_DIAGNOSTICS[provider]` → `capacity_unavailable`.
2. **Structured quota detection (new, stdout only** — both CLIs emit their
   event streams on stdout; stderr keeps only the exact-line match**):**
   - Decode stdout as UTF-8 transiently. Undecodable → skip the phase
     (result: `provider_process_failure`).
   - For each line: skip blank lines; `json.loads`; skip lines that fail to
     parse or are not dicts. Malformed lines never abort the scan —
     surrounding valid events are still inspected.
   - **Codex** (`provider is ProviderId.CODEX`): event with `type` in
     `{"error", "turn.failed"}` → serialize the event
     (`json.dumps(event, ensure_ascii=True, separators=(",", ":")).lower()`)
     and test whether any marker occurs as a substring of the serialized
     text — the same serialize-and-match method the exit-0 `_failure_code`
     uses, minus its auth-first precedence: at the transport, an auth-shaped
     error event mentioning a quota marker classifies as capacity.
     Deliberate (recall wins; a false positive only reaches the second
     pre-approved candidate). These event types are provider-authored
     error payloads; model-authored text lives in `item.*` events, which are
     never inspected.
   - **Claude** (`provider is ProviderId.CLAUDE_CODE`): event with
     `type == "result"` that is error-shaped
     (`event.get("subtype") != "success"` or `event.get("is_error") is not
     False`) → test the claude subtype markers (`"quota"`, `"rate"`,
     `"limit"`) against `str(event.get("subtype", "")).lower()` — the same
     check `_parse_result` performs. **Only the subtype field is matched**,
     never the serialized event: claude error results can carry
     model-authored partial output in their `result` field, and model text
     must not be able to trigger classification. Informational
     `rate_limit_event` events are not error-shaped and are never inspected.
   - Any match → `capacity_unavailable`. No match → `provider_process_failure`.

Matching events are accepted at **any position** in the stream, not only the
terminal line (unlike the exit-0 parsers, which enforce protocol shape). The
failure modes are asymmetric: a false negative denies the fallback — the
current bug — while a false positive merely runs the second pre-approved
candidate, which is only reachable when attempt 1 produced no accepted
output and zero side effects. Recall wins.

Bounds: stdout is already capped at `MAX_CLI_OUTPUT_BYTES` (4 MiB) by the
transport; per-line `json.loads` on a failure path needs no further limit.

**Sanitization invariant, amended precisely:** the classifier's docstring
currently claims it operates "without decoding". The new phase decodes
**transiently inside the classifier**: decoded text and parsed events are
local variables that never escape the function, are never logged, and never
appear in diagnostics, errors, receipts, or stores. Only the allowlisted
category string is returned. `CliProcessFailureDiagnostics` fields are
unchanged (hashes only). The docstring is updated to state the retained
invariant: "decodes transiently; retains and returns only an allowlisted
category".

Non-provider commands (e.g. `service.py` validation/git invocations run
through `run_cli_process` under a provider tag) also flow through this
classifier on nonzero exit; their diagnostics are discarded by those callers
today, and the classifier must stay exception-total precisely because such
stdout can be arbitrarily pathological.

## 6. Design — canonical evidence mapping (exit-0 path + future runners)

New public total function in `route_pool.py`, beside `_FAILURE_CATEGORIES`:

```python
def failure_category_for_adapter(
    code: object,
    diagnostics: CliProcessFailureDiagnostics | None = None,
) -> str:
    """Map one adapter failure to an allowlisted route-attempt category."""
    if isinstance(diagnostics, CliProcessFailureDiagnostics):
        return diagnostics.failure_category
    if isinstance(code, str) and code in {"quota", "capacity_unavailable"}:
        return "capacity_unavailable"
    return "provider_process_failure"
```

- **Diagnostics precedence:** when an `AdapterError` carries
  `process_diagnostics` (nonzero-exit path — adapter code is the
  uninformative `"unavailable"`), the transport's classification wins.
- **Code mapping:** `"quota"` (exit-0 quota/rate-limit detection) and
  `"capacity_unavailable"` (exit-0 exact capacity message) both map to
  `capacity_unavailable`; every other code — including auth_required,
  timeout, cancelled, protocol, model_mismatch — maps to
  `provider_process_failure` and can never advance a pool.
- **Total, never raises:** unknown or non-string input degrades to
  `provider_process_failure` (fail-closed: fallback denied). Evidence
  construction must never be blocked by a mapping error.
- Import direction: `route_pool.py` imports `CliProcessFailureDiagnostics`
  from `.process_runner`; `process_runner.py` imports nothing from
  route-pool modules. No cycle.

The adapters keep raising `AdapterError("quota")` — the distinct code stays
useful for diagnostics and receipts; normalization happens only where
`RouteAttemptEvidence` is built. No production `RouteRunner` exists yet
(Branch B posture); this function is the contract future runners and the
acceptance harness use.

## 7. Error handling

All failure modes degrade to `provider_process_failure`, which denies
fallback — the pre-existing fail-closed posture:

| Condition | Result |
|---|---|
| Undecodable stdout | structured phase skipped → `provider_process_failure` |
| Non-JSON / non-dict lines | line skipped, scan continues |
| Error-shaped event without quota markers | `provider_process_failure` |
| Model text containing "rate limit" (codex `item.*`, claude `result` field) | never inspected → `provider_process_failure` |
| Claude informational `rate_limit_event` in a stream | not error-shaped → never inspected |
| Mapping given unknown/invalid code | `provider_process_failure` |

Nothing in this design can *grant* authority or output; misclassification in
the worst case runs one additional operator-pre-approved candidate.

## 8. Testing

Unit tests (worktree, `PYTHONPATH=<worktree>/src`):

- `tests/test_routing_process_runner.py`:
  - codex `{"type":"error","message":"You've hit your usage limit…"}` on
    stdout, exit 1 → `capacity_unavailable` (the observed live shape);
  - codex `turn.failed` with `rate_limit` text → `capacity_unavailable`;
  - codex error event without markers → `provider_process_failure`;
  - marker text inside an `item.completed` agent message →
    `provider_process_failure` (event-type gate);
  - claude error-shaped `result` with quota/rate/limit subtype →
    `capacity_unavailable`;
  - claude `result` with marker text only in the `result` field, generic
    subtype → `provider_process_failure` (subtype-only rule);
  - non-JSON stdout, undecodable stdout → `provider_process_failure`;
  - existing exact-line capacity tests unchanged and passing;
  - quota event on **stderr** only → `provider_process_failure` (stdout-only
    rule).
- `tests/test_routing_codex_executor.py`: exit-0 `turn.failed` usage-limit
  event → `AdapterError("quota")` (marker widening).
- `tests/test_route_pool.py`: `failure_category_for_adapter` — diagnostics
  precedence; `"quota"` → capacity; `"capacity_unavailable"` → capacity;
  `"auth_required"`/`"timeout"`/unknown/non-string → process-failure; and an
  end-to-end `execute_approved_route_pool` case where attempt 1 fails with
  category `capacity_unavailable` derived via the mapping and the pool
  advances.

Known unknown, recorded here deliberately: **claude-code's real quota
failure shape is unobserved.** The claude branch mirrors its own exit-0
parser semantics. When a real claude quota failure is first observed, verify
the subtype actually matches; if quota surfaces only in free text with a
generic subtype, that is a new spec round (do not silently widen matching to
model-reachable fields).

## 9. Acceptance — store-copy live fallback smoke

First live firing of the capacity advancement path, in its natural failure
condition. Window: while ChatGPT Codex quota is exhausted, i.e. **before
2026-07-29 ~09:40**.

Harness pair in `F:\tmp\graphite-live-acceptance-harness`:
`_prepare_capacity_fallback_smoke.py` / `_execute_capacity_fallback_smoke.py`,
following the established bundle pattern (BUNDLE dict → sha256 digest,
`--approved <digest>` gate, store-baseline sha pinning, sanitized receipt,
`--dry-run` mode against store copies with stubbed inference).

Live run flow (all against a **copy** of
`F:\tmp\graphite-production-live-fixture`; the live stores are sha-pinned
before and re-verified untouched after):

1. Copy both stores to a scratch fixture.
2. **Codex authority on the copy only:** mint a capability snapshot with a
   stubbed verifier and activate the codex boundary — explicitly labeled
   TEST-ONLY fabricated authority; legitimate because it exists only inside
   the disposable copy. (Real codex verification is impossible during the
   window — that is the point.)
3. **Claude authority on the copy:** one real claude verification inference →
   mint → activate (claude boundary is ACTIVE via carry; its prior snapshots
   are TTL-expired).
4. Build a 2-candidate pool: codex primary, claude fallback,
   `allow_cross_provider=True`,
   `allowed_fallback_reasons=("capacity_unavailable",)`, `max_attempts=2`,
   signed approval via the harness approval authority.
5. `execute_approved_route_pool` with a harness `RouteRunner` that wraps
   `execute_codex` / `execute_claude` and converts `AdapterError` into
   `RouteAttemptEvidence` via `failure_category_for_adapter`.
6. **Attempt 1 is a real codex exec** → rejected by the real quota outage →
   transport classifies `capacity_unavailable` (new code) → evidence row →
   pool advances. **Attempt 2 is a real claude exec** (trivial bounded
   prompt) → succeeds.

Receipt assertions: attempt-1 evidence `outcome_category ==
"capacity_unavailable"`; attempt-2 `succeeded` with the expected output
marker; exactly 2 evidence rows; live stores byte-identical to the pinned
baselines. If attempt 1 unexpectedly **succeeds** (quota restored early /
credits purchased), the receipt reports `inconclusive_capacity_restored` —
no harm, smoke re-scheduled to a synthetic window or retired.

Live-inference budget: 1 claude verification + 1 claude execution + 1
quota-rejected codex attempt (consumes no quota). Governance: displayed
manifest, operator approval as `Approved: <purpose> bundle <digest>`,
operator runs the execute script via `!` (the permission classifier blocks
Claude Code from live-inference python), no inline keys, receipts sanitized.

**Outcome (2026-07-25): PASSED live** — bundle `71c87c76…`, receipt
`capacity_fallback_smoke_receipt.json` in the harness directory. The real
codex attempt hit the real quota outage (exit 1, 8.57s, JSONL error events on
stdout) → transport classified `capacity_unavailable` → mapping honored
diagnostics precedence → the pool advanced (first live firing of the
advancement path) → the real claude leg succeeded (`claude-sonnet-5`,
expected marker). Evidence trail `["capacity_unavailable", "succeeded"]`;
live stores byte-identical to their pinned baselines. Two operational
findings en route, both fail-closed as designed: codex `exec` requires a
git-repo workspace (a bare directory dies sub-second with empty stdout — the
classifier correctly refused to call that capacity), and pool approval
consumption checks the repository quota against the store's cumulative
`budget_ledger` reservations, so harnesses on stores with history must size
quotas above that total.

## 10. Out of scope

- No CLI wiring of route pools or remote providers into `graphite route`
  (Branch B posture unchanged).
- No new failure categories; frozen sets and the 2-candidate reason pin
  unchanged.
- No retry/backoff or scheduling logic on capacity failures.
- No change to lifecycle/verification flows; codex renewal r3 remains a
  separate resume (post-quota, steps recorded in program memory).
- No persistence-schema changes of any kind (routing store stays at v8).

## 11. Compatibility

- `CliProcessFailureDiagnostics` shape, `_FAILURE_CATEGORIES`, route-pool
  validation, and stored evidence vocabulary are unchanged — no migration.
- Prior evidence rows recorded as `provider_process_failure` for quota
  outages stay as recorded; classification is not retroactive.
- Supersedes nothing: the OpenRouter-era decision to defer the live
  capacity-fallback firing is discharged (not contradicted) by §9.
- Adapter surface: only behavior change is codex exit-0 usage-limit events
  raising `AdapterError("quota")` instead of `"unavailable"` (§4) — a
  strictly more precise code on a path that previously produced an opaque
  one.
