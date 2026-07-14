# Router Model Pool Hardening Design

**Date:** 2026-07-14
**Status:** Implemented (offline acceptance passed; live smoke pending explicit approval)
**Scope:** Ollama Cloud development-routing profiles and deterministic selection

## 1. Problem

The first live-smoke preflight selected `glm-5:cloud` on 2026-07-14 even though
its verified retirement date was 2026-07-15. The selection was deterministic but
operationally unsound: equal candidate scores fell back to lexical model order,
which ignored model specialization and lifecycle runway.

Graphite must never route a new call to a retired or expiring profile. It also must
not authorize every model returned by Ollama inventory discovery. Model admission
remains an explicit, versioned allowlist decision supported by official provider
evidence and local identity verification.

## 2. Goals

- Remove `glm-5:cloud` and all profiles with an active retirement date.
- Ship a conservative four-model Ollama Cloud development pool.
- Prefer the lowest usage tier that satisfies task capability, context, and risk.
- Use deterministic capability-aware ranking instead of lexical tie-breaking alone.
- Keep every execution approval-, digest-, effort-, context-, and quota-bound.
- Require benchmark evidence before learned statistics can promote a new profile.
- Complete the previously blocked synthetic live-smoke test with the selected model.

## 3. Non-goals

- No Grok or Llama profile: no suitable official Ollama Cloud identity was verified.
- No Qwen profile.
- No `deepseek-v4-flash:cloud` profile while it remains a preview.
- No `gemma4:31b-cloud` profile until Graphite benchmark evidence supports admission.
- No automatic model pull, catalog-wide discovery, mutable alias, or community model.
- No autonomous execution or weakening of the permanent high-risk approval gate.
- No model-specific effort beyond `default` until the exact request payload is tested.

## 4. Approved Model Pool

| Exact identifier | Role | Usage class | Minimum context | Initial status |
|---|---|---:|---:|---|
| `kimi-k2.7-code:cloud` | Primary routine and complex coding | provider-reported high | 256K | provisional |
| `minimax-m2.7:cloud` | Coding and agentic alternative | provider-reported medium | 200K | provisional |
| `nemotron-3-super:cloud` | Reasoning, review, independent alternative | provider-reported medium | 256K | provisional |
| `minimax-m3:cloud` | Long-context escalation for eligible tasks | provider-reported high | 512K | provisional |

Provider usage classes are coarse routing metadata, not USD prices. Graphite must
not claim cost savings until captured result records establish them.

Official evidence:

- Kimi K2.7 Code: <https://ollama.com/library/kimi-k2.7-code>
- MiniMax M2.7: <https://ollama.com/library/minimax-m2.7:cloud>
- Nemotron 3 Super: <https://ollama.com/library/nemotron-3-super:cloud>
- MiniMax M3: <https://ollama.com/library/minimax-m3:cloud>

The implementation must also verify every exact identifier and digest through the
bounded loopback `/api/tags` inventory before it becomes execution-eligible.

## 5. Registry and Lifecycle Rules

`BUNDLED_PROFILES` remains the sole authority allowlist. Inventory presence proves
availability, not trust. Each profile records exact identifier, profile version,
capabilities, minimum verified context, usage class, evidence URL/access date, and
optional lifecycle data.

Profiles with `retirement_date` are not shipped in the active pool. As defense in
depth, policy rejects any profile whose retirement date is present, already passed,
or falls inside a fixed 30-day minimum runway. Malformed lifecycle metadata fails
closed. Removing a profile never causes automatic fallback outside the allowlist.

## 6. Deterministic Routing

Hard gates run before scoring: graph validity/freshness, registry freshness, exact
inventory identity, capability, risk, context, effort support, data policy, quota,
and lifecycle runway.

Eligible candidates receive fixed typed metadata:

- Usage class: `medium` is preferred to `high` when capability and expected quality
  are otherwise sufficient.
- Role fit: code-specialized, reasoning/review, and long-context roles
  are matched to deterministic task categories.
- Evidence: repository-local admissible evidence may adjust reliability estimates,
  but cannot add a model, lower risk, change lifecycle gates, or grant authority.

