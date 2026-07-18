# Claude and Codex Development Router Implementation Plan

> **Implementation rule:** Execute tasks in order with red-green-refactor discipline. Do not make live subscription calls until Task 9, and never retry or fall back during live acceptance.

**Goal:** Replace Ollama Cloud in Graphite's governed development router with approval-bound, subscription-authenticated Claude Code and Codex CLI execution in isolated Git worktrees.

**Architecture:** Provider-neutral contracts bind a signed approval to one tested CLI capability snapshot, requested model, effort, repository commit, context manifest, worktree, limits, and permissions. Provider adapters share a hardened subprocess runner but own exact arguments and structured-event parsing. Graphite validates every resulting diff and repository check independently; cross-provider review is read-only and separately approved.

**Technology:** Python 3.11+, standard-library subprocess and SQLite, existing Graphite routing contracts, Git worktrees, pytest, Ruff, deterministic fake executables, Claude Code CLI, Codex CLI.

**Design:** `docs/superpowers/specs/2026-07-18-claude-codex-development-router-design.md`

**Planning evidence:** Local help was inspected for Claude Code 2.1.208 and Codex CLI 0.144.1 on 2026-07-18. Those observations define adapter tests, not an evergreen compatibility claim. Model and effort evidence must be refreshed from official documentation and verified against the authenticated subscription before activation:

- Claude effort documentation: <https://platform.claude.com/docs/en/build-with-claude/effort>
- OpenAI model catalog: <https://developers.openai.com/api/docs/models/all>
- Installed CLI `--help` and authentication-status output, captured only as sanitized versioned fixtures

## Global constraints

- No package installation is required. If implementation discovers a new dependency is unavoidable, stop and run the repository package validator before installation.
- Never invoke a shell for provider execution. Use fixed argument arrays and `shell=False`.
- Never read or persist Anthropic/OpenAI API keys. Strip API keys, cloud credentials, registry tokens, and unrelated secrets from child environments.
- Do not infer CLI subscription model availability from API documentation.
- A provider/model/effort profile is execution-eligible only after a separately approved verification call records the effective model, CLI identity, CLI version, adapter protocol version, and verification time.
- Normal CI uses deterministic fake executables and consumes no subscription quota.
- No provider retry, fallback, effort change, session resume, automatic merge, or automatic cleanup of rejected work.
- Preserve the optional Ollama report-enrichment adapter; remove Ollama only from governed development routing.

## File responsibility map

- `src/graphite/routing/contracts.py`: provider-neutral identity, capability snapshot, approval, receipt, diff, and review contracts.
- `src/graphite/routing/profiles.py`: immutable profile definitions and verified capability snapshot loading.
- `src/graphite/routing/process_runner.py`: bounded no-shell subprocess execution and descendant cleanup.
- `src/graphite/routing/claude_executor.py`: Claude authentication/version preflight, fixed arguments, and structured-output parsing.
- `src/graphite/routing/codex_executor.py`: Codex authentication/version preflight, fixed arguments, and JSONL parsing.
- `src/graphite/routing/worktree.py`: isolated worktree lifecycle, containment, baseline identity, and quarantine.
- `src/graphite/routing/diff_policy.py`: bounded Git diff inspection and sensitive-change rejection.
- `src/graphite/routing/service.py`: recommendation, approval, execution, validation, review, and integration orchestration.
- `src/graphite/routing/storage.py`: schema v4 migration, capability snapshots, legacy history, attempt state, review linkage, and telemetry.
- `src/graphite/routing/policy.py`: provider-aware gates, priors, evidence thresholds, and reviewer selection.
- `src/graphite/routing/settings.py`: bounded CLI, worktree, diff, and validation settings.
- `src/graphite/cli.py`: explicit profile verification, recommendation, execution, review, accept/reject, and recovery commands.
- `tests/fake_clis/`: deterministic Claude/Codex executables and bounded event fixtures.
- `tests/test_routing_*`: unit, contract, migration, security, and orchestration coverage.
- `README.md`, `ARCHITECTURE.md`, design/evidence notes: operator contracts, migration, rollback, and acceptance evidence.

