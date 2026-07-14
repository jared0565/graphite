# Adaptive Development Router Design

**Date:** 2026-07-14
**Status:** Approved design
**Initial release:** Approval-gated, Ollama Cloud development routing

## 1. Purpose

Graphite will add a local-first development-routing subsystem that recommends an Ollama Cloud model and supported effort configuration from validated repository evidence. After showing the recommendation, alternatives, confidence, expected quota or cost, and outbound context, Graphite may invoke one selected model only after explicit approval.

The first release establishes the evidence and safety foundation for possible future autonomy. It records recommendations, overrides, executions, verification outcomes, repairs, escalations, and reversions. Recommendation quality may improve from this evidence, but accumulated usage cannot silently grant execution authority.

## 2. Goals

- Reduce completed-task inference cost without lowering verified code quality.
- Improve development speed by selecting context, model, and effort appropriate to the task.
- Make every recommendation explainable, versioned, auditable, and reversible.
- Learn repository-specific routing performance while preventing cross-repository content leakage.
- Support explicitly consented, budgeted shadow evaluation.
- Preserve direct human control over model invocation.
- Produce evidence that can support a separately designed autonomous execution phase.
- Use the Realty application benchmark to calibrate speed, quality, robustness, and token economics.

## 3. Scope

### 3.1 Included

- Development-time model and effort recommendations.
- Approval-gated invocation of allowlisted Ollama Cloud models through the loopback Ollama API.
- Deterministic task, complexity, impact, and risk classification.
- Bounded repository context construction with an outbound-data manifest.
- Repository-local detailed telemetry.
- Explicitly opted-in, sanitized machine-wide aggregate learning.
- Budgeted shadow comparisons for eligible low- and medium-risk tasks.
- Manual Claude Code or Codex handoff recommendations when Ollama confidence is insufficient.
- Operator-controlled policy promotion and rollback.

### 3.2 Excluded

- OpenRouter for development routing. OpenRouter is reserved for a separate production in-application inference gateway.
- Direct invocation of Claude Code, Codex CLI, Codex App, or ChatGPT.
- Autonomous code mutation or arbitrary shell execution.
- Automatic package, model, or tool installation.
- Automatic Ollama model pulls.
- Production application inference.
- Automatic promotion to autonomous execution.
- High-risk autonomous execution, regardless of accumulated evidence.
- Model-weight training or fine-tuning.

## 4. Architectural principles

1. **Deterministic core first.** Graph construction, validation, context selection, classification, and policy constraints remain deterministic and inspectable.
2. **Separate recommendation from authority.** The routing policy cannot execute a model. A valid, single-use approval manifest grants bounded authority to the executor.
3. **Fail closed.** Invalid evidence, unsupported capabilities, unavailable telemetry, exhausted budgets, expired approval, or ambiguous model identity prevents execution.
4. **Treat model output as untrusted.** Model text cannot approve itself, change policy, bypass verification, or modify files.
5. **Keep trust boundaries explicit.** Repository evidence, Ollama, model responses, telemetry, machine-wide aggregates, and human verdicts are separate trust domains.
6. **Optimize completed-task value.** Per-token price alone is insufficient; routing accounts for likely success, retries, escalation, latency, verification, and data exposure.
7. **Preserve operator control.** Learning may adjust recommendation statistics, but authority changes require explicit promotion.

## 5. System architecture

```text
Task request
  -> fresh, validated graph
  -> bounded context selection
  -> task, impact, and risk classification
  -> eligible Ollama model/effort combinations
  -> ranked routing recommendation
  -> outbound-data and budget preview
  -> explicit single-use approval
  -> bounded primary Ollama Cloud execution
  -> optional consented shadow comparison
  -> external verification evidence
  -> repository-local outcome record
  -> optional sanitized aggregate update
  -> promotion eligibility calculation
```

The routing subsystem is a new `src/graphite/routing/` package. Existing deterministic graph construction and `llm.py` report enrichment remain separate. The router consumes validated Graphite evidence but cannot alter graph facts.

Before the router becomes an execution dependency, Graphite must add engine-version-aware freshness and bounded, validated reads for direct query, context, impact, and MCP graph consumers.

## 6. Components

### 6.1 Contracts

`contracts.py` defines immutable, typed records:

- `TaskRequest`: objective, repository root, target paths, budget, requested data policy, and optional category hint.
- `TaskProfile`: category, complexity, impact radius, risk flags, context requirements, and verification requirements.
- `ModelProfile`: exact model identity, availability, capabilities, context bounds, supported effort mapping, and evaluation status.
- `RoutingDecision`: selected configuration, ranked alternatives, scores, confidence, reasons, policy version, and evidence version.
- `ApprovalManifest`: exact model, effort, outbound context inventory, request limits, budget, issue time, expiry, and single-use nonce.
- `ExecutionReceipt`: actual model identity, effort, usage, latency, status, response classification, and sanitized failure category.
- `VerifiedOutcome`: verification provenance, build/test/security results, human verdict, repairs, escalations, and later reversion state.

### 6.2 Classifier

`classifier.py` derives deterministic features from validated repository evidence. Features include affected file count, reverse dependencies, community crossings, language, file types, change category, test proximity, and risk signals for authentication, authorization, tenant isolation, migrations, deployment, secrets, infrastructure, concurrency, and financial or legal decision paths.

User-provided category hints cannot lower the computed risk tier.

### 6.3 Context builder

`context_builder.py` selects a bounded dependency neighborhood and produces an inspectable outbound-data manifest. It excludes environment files, credential material, keys, generated artifacts, configured sensitive paths, and paths outside the selected repository.

Secret detection is defense in depth rather than a completeness guarantee. Explicit approval after context construction remains mandatory.

### 6.4 Model registry and effort mapping

`registry.py` inventories allowlisted Ollama Cloud models and their evaluated capabilities. Model identities are exact and immutable for a decision. Availability is rechecked before execution.

`effort.py` exposes normalized routing levels only when a model has a tested mapping to supported provider controls. Unsupported effort configurations are ineligible; Graphite never fabricates a provider parameter. A model at maximum effort remains subject to response, time, quota, and context limits.

### 6.5 Policy engine

`policy.py` first applies hard eligibility gates, then ranks eligible configurations with expected completed-task utility:

```text
utility =
  calibrated_success_probability * quality_value
  - expected_usage_cost
  - latency_penalty
  - retry_and_escalation_risk
  - uncertainty_penalty
  - data_exposure_penalty
```

Ollama subscription usage uses configurable quota or shadow-cost estimates when precise USD usage is unavailable. Repository-specific evidence takes precedence over sanitized machine-wide priors.

### 6.6 Approval controller

`approval.py` displays the selected configuration, alternatives, confidence, reasons, outbound context, estimated usage, shadow eligibility, and required verification. Approval produces a short-lived, single-use manifest bound to the evidence, model, effort, context, and budget.

Any material change invalidates approval.

### 6.7 Ollama executor

`ollama_executor.py` communicates only with the loopback Ollama API. It rejects arbitrary remote base URLs, non-allowlisted models, unsupported effort controls, expired approval, changed evidence, and exceeded budgets.

Calls use fixed request shapes, bounded input and output, monotonic deadlines, concurrency limits, closed stdin where applicable, sanitized errors, and no hidden retries. The executor has no shell, package-management, model-pull, or filesystem-mutation authority.

### 6.8 Shadow evaluator

`shadow.py` samples a configurable 5-10% of eligible low- and medium-risk tasks only after explicit consent and budget approval. It cannot select high-risk tasks.

In the first release, shadow output is evaluated through controlled benchmark fixtures or human pairwise review. It is not classified as production-verified code because Graphite does not apply competing patches. Executable counterfactual testing in isolated worktrees requires a later design.

### 6.9 Telemetry and learning

`telemetry.py` records detailed evidence locally and, when explicitly enabled, emits sanitized aggregate observations to a machine-wide store. `promotion.py` calculates confidence and eligibility but cannot change execution authority.

The active recommender may update calibrated success estimates from admissible evidence while retaining a stable policy definition. Each decision records the exact statistics and policy version it used. Candidate policy changes are evaluated in shadow mode and require operator promotion.

## 7. CLI surface

```text
graphite route recommend
graphite route run
graphite route record-outcome
graphite route status
graphite route policy
```