Selection priorities are:

1. Satisfy every hard gate.
2. Prefer verified role fit.
3. Prefer repository evidence when statistically admissible.
4. Prefer the lower usage class when expected quality is otherwise equivalent.
5. Use exact model identifier only as the final stable tie-break.

Expected initial behavior:

- Low/medium coding: Kimi K2.7 Code by role fit; MiniMax M2.7 is the alternative.
- Review/reasoning: Nemotron 3 Super is eligible as an independent alternative.
- Eligible medium-risk work whose context exceeds the smaller profiles: MiniMax M3
  may be recommended. Architecture remains high-risk and returns manual frontier
  handoff while every pool profile is provisional. Future validated profiles still
  cannot bypass the permanent high-risk approval gate.

## 7. Execution and Failure Handling

The existing executor remains unchanged in authority: canonical loopback only,
exact digest revalidation, one request, no redirects/retries/fallbacks/pulls, and no
ambient credentials. If the selected model disappears or changes digest after
approval, execution stops with a sanitized error. Graphite does not silently switch
to another model because approval is bound to the exact selection.

The service preserves model text only in an ephemeral execution result for the
immediate approved interactive caller so a human can evaluate it. Receipt
serialization, JSON/status output, SQLite evidence, logs, and aggregate telemetry
must never contain that text.

When no active profile is eligible, Graphite returns the existing manual
Claude/Codex handoff. Provider or registry failures never relax gates.

## 8. Testing and Acceptance

Tests must prove:

- `glm-5:cloud` and all expiring profiles are absent or lifecycle-ineligible.
- The four exact profiles serialize, cache, and revalidate correctly.
- Unknown inventory entries never become candidates.
- Kimi K2.7 Code wins the approved low-risk synthetic coding task.
- MiniMax M2.7 and Nemotron remain deterministic alternatives.
- MiniMax M3 is reserved for long-context escalation on otherwise eligible tasks.
- Usage class affects only ranking, never authority or risk.
- Unsupported effort and changed digests still fail closed.
- Existing no-network recommendation, approval, executor, storage, CLI, and security
  regression suites remain green.

After offline acceptance, rebuild the synthetic fixture and display its outbound
manifest, selected exact model, effort, estimated tokens, and maximum reservation.
Only an explicit interactive approval may authorize the single live smoke call.
Record receipt metadata and a human verdict without persisting prompt or response
text. If the provider is unavailable, record an external-readiness limitation rather
than weakening offline acceptance.

## 9. Rollback

Registry and policy changes are isolated and versioned. Rollback restores the prior
profile/policy commit but must not re-enable a profile whose retirement date has
passed. Repository evidence remains append-only; no migration may rewrite historical
executions or outcomes.

## 10. Acceptance Status

Offline acceptance passed on 2026-07-14 with feature source commit
`d88068006da8d6e4677ec4208c3c00a6c998c3aa` explicitly pinned for both the focused
and full suites. Acceptance evidence was first recorded separately by commit
`13c23f26ab077b254a1c6ab6fb9c86c94f285c63`. The exact focused command included
the registry, policy, service, executor, CLI, CLI recovery, approval, security,
telemetry, shadow, storage, and documentation suites; it passed 279 tests with one
expected Windows-only POSIX permission skip. The full suite passed with 1,369 tests
and 44 platform/optional-tooling skips; no routing security or approval-authority
test was skipped. Ruff and `git diff --check` passed. The rebuilt repository graph
is fresh (133 source files) and validates with 5,506 nodes, 11,918 edges, zero errors,
and zero warnings.

The isolated fixture passed its unit test and recommends
`kimi-k2.7-code:cloud` at `default` effort and low risk. Its current approval
preflight is recorded in the implementation plan. No provider chat/inference call
was made. The controlled preflight used only the recommendation/preparation path,
which has no approval issuance or consumption operation, and artifact inspection
found no contrary evidence. A later read-only reviewer could not query the routing
database because of its ACL, so this record does not claim independent database-row
verification. The original adaptive-router live smoke remains pending explicit
approval.
