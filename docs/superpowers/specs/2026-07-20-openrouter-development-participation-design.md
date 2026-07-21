# OpenRouter development participation design — 2026-07-20

## Purpose and authority change

The operator has revoked the earlier scope statement that OpenRouter is
reserved for application inference. Selected OpenRouter models become a third
governed development provider alongside the Claude Code and Codex CLIs, with
full parity: read-only review/verification roles and bounded edit authority.
The governance regime is unchanged: no execution without a displayed manifest
and explicit approval; capability snapshots only from approved live
verification; write authority only as a promoted workspace-write snapshot
earned through a passing edit smoke; sanitized persistence only. The evidence
docs' old scope wording is updated as part of this work.

Requested initial candidates (existence not assumed; the catalog probe
decides): `moonshotai/kimi-k3`, `moonshotai/kimi-k2.7-code`,
`moonshotai/kimi-k2.6`, `z-ai/glm-5.2`, `meta/muse-spark-1.1`.

## Operator decisions

- Role: full edit parity now (read-only review plus bounded edit authority).
- Rollout: one non-inference catalog probe batch for all five slugs, then
  one verification bundle covering every slug that exists.
- Routing: verified models join the governed route pool as ordered
  candidates per category; every execution still requires manifest approval.
- Cost: hard per-call cost ceiling. Pricing is captured at probe time,
  worst-case cost is displayed in every manifest, over-cost fails closed,
  and pricing drift invalidates the manifest before invocation.
- Edit delivery: whole-file replacement (approach A). Unified-diff and
  agent-loop modes are explicitly deferred.

## Architecture

### openrouter_executor (new module)

Mirrors the Claude/Codex adapter contracts.

- `preflight_openrouter` wraps the existing `observe_openrouter` probe
  (auth health, exact-slug catalog membership, model and routing-policy
  digests) and additionally captures the model's pricing
  (prompt/completion per-token microunits) into the identity evidence as a
  pricing digest.
- `execute_openrouter` performs exactly one bounded
  `POST https://openrouter.ai/api/v1/chat/completions` with:
  - the pinned-address connection discipline reused from `probe_runner`
    (DNS pinning, peer revalidation before credential injection, no
    redirects, response and header caps, JSON content validation);
  - `response_format` json_schema bound to the action's schema by SHA-256,
    temperature 0, one attempt, no retry/fallback/substitution;
  - fail-closed parsing of `usage` (prompt and completion tokens required,
    bounded, integer);
  - reported-cost computation from returned usage times the pinned pricing,
    compared against the manifest's `maximum_cost_microunits`.
- The API key enters only via the explicit session environment, never argv
  or persisted evidence; a missing key fails closed before any request.

### Whole-file edit engine (inside the executor module)

Edit mode binds a response schema requiring the complete new content of
every file in the approved edit scope:
`{"result": "GRAPHITE_EDIT_OK", "files": [{"path": ..., "content": ...}]}`.

Validation completes fully before the first write:

- the `files[].path` set must exactly equal the approved `edit_scope`
  allowlist (no extras, no omissions);
- paths must be relative, contain no traversal or drive/absolute
  components, and resolve inside the isolated worktree without crossing
  symlinks or Windows reparse points;
- per-file and aggregate byte caps are enforced on encoded content.

Only after full validation are files written atomically (temp file plus
rename) inside the isolated worktree. A failed edit therefore leaves the
worktree byte-identical — stronger than the CLI providers, whose agents
write incrementally. Downstream evidence machinery is reused unchanged:
`collect_diff_evidence`, expected-diff comparison, deterministic validation
(pytest and `git diff --check`), and atomic promotion via
`verify_and_save_approved_edit_profile`.

### Identity and profiles

- `RequestedProfile` gains `ProviderId.OPENROUTER` with evidence host
  `openrouter.ai` allowlisted.
- Capability snapshots for OpenRouter bind an endpoint runtime identity:
  provider, canonical endpoint, model-identity digest, routing-policy
  digest, API contract version, and pricing digest — in place of a CLI
  executable hash. The lifecycle store already models OpenRouter
  identities; bindings reuse it.