- `recommend` is read-only and performs no network request.
- `run` requires a fresh approval manifest and performs one bounded primary call plus an independently approved shadow call when selected.
- `record-outcome` imports explicit verification evidence or a human verdict with provenance.
- `status` reports budgets, confidence, evidence counts, shadow activity, and promotion eligibility without exposing repository content.
- `policy` displays, promotes, or rolls back recommendation policy versions; authority promotion remains a separate future operation.

## 8. Persistence and isolation

The implementation uses Python's standard-library SQLite support.

- Repository detail: `.graphite/routing/events.sqlite3`
- Machine aggregate: platform-local Graphite state directory

Both are excluded from source control. Databases use schema-versioned migrations, transactions, WAL mode, bounded lock waits, and idempotency keys. Permissions are restricted to the current user where supported.

Raw prompts, source content, credentials, and complete model responses are not stored by default. Optional response retention is separate, repository-local, explicitly enabled, retention-bounded, and never aggregated.

Machine-wide aggregates may contain only anonymous task category, risk tier, model and effort identity, bounded complexity buckets, usage and latency buckets, verification category and outcome, approval or override state, escalation state, and policy/evaluator versions.

Aggregates may not contain repository names, paths, symbols, prompts, responses, user identities, credentials, or stable repository fingerprints. Deletion and retention changes reduce confidence rather than preserving unsupported derived certainty.

## 9. Learning and evidence policy

Admissible evidence has explicit provenance:

1. Machine-verified build, test, lint, type-check, security, or benchmark evidence.
2. CI-imported evidence with validated source and task correlation.
3. Human acceptance, rejection, or pairwise preference.
4. Later reversion or defect evidence correlated to the task.

Machine verification has greater weight than self-reported human outcomes. Missing or ambiguous provenance is retained for audit but excluded from promotion statistics.

Learning cannot lower a deterministic risk classification, remove approval, expand outbound data, add a provider, install a model, change an allowlist, or promote autonomy.

## 10. Manual frontier handoff

When no Ollama configuration meets confidence, risk, capability, context, or budget requirements, Graphite recommends a manual Claude Code or Codex handoff. The recommendation includes the reason and required verification but does not invoke either CLI.

If the operator later records the handoff result, Graphite may use admissible outcome metadata for comparative evaluation. It does not store subscription credentials or assume access to a specific frontier model.

## 11. Failure behaviour

- Missing, stale, invalid, oversized, or unreadable graph evidence prevents execution.
- No eligible Ollama model produces a manual handoff recommendation.
- Missing, expired, reused, or mismatched approval prevents execution.
- Exhausted repository, machine, or shadow budget prevents the affected call.
- Unsupported effort or changed model identity prevents execution.
- Timeout, cancellation, malformed response, oversized response, or provider failure produces a sanitized receipt and requires an explicit retry decision.
- Telemetry unavailability prevents execution because unaudited calls are not permitted.
- Persistence corruption is surfaced as a stable failure category; Graphite does not silently reset evidence.
- Partial output remains untrusted and is not treated as a successful outcome.

## 12. Security controls

- Loopback-only Ollama transport in the initial release.
- Exact allowlisted Cloud model identities.
- Default-deny sensitive path handling and explicit outbound manifests.
- No secrets in structured logs or machine-wide aggregates.
- Fixed request schemas, response caps, timeouts, concurrency limits, and budget limits.
- Single-use, expiring approval bound to immutable evidence.
- Strict repository containment and normalized relative paths.
- No shell, arbitrary tools, package installation, model installation, or code mutation.
- Structured, sanitized audit events for recommendation, approval, execution, shadow selection, verification, escalation, and promotion eligibility.
- Repository-local evidence cannot influence another repository except through explicitly opted-in sanitized aggregates.

These controls reduce and contain risk; they are not a claim that Graphite, Ollama, the model provider, or the host is intrusion-proof.

## 13. Testing strategy

### 13.1 Unit and property-oriented tests

- Classification and non-lowerable risk rules.
- Model eligibility and effort mapping.
- Deterministic scoring and tie breaking.
- Budget, quota, confidence, and promotion calculations.
- Contract serialization and schema migrations.
- Aggregate sanitization and retention.

### 13.2 Contract and integration tests

- Bounded fake Ollama server for success and failure cases.
- Approval issuance, expiry, reuse, mismatch, and cancellation.
- Idempotent receipts and duplicate submission handling.
- SQLite concurrency, migration, corruption, and bounded lock behaviour.
- Explicit outcome import with provenance.