---

## Task 1: Introduce provider-neutral identity and approval contracts

**Files:**
- Modify `src/graphite/routing/contracts.py`
- Modify `src/graphite/routing/approval.py`
- Add `tests/test_routing_cli_contracts.py`
- Modify `tests/test_routing_approval.py`

- [x] Write failing tests for `ProviderId`, `CliIdentity`, `CapabilityProfile`, and `CapabilitySnapshot`. Require exact bounded identifiers, semantic CLI versions, normalized executable hashes, supported efforts, permission mode, risk ceiling, and a canonical SHA-256 snapshot digest.
- [x] Write a failing approval test proving provider, effective model, CLI identity/version, capability digest, repository commit, canonical worktree identity, and permission policy are signature-bound. Mutating any field must yield `approval_manifest_changed`.
- [x] Add a v4 CLI approval manifest with `capability_snapshot_digest`, provider, CLI, commit, worktree, and permission fields. Preserve the schema-v3 `ApprovalManifest` unchanged until Task 2 performs the atomic storage cutover; after cutover it remains a historical decoder only and can never receive new authority.
- [x] Extend `ExecutionReceipt` with provider, effective model, CLI version, changed-file count, changed-byte count, validation outcome, and optional provider-reported usage. Do not add raw text, prompt, paths, or diagnostics.
- [x] Run:

  ```powershell
  python -B -m pytest -q --basetemp F:\tmp\graphite-cli-router-task1 tests/test_routing_cli_contracts.py tests/test_routing_approval.py tests/test_routing_contracts.py
  python -B -m ruff check src/graphite/routing/contracts.py src/graphite/routing/approval.py tests/test_routing_cli_contracts.py tests/test_routing_approval.py
  git diff --check
  ```

- [x] Commit: `feat: add provider-neutral routing authority contracts`.

## Task 2: Add schema-v4 migration, backup gate, and legacy quarantine

**Files:**
- Modify `src/graphite/routing/storage.py`
- Add `tests/fixtures/routing_schema_v3_94eb333.sql`
- Modify `tests/test_routing_storage.py`
- Modify `tests/test_routing_cli_recovery.py`

- [x] Capture the current schema-v3 DDL as an immutable fixture with provenance and sanitized sample Ollama history.
- [x] Write failing migration tests for v3 to v4, fresh v4 creation, idempotent reopen, malformed digest rejection, partial-migration quarantine, concurrent-writer refusal, and preservation of historical Ollama attempts.
- [x] Require a verified pre-migration backup marker before destructive schema replacement. Use an atomic same-volume backup, SQLite integrity check, restrictive permissions, and a digest recorded outside the migrated database.
- [x] Migrate stored identity to provider plus `capability_snapshot_digest`. Add capability snapshot, task-worktree, validation-result, and review-link tables with bounded fields, foreign keys, uniqueness, state-transition triggers, and immutable terminal evidence.
- [x] Prevent any v3 Ollama record from becoming a v4 approval, executable attempt, reconciliation input, or reviewer authority.
- [x] Add stable CLI error codes for backup failure, migration busy, unsupported schema, quarantined state, and rollback-required state.
- [x] Run focused migration, recovery, integrity, and trigger tests with a writable `--basetemp`.
- [x] Commit: `feat: migrate routing authority to CLI capability snapshots`.

## Task 3: Build the hardened common CLI process boundary

**Files:**
- Add `src/graphite/routing/process_runner.py`
- Add `tests/test_routing_process_runner.py`
- Add `tests/fake_clis/fake_cli.py`

