# Claude and Codex Development Router Design

**Date:** 2026-07-18
**Status:** Approved for implementation planning
**Scope:** Governed application-development routing through authenticated Claude Code and Codex CLIs

## 1. Objective

Replace Ollama Cloud in Graphite's governed development router with the locally authenticated Claude Code and Codex CLIs. Preserve deterministic Graphite analysis, explicit execution authority, bounded resource use, durable audit state, recovery, and evidence-based routing. The design must improve operational stability without treating any model output as validation or release authority.

Graphite will use existing CLI subscription sessions only. It will not request, read, store, forward, or infer Anthropic or OpenAI API keys. OpenRouter remains the selected provider for separate in-application LLM requirements. The existing optional Ollama enrichment adapter remains available for backward compatibility, disabled by default, and is excluded from governed development routing.

## 2. Non-goals

- Autonomous execution or merging
- Automatic provider or model fallback
- Dual-running every task
- Provider API integration or separate API billing
- Self-modifying allowlists, permissions, risk ceilings, or autonomy policy
- Fabricated USD cost estimates when subscription usage is not comparable
- Removing the independent, optional local enrichment adapter

## 3. Chosen Architecture

Graphite will implement provider-neutral routing contracts with separate `ClaudeCodeExecutor` and `CodexExecutor` adapters. A persistent broker daemon is rejected for this phase because it adds credential concentration, IPC security, lifecycle management, and another failure domain. Static command templates are rejected because they are too fragile for identity binding, structured parsing, and durable audit receipts.

The common executor contract owns approval verification, context binding, process limits, diff inspection, receipt construction, and stable errors. Each adapter owns only provider-specific executable validation, argument construction, authentication preflight, environment policy, structured-event parsing, usage extraction, and process termination details.

The deterministic graph, validation, policy gates, and repository checks remain independent authority boundaries. Model responses and edits are untrusted inputs.

## 4. Provider and Model Identity

An eligible profile is an exact, versioned tuple containing:

- Provider (`claude-code` or `codex`)
- Requested model identifier or documented alias
- Effort level
- Tested CLI version range
- Supported task capabilities
- Minimum verified context limit
- Risk ceiling
- Coarse usage class
- Evidence source and verification date

Graphite uses a maintained allowlist of tested tuples and never invents availability through heuristic discovery. Preflight verifies the resolved executable, executable identity, CLI version, authentication health, and requested profile. Claude streaming output reports the generating model on assistant events, and every event must match the approval. Codex 0.144.1 exposes exact full slugs in its model catalog, but its documented `exec --json` terminal event reports usage without echoing a model. For Codex, the contract identity is therefore the non-alias full slug bound in the capability snapshot and passed through strict, user-config-free `-m` execution; a successful terminal proves that selected slug was accepted. If a future terminal event reports a model, it must match. An alias or profile whose exact contract identity cannot be established cannot receive execution authority.

A canonical capability snapshot is sorted and hashed. The approval binds its digest in place of the Ollama inventory digest. The snapshot includes the selected profile, CLI identity and version, adapter protocol version, and verification time. It contains no credential material.

## 5. Routing and Learning

Hard eligibility gates precede scoring: authenticated CLI health, tested version compatibility, task capability, context size, risk ceiling, requested permissions, and remaining repository and machine budgets.

Initial recommendations use versioned expert priors. Routine bounded edits favor lower-usage or lower-effort profiles. Architecture, security, concurrency, database migrations, difficult debugging, and broad changes favor stronger profiles. One provider executes a normal task. A high-risk task may receive a separately approved, read-only review by the other provider.

Graphite records task category, selected profile, latency, reported usage when available, deterministic validation outcome, review findings, rework, and explicit human acceptance. It excludes repository content, prompts, secrets, and response text from learning telemetry. Missing or incomparable subscription cost data is recorded as unknown rather than converted to invented USD values.

Historical performance may adjust recommendations only after a configured minimum evidence threshold. Learning emits a versioned candidate policy and audit evidence; it cannot silently change the allowlist, permissions, risk ceilings, or autonomy level. Promotion requires regression evaluation and explicit human approval. Controlled exploration is restricted to low-risk tasks and still requires execution approval.

## 6. Execution Flow

1. Verify the repository state, selected commit, executable identity, CLI version, authentication health, and profile eligibility.
2. Classify the task, apply hard gates, score eligible profiles, and present one recommendation with rationale and bounded limits.
3. Build a source-free public context manifest and bounded private context. Bind both to a short-lived, signed, single-use approval.
4. After approval, create an isolated Git worktree at the approved commit. Never edit the user's active checkout directly.
5. Start exactly one non-interactive CLI process with workspace-only writes, no unattended privilege escalation, no provider fallback, no session reuse, bounded I/O, one deadline, a bounded process tree, and a minimal environment with ambient secrets removed.
6. Capture bounded structured events and the resulting Git diff. Do not persist raw model text in audit telemetry.
7. Reject containment escapes, symlink or reparse-point escapes, submodule changes, sensitive configuration or secret paths, binary mutations, excessive files, excessive bytes, or modifications outside the approved worktree.
8. Run deterministic graph validation and configured repository checks.
9. Persist a sanitized receipt and show the user the diff, validation evidence, available usage telemetry, and accept/reject controls.
10. Integrate accepted work only through a separate explicit commit or cherry-pick action. Rejected work remains quarantined until deliberate cleanup.