### 13.3 Adversarial tests

- Traversal, symlinks, selected-root crossings, and unsafe paths.
- Sensitive files, encoded secrets, misleading extensions, and oversized context.
- Redirects, arbitrary base URLs, malformed JSON, oversized responses, and remote error bodies.
- Credential and absolute-path redaction.
- Budget races, concurrent calls, stale approval, and model replacement.

### 13.4 End-to-end and benchmark tests

- Recommendation-only flows make no network request.
- Approved execution cannot mutate source or run commands.
- Shadow selection respects consent, task risk, rate, and budget.
- Identical evidence and policy versions produce identical recommendations.
- Real Ollama Cloud tests are explicit, opt-in, quota-bounded, and excluded from ordinary CI.
- The Realty benchmark measures delivery time, accepted quality, robustness, tokens, cost equivalent, latency, repairs, and reversions across frontier-only, native automatic, and Graphite-routed policies.

## 14. Acceptance criteria

- Existing Graphite tests, lint, and validation remain green.
- No model call occurs without valid single-use approval.
- No arbitrary command execution, package/model installation, or source mutation is possible through the router.
- High-risk tasks always retain approval and manual frontier handoff.
- Every call has correlated decision, approval, and execution audit records.
- Shadow calls cannot exceed configured sampling or budget limits.
- Machine-wide aggregates contain no repository-identifying or source-derived strings.
- Provider failures cannot expose response bodies, credentials, repository content, or absolute host paths.
- With an already loaded, validated graph, recommendation-only routing completes within a p95 of two seconds on the documented Realty benchmark reference machine; graph scanning, graph building, and provider latency are measured separately.
- Learning results are reproducible from retained admissible evidence and versioned policy inputs.

## 15. Future autonomy eligibility

Eligibility is calculated independently by task class. Confidence uses the lower bound of a two-sided 95% Wilson score interval over admissible machine-verified outcomes:

- Low risk: at least 50 machine-verified outcomes, a 95% confidence lower bound of at least 90%, and no severe failure.
- Medium risk: at least 100 machine-verified outcomes, a 95% confidence lower bound of at least 95%, and no severe failure.
- High risk: never eligible under this design.

A severe failure is any outcome involving unauthorized access, tenant-boundary violation, credential or sensitive-data disclosure, data loss or corruption, unrecoverable migration or deployment failure, or a critical/high-severity security finding attributable to the routed result. One severe failure resets eligibility for that task class until an operator completes incident review and explicitly starts a new evidence window.

Meeting a threshold does not grant authority. It only makes the task class eligible for explicit operator promotion under a future autonomous-execution design.

## 16. Rollout

1. Add engine-version-aware freshness and bounded validated graph reads.
2. Add routing contracts, registry, classifier, effort mapping, and recommendation-only CLI.
3. Add outbound context manifests and approval-gated Ollama execution.
4. Add repository-local telemetry and outcome import.
5. Add opt-in sanitized aggregate learning.
6. Add budgeted shadow evaluation.
7. Calibrate policies with the Realty benchmark.
8. Evaluate recorded manual Claude/Codex handoff outcomes.
9. Draft a separate design for autonomous execution.
10. Build the production OpenRouter gateway as an independent initiative.

## 17. Operational success measures

The first release will report rather than claim improvement. Target hypotheses are:

- 10-25% shorter production release time after router calibration.
- 50-70% lower API-equivalent development inference cost versus frontier-only execution.
- Comparable or better machine-verified acceptance and escaped-defect rates.
- No tenant-isolation, authorization, security, migration, or deployment task made autonomous.
- No cross-repository content in machine-wide learning data.

The Realty benchmark and subsequent verified outcomes determine whether these hypotheses hold. Failed hypotheses cause policy adjustment or rollback rather than relaxed quality requirements.

## 18. Repository preparation and implementation isolation

Implementation begins only from a passing controlled-temp test baseline. Existing unrelated TypeScript resolver work must be completed, committed, or isolated before routing changes begin. Routing commits must not absorb unrelated working-tree changes.

The implementation plan will create small, reviewable changes aligned with the rollout order and will rebuild and validate Graphite's own graph after each graph-affecting phase.