- Model/pricing/routing-policy digest drift between probe and execution
  invalidates the run before the model is invoked.

### Route pool

Verified models join the governed pool as ordered candidates per category:
read-only review/authorization with a read-only snapshot; isolated code
edits once write-promoted. The pool, coordinator, and loaders already
support OpenRouter identities; this adds configuration and loader support,
not new selection mechanism. Cross-provider review pairs an OpenRouter
primary with a CLI reviewer and vice versa; the reviewer is always a
different provider than the diff author.

## Data flow

1. Catalog probe batch (one approved manifest, non-inference): one
   `observe_openrouter` pass per slug; absent slugs fail closed as
   `probe_model_unavailable` and simply do not advance.
2. Verification bundle (one approved bundle, one attempt per surviving
   slug, independent across models): schema-bound read-only verification
   with token budgets and cost ceiling; a pass creates a read-only
   snapshot, lifecycle binding, and two telemetry events.
3. Edit smoke (separately approved per model): isolated fresh worktree at
   the fixture baseline, whole-file schema bound by hash, pinned expected
   diff, budgets and cost ceiling enforced before the terminal check, then
   the unchanged diff-policy/validation/promotion pipeline.
4. Cross-provider review: a write-promoted OpenRouter model's diff is
   reviewed by a different provider; OpenRouter models become eligible
   reviewers of CLI-authored diffs using the proven schema-validated
   review contract.

## Error handling (fail-closed, sanitized)

- missing API key → `credential_missing`, before any request;
- endpoint, model, pricing, or routing-policy digest drift →
  `identity_drift`-class failure before invocation;
- HTTP failure, non-JSON, or oversized body → provider-process-failure
  class with status class and body hash only;
- missing or schema-violating structured response →
  `response_contract_invalid`;
- scope extras/omissions, traversal, symlink/reparse, or byte-cap breach →
  `edit_scope_violation` with zero files written;
- over token budget or over cost ceiling → `edit_budget_exceeded` /
  `cost_ceiling_exceeded` with sanitized usage receipt;
- diff mismatch after write → existing `edit_diff_mismatch`;
- promotion or persistence failure → existing transactional rollback.

Raw responses, file contents, prompts, credentials, and endpoint details
beyond the canonical root are never persisted or printed; receipts carry
hashes, counts, durations, usage, and cost only.

## Testing

- Executor contract: exact request construction, schema binding, usage and
  cost parsing, credential absence, drift refusal, boundary and overflow
  cost arithmetic — deterministic fake transports only.
- Edit engine: hostile-path battery (traversal, absolute and
  drive-relative paths, symlinks, reparse points), scope extras/omissions,
  byte caps, atomicity under validation failure and simulated mid-set write
  failure, byte-exact content preservation across line-ending styles.
- Profiles/snapshots/pool: evidence-host enforcement, endpoint-identity
  digests, lifecycle binding, promotion parity, pool loading per category.
- Regression: full routing selection, complete offline suite, Ruff,
  `git diff --check`, canonical graph freshness, and byte-unchanged
  Claude/Codex argv contracts.

## Live acceptance order

Each step has its own displayed manifest and explicit approval:

1. catalog probe batch for all five slugs (non-inference), reporting
   existence and pricing;
2. verification bundle for surviving slugs (read-only snapshots);
3. first edit smoke on one model (suggested: `moonshotai/kimi-k2.7-code`),
   cross-provider reviewed by a CLI provider;
4. remaining edit smokes as elected, then a pool-registration manifest
   listing the exact ordered candidates per category.

## Success criteria

- At least one OpenRouter model holds a promoted workspace-write snapshot
  earned through the exact-diff smoke.
- Verified models are loadable pool candidates for their categories.
- Every failure path in the matrix has a deterministic test.
- Evidence docs updated, including revocation of the old scope statement.

## Out of scope

Unified-diff and agent-loop edit modes; OpenRouter overlay/application
inference (already exists); any change to Claude/Codex adapter contracts;
merge, push, or deployment.