- [x] Write failing tests for fixed argv, closed stdin after bounded prompt transfer, no shell, minimal environment, output caps, one monotonic deadline, cancellation, timeout, nonzero exit, malformed UTF-8, and process-tree termination.
- [x] Test Windows Job Object containment and POSIX process-group containment behind platform adapters. If native descendant containment cannot be established, return `process_containment_unavailable` before approval consumption.
- [x] Define an explicit environment allowlist needed for executable loading, terminal-neutral behavior, locale, subscription-owned CLI home, and OS operation. Strip `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, cloud credentials, registry tokens, proxy overrides not explicitly allowed, and repository-defined environment injection.
- [x] Bound stdout and stderr independently. Hash canonical input and validated structured output, but persist neither body.
- [x] Ensure cleanup is idempotent, deadline-bounded, and safe after partial startup.
- [x] Run process-runner and routing security tests on Windows; keep explicit platform skips only for tests whose equivalent platform contract is covered elsewhere.
- [x] Commit: `feat: add bounded CLI process runner`.

## Task 4: Implement profile verification and immutable capability snapshots

**Files:**
- Add `src/graphite/routing/profiles.py`
- Modify `src/graphite/routing/policy.py`
- Modify `src/graphite/routing/settings.py`
- Add `tests/test_routing_profiles.py`
- Modify `tests/test_routing_policy.py`

- [x] Define provisional requested profiles without claiming subscription availability. Claude may use aliases documented by the installed CLI (`fable`, `sonnet`, `opus`) only with explicitly supported effort levels. Codex identifiers must come from operator-selected, official evidence and remain ineligible until effective identity is verified.
- [x] Do not bundle a guessed Codex default or translate API model availability into subscription availability.
- [x] Implement an explicit verification workflow that performs one approved no-edit request, parses the effective model and CLI metadata, and records a short-lived capability snapshot. Verification cannot grant repository-write authority or count as task execution evidence.
- [x] Hash the sorted snapshot including provider, requested and effective model, effort, executable hash, CLI version, adapter protocol version, permission capability, evidence reference, and verification time.
- [x] Apply hard gates for snapshot expiry, executable/version drift, auth health, context, task capability, risk ceiling, permission mismatch, and budget. Unknown identity fails closed.
- [x] Add deterministic provider-aware priors only after hard gates. Keep evidence adjustments bounded and require the configured minimum sample count.
- [x] Run profile and policy tests, including property-style permutation tests for canonical digest stability and deterministic ranking.
- [x] Commit: `feat: add verified CLI capability profiles`.

## Task 5: Implement Claude Code and Codex adapters

**Files:**
- Add `src/graphite/routing/claude_executor.py`
- Add `src/graphite/routing/codex_executor.py`
- Remove `src/graphite/routing/ollama_executor.py` from development routing imports; retain unrelated enrichment code.
- Add `tests/test_routing_claude_executor.py`
- Add `tests/test_routing_codex_executor.py`
- Replace Ollama-specific cases in `tests/test_routing_executor.py`

- [x] Create deterministic fake CLI scenarios for auth success/failure, version drift, effective-model match/mismatch, valid edit events, quota/rate limits, malformed output, oversized output, timeout, and cancellation.
- [x] Claude preflight uses `claude auth status --json` and a separately bounded version check. Execution uses non-interactive structured output, no session persistence, safe mode, no fallback model, an explicit restricted tool set, and an explicit effort/model. Never use `--dangerously-skip-permissions`, `--continue`, `--resume`, background agents, plugins, Chrome, MCP, or implicit settings.
- [x] Codex preflight uses `codex login status` and a separately bounded version check. The canonical execution argv places global controls before the subcommand: `codex --strict-config -a never -s workspace-write -C <worktree> -m <model> -c model_reasoning_effort=<effort> exec --json --ephemeral --ignore-user-config --ignore-rules -`. Never use bypass flags, resume, cloud tasks, additional writable directories, or local/Ollama providers.
- [x] Use adapter-specific allowlisted event schemas and reject unknown terminal identity, duplicate terminal events, trailing events, inconsistent usage, path-bearing diagnostics, or missing completion identity.
- [x] Normalize only stable sanitized failures: auth, quota, unavailable, version, model mismatch, protocol, response limit, timeout, cancelled, and containment.
- [x] Prove with tests that neither adapter retries, falls back, changes model/effort, resumes, opens a shell, or receives stripped secrets.
- [x] Commit: `feat: execute approved tasks through Claude and Codex CLIs`.

## Task 6: Add isolated worktrees and diff security policy

**Files:**
- Add `src/graphite/routing/worktree.py`
- Add `src/graphite/routing/diff_policy.py`
- Add `tests/test_routing_worktree.py`
- Add `tests/test_routing_diff_policy.py`
- Modify `src/graphite/routing/settings.py`

- [x] Write failing tests for clean and dirty source repositories, commit drift, nested repositories, symlinks, Windows reparse points, path aliases, case collisions, submodules, and worktree roots outside the controlled state directory.
- [x] Create worktrees with fixed Git argv at the approval-bound commit under a private Graphite task directory. Record canonical root, Git common-dir identity, baseline commit, and quarantine state.
- [x] Never remove an unaccepted worktree automatically. Cleanup requires explicit task identity, containment revalidation, terminal audit state, and user authority.
- [x] Parse Git's machine-readable diff/name-status output with bounds. Reject path escape, sensitive configuration and credential locations, `.git*` control changes, submodules, binary patches, executable-policy violations, excessive files, excessive bytes, and unmerged states.
- [x] Revalidate the filesystem after execution to detect symlink/reparse replacement and writes outside the approved worktree. A containment uncertainty fails closed and quarantines evidence.
- [x] Hash the accepted diff deterministically without storing its contents in routing telemetry.
- [x] Run worktree/diff tests plus existing Git security tests.
- [x] Commit: `feat: contain routed edits in inspected worktrees`.

## Task 7: Rewire service, CLI, review, validation, and integration gates

**Files:**
- Modify `src/graphite/routing/service.py`
- Modify `src/graphite/routing/shadow.py`
- Modify `src/graphite/cli.py`
- Modify `tests/test_routing_service.py`
- Modify `tests/test_routing_cli.py`
- Modify `tests/test_routing_shadow.py`

- [x] Replace the cached Ollama inventory flow with verified capability snapshots. Recommendations expose provider, requested/effective model, effort, snapshot expiry, permission mode, rationale, limits, and `single_use_approval_required`.
- [x] Split execution into explicit prepare, approve, run, inspect, validate, review, accept, reject, and cleanup state transitions. Every transition is idempotent or rejects replay with a stable code.
- [x] Bind the canonical prompt hash before approval consumption. Consume approval and reserve quota immediately before starting the one provider process.
- [x] Run configured validation commands through a separate bounded process policy after diff inspection. Validation cannot inherit provider credentials or model-generated shell state.
- [x] Implement high-risk cross-provider review as read-only, separately approved, independently budgeted, and linked to the primary diff hash. It cannot mutate or reuse the primary worktree.
- [x] Add explicit accept/reject CLI commands. Accept records human authority and creates a bounded integration commit or emits a cherry-pickable commit ID; it never merges automatically. Reject quarantines the worktree. Cleanup remains separate.
- [x] Ensure JSON and non-TTY modes cannot grant approval. `--yes` remains incapable of consent.
- [x] Remove governed-routing Ollama refresh and run commands while keeping unrelated report enrichment commands compatible.
- [x] Run service, CLI, approval, review, recovery, and security tests.
- [x] Commit: `feat: orchestrate approval-gated CLI development tasks`.

## Task 8: Complete evidence learning and policy promotion controls

**Files:**
- Modify `src/graphite/routing/telemetry.py`
- Modify `src/graphite/routing/policy.py`
- Modify `src/graphite/routing/storage.py`
- Modify `tests/test_routing_telemetry.py`
- Modify `tests/test_routing_policy.py`
- Modify `tests/test_routing_storage.py`

- [x] Record only provider/profile identity, task category/risk, latency, reported usage, diff size, validation outcome, review defect classes, rework count, human verdict, and provenance.
- [x] Prove source, prompt, response, diff contents, secrets, paths, and raw diagnostics cannot enter telemetry schemas or public serialization.
- [x] Add minimum evidence counts, bounded confidence intervals or conservative scoring penalties, recency weighting, and deterministic tie-breaking. Missing cost is `unknown`, never zero.
- [x] Policy learning produces a candidate version and comparison evidence only. Promotion requires an explicit human action and cannot change allowlists, permission ceilings, risk ceilings, or autonomy.
- [x] Add rollback to the previous signed policy version without deleting evidence.
- [ ] Commit: `feat: learn CLI routing performance under promotion gates`.

## Task 9: Documentation, rollback drill, and offline acceptance

**Files:**
- Modify `README.md`
- Modify `ARCHITECTURE.md`
- Modify `tests/test_documentation.py`
- Add `docs/superpowers/implementation-notes/2026-07-18-claude-codex-router-evidence.md`
- Update the design status only from source-derived evidence.

- [x] Document the trust boundaries, authenticated-CLI-only setup, explicit profile verification, worktree lifecycle, review policy, telemetry minimization, and absence of automatic fallback/merge.
- [x] Document v3 backup, v4 migration, integrity verification, stop-routing requirement, restore procedure, and forward-fix path.
- [x] Exercise the rollback fixture: migrate a v3 copy to v4, validate history, stop writers, restore the verified v3 backup, and prove the old schema reader succeeds. Do not alter the operator's real routing database during tests.
- [x] Run focused routing tests and Ruff, then the complete suite with an explicit writable base temp:

  ```powershell
  python -B -m pytest -q --basetemp F:\tmp\graphite-cli-router-full
  python -B -m ruff check src tests
  graphite build .
  graphite validate --graph-json graph-out\graph.json --json
  git diff --check
  git status --short --branch
  ```

- [x] Record exact commit, test counts, intentional skips, graph counts, schema drill result, and unresolved external-readiness gates. Commit documentation and evidence.

## Task 10: Explicit bounded live acceptance

**Authority gate:** Stop and request the user's approval for each exact call after displaying provider, requested model, effort, CLI version, capability snapshot digest, repository/fixture fingerprint, permission mode, maximum usage reservation, and timeout.

- [x] Use a disposable synthetic Git fixture containing no secrets or user source. Verify canonical paths and Graphite graph freshness.
- [x] Make one no-edit profile-verification call for the selected Claude profile. No retry or fallback. Record only sanitized receipt metadata and human verdict.
- [x] Make one no-edit profile-verification call for the selected Codex profile. No retry or fallback. Record only sanitized receipt metadata and human verdict. The identity/response checks passed, but reported input usage exceeded the approval, so the result is invalid and no active snapshot remains.
- [ ] After both profiles are verified, request separate approval for one bounded edit smoke per provider in isolated disposable worktrees. Each call must create a trivial tested change, pass diff policy and validation, and remain unmerged.
- [ ] Exercise one separately approved read-only cross-provider review against a synthetic high-risk diff.
- [x] If any call fails, preserve failed-closed evidence and do not substitute another model. Fix only offline-contract defects without another call; any repeat requires a new explicit approval.
- [ ] Mark the capability production-ready only if both adapters pass profile verification, edit smoke, cross-provider review, audit persistence, and the rollback drill. Otherwise document the exact remaining gate.

## Final branch gate

- [ ] Re-run the full offline suite and graph validation after the final evidence commit.
- [ ] Confirm no API keys, auth payloads, prompts, source, response text, worktree content, or raw provider diagnostics entered Git history or routing telemetry.
- [ ] Confirm `main` remains unchanged until the user selects merge, PR, keep, or discard.
- [ ] Use the branch-finishing workflow and report production limitations without overclaiming readiness.