The Claude adapter uses an explicitly restricted tool set and non-interactive structured output. The Codex adapter uses workspace-write sandboxing and a non-interactive approval policy that cannot escalate. Provider-specific permissions must be at least as restrictive as the common Graphite policy; a CLI capability gap fails closed.

## 7. Review Flow

Cross-provider review is available only for configured high-risk categories. It receives a separately bounded context representing the approved primary diff, runs read-only, reserves its own budget, and requires its own approval. It cannot modify the primary worktree or reuse the primary approval, nonce, quota reservation, or receipt identity. Review failure does not trigger another provider automatically and does not erase primary evidence.

## 8. Failure Handling and Audit

Stable sanitized failure codes cover missing executables, authentication expiry, quota or rate limits, unsupported versions, profile mismatch, malformed structured output, response limits, timeout, cancellation, unsafe diffs, failed validation, and persistence failure. Error records contain no repository path, prompt, response body, credential, or provider diagnostic text.

The executor never retries, changes effort, switches model, switches provider, resumes a session, or reuses an approval. Timeout and cancellation terminate the bounded process tree. If execution began and exact usage is unavailable, accounting remains conservative. The existing durable attempt state machine and staged-receipt reconciliation remain in force; reconciliation never invokes a provider or grants authority.

## 9. Storage Migration and Rollback

Routing storage advances through a forward migration that replaces the approval-bound `inventory_digest` concept with `capability_snapshot_digest` while retaining legacy Ollama history. Historical records remain readable, retain their original provider identity, and can never be replayed as Claude or Codex executions.

Before migration, Graphite creates or requires an operator-confirmed backup and stops concurrent routing writers. Malformed or partially migrated state is quarantined and fails closed. Database constraints and triggers enforce digest shape, identity immutability, approval uniqueness, and valid state transitions.

The previous release cannot be assumed to read the new schema. Rollback therefore requires stopping routing, restoring the verified pre-migration database backup, and reverting code as one operation. Documentation must include backup verification, restore validation, forward-fix guidance, and an exercised rollback checklist.

## 10. Verification

Unit and security tests cover:

- Exact argument construction and environment filtering for both adapters
- Executable identity, authentication, version, profile, and effective-model checks
- Structured-output parsing, size bounds, malformed events, and sanitized errors
- Timeout, cancellation, descendant termination, and cleanup
- Worktree containment, symlink and reparse-point defenses, sensitive paths, submodules, binary files, file-count limits, and byte limits
- Approval binding, single use, expiry, quota reservation, and cross-provider isolation
- Independent reviewer authority
- Learning thresholds, telemetry minimization, policy versioning, and human promotion gates
- Schema migration, legacy quarantine, backup validation, reconciliation, and rollback fixtures

Contract tests use deterministic fake CLI executables; ordinary CI never consumes subscription quota. Opt-in live smoke tests make exactly one approved, non-destructive request per provider and cannot retry or fall back. Acceptance also requires the complete regression suite, static checks, a fresh graph build and validation, documentation verification, and a clean worktree.

Graphite must not be described as production-ready for this capability until both authenticated CLIs pass bounded live smoke tests and the schema rollback exercise is verified.

## 11. Operational Security

The CLI subprocess environment is an explicit trust boundary. It uses fixed argument arrays with no shell, closed stdin except for the canonical bounded request, controlled working directories, bounded stdout and stderr, and a minimal environment. Ambient API keys, cloud credentials, registry tokens, and unrelated session variables are removed. Subscription authentication remains owned by each CLI's established credential mechanism and is never copied into Graphite state.

Worktree creation, diff collection, validation, and integration are distinct authority stages. Graphite records who approved execution and integration, which commit and profile were approved, which checks ran, and their sanitized outcomes. No claim of perfect security is made; the design emphasizes prevention, containment, detection, auditability, response, and recoverable rollback.

## 12. Acceptance Criteria

- Governed development routing has no Ollama model profiles or Ollama execution path.
- Claude Code and Codex are the only development execution providers.
- Both use existing authenticated CLI sessions without API-key handling.
- One approval authorizes one exact provider/model/effort/CLI/worktree/context execution.
- No automatic retry, fallback, session reuse, or merge exists.
- Primary edits occur only in an isolated worktree and pass containment and diff policy.
- High-risk cross-provider review is read-only and separately approved.
- Telemetry supports evidence-based future routing without retaining source or response text.
- Legacy audit history survives migration but cannot grant new authority.
- Offline tests pass without subscription calls; live tests remain explicit and bounded.
- Documentation states unresolved readiness limitations and the tested rollback procedure.
