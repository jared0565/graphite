# Provider Lifecycle and Canonical Graph Isolation Implementation Plan

> **Implementation rule:** Execute tasks in order with red-green-refactor discipline. All ordinary tests use fake executables or local fake HTTP servers. Do not invoke Claude, Codex, Ollama inference, OpenRouter inference, another model, retry, fallback, or network inference during Tasks 1-10.

**Goal:** Make AI runtime updates an isolated lifecycle event rather than a Graphite outage, while preserving exact identity authority for governed execution and making canonical graph operations deterministic and provider-independent.

**Architecture:** A provider-neutral lifecycle registry stores sanitized observations and append-only transitions separately from capability snapshots and execution attempts. Narrow adapters perform bounded non-inference probes for Claude Code, Codex, Ollama, and OpenRouter. The daemon observes drift but cannot grant authority; every execution performs the same final identity check. Canonical graph commands are structurally inference-free, and optional model enrichment writes only fingerprint-bound, non-authoritative overlays.

**Technology:** Python 3.11+, standard-library subprocess, SQLite, HTTP clients, hashing, existing Graphite routing contracts, pytest, Ruff, deterministic fake CLIs, and local fake HTTP servers.

**Design:** `docs/superpowers/specs/2026-07-19-provider-lifecycle-graph-isolation-design.md`

## Global constraints

- No package installation is planned. If a new dependency becomes unavoidable, stop and run the repository package validator before installation.
- Do not work on `main`, merge, push, or establish an upstream without explicit authorization.
- Never run a provider process through a shell. Use fixed argument arrays, bounded output, one monotonic deadline, and the hardened process runner.
- Lifecycle probes may inspect only approved identity, version, capability, credential-health, and bounded metadata surfaces. They must not invoke generation, chat, embedding, tool, completion, or other inference endpoints.
- Never persist raw stdout, stderr, HTTP bodies, redirects, provider diagnostics, executable paths from provider output, prompts, source, model output, auth payloads, or credentials.
- Compatibility is not capability authority. A successful drift probe may produce `verification_required`; it can never produce `active`.
- Any runtime, endpoint, model digest, provider-routing policy, or required-capability change invalidates matching snapshots and unconsumed approvals.
- Canonical graph operations must behave identically when providers are healthy, changed, missing, incompatible, unauthenticated, or unavailable.
- Every Graphite rebuild used for acceptance must explicitly use `--llm none` until canonical isolation is implemented and tested; afterward canonical commands must reject or ignore any attempt to enable enrichment.
- Live verification remains a separate authority-gated phase after this plan. Display a complete manifest and obtain explicit approval for each exact call.

## File responsibility map

- `src/graphite/routing/lifecycle.py`: normalized identities, compatibility policies, lifecycle states, transition rules, reason codes, and canonical digests.
- `src/graphite/routing/lifecycle_storage.py`: schema-v1 lifecycle database, backup/integrity handling, immutable transition events, current observations, and invalidation records.
- `src/graphite/routing/probe_runner.py`: bounded no-inference process and HTTP probe boundary, endpoint policy, response caps, redirect policy, and sanitized failure classification.
- `src/graphite/routing/claude_probe.py`: Claude executable, version, auth-health, and required-capability observation.
- `src/graphite/routing/codex_probe.py`: Codex executable, version, auth-health, and required-capability observation.
- `src/graphite/routing/ollama_probe.py`: allowlisted endpoint, server version, API capability, tag, and immutable model-digest observation.
- `src/graphite/routing/openrouter_probe.py`: canonical HTTPS endpoint, configured model/routing identity, credential health, and non-inference contract observation.
- `src/graphite/routing/lifecycle_service.py`: provider-neutral discovery, compatibility evaluation, transition persistence, snapshot/approval invalidation, manifest preparation, and lazy checks.
- `src/graphite/routing/contracts.py`, `profiles.py`, `approval.py`, `service.py`, and `storage.py`: bind lifecycle identity to capability snapshots, approvals, and final execution checks.
- `src/graphite/provider_observer.py`: bounded multi-provider observation scheduling and backoff, independent from graph builds.
- `src/graphite/daemon.py` and `daemon_health.py`: invoke and report observation without making it graph authority.
- `src/graphite/llm.py`: explicit overlay enrichment implementation only; no canonical build integration.
- `src/graphite/overlays.py`: overlay manifest, containment, stale detection, atomic writes, and non-authoritative reads.
- `src/graphite/cli.py`, `config.py`, and `watch.py`: canonical no-inference enforcement, explicit lifecycle/overlay commands, and operator status.
- `tests/fake_clis/` and new local fake-provider helpers: deterministic provider observations without subscription or external network usage.
- `tests/test_provider_*`, existing routing/daemon/LLM tests, and documentation: contracts, migrations, security, determinism, recovery, and operator guidance.

---

## Task 1: Add provider-neutral lifecycle contracts and compatibility policy

**Files:**
- Add `src/graphite/routing/lifecycle.py`
- Add `tests/test_provider_lifecycle.py`

Security refinement: lifecycle uses a distinct four-provider identifier so the
existing Claude/Codex governed-execution enum cannot silently accept HTTP
providers through legacy CLI fallback branches.

- [x] Write failing tests for `ProviderRuntimeIdentity`, `ProviderCompatibilityPolicy`, `ProviderLifecycleState`, `ProviderLifecycleEvent`, and stable reason codes. Bound all strings, collections, timestamps, and digest fields.
- [x] Define runtime kinds for local CLI, local HTTP runtime, and remote HTTP service. Support only the approved provider/runtime combinations: Claude/CLI, Codex/CLI, Ollama/local HTTP, and OpenRouter/remote HTTPS.
- [x] Canonically digest provider, runtime kind, normalized semantic version, executable or endpoint identity digest, configured model identity, provider-routing policy digest, required capabilities, and policy version. Exclude observation time and all raw diagnostics from the identity digest.
- [x] Implement explicit valid transitions among `discovered`, `compatible`, `verification_required`, `active`, `incompatible`, and `unavailable`. Reject direct discovery/probe transitions to `active`.
- [x] Classify unchanged, hash-only, patch, minor, major, capability, endpoint, model-digest, routing-policy, credential-health, and missing-runtime changes deterministically.
- [x] Require patch and hash-only changes to pass standard probes, minor changes to pass expanded probes, and major or required-capability changes to remain `incompatible` pending policy promotion.
- [x] Add serialization tests proving public records cannot contain paths, prompts, source, response bodies, credentials, auth payloads, or raw diagnostics.
- [x] Run focused lifecycle and contract tests, full offline routing tests, Ruff, and `git diff --check`.
- [x] Commit: `feat: add provider lifecycle authority contracts`.

## Task 2: Add isolated lifecycle persistence and schema-v5 routing bindings

**Files:**
- Add `src/graphite/routing/lifecycle_storage.py`
- Modify `src/graphite/routing/storage.py`
- Add `tests/fixtures/provider_lifecycle_schema_v1.sql`
- Add `tests/fixtures/routing_schema_v4_lifecycle_migration.sql`
- Add `tests/test_provider_lifecycle_storage.py`
- Modify `tests/test_routing_storage.py`
- Modify `tests/test_routing_cli_recovery.py`

- [ ] Keep lifecycle observations/events in a dedicated lifecycle database under the repository routing state directory. Do not place them in canonical graph artifacts or daemon status JSON.
- [ ] Write failing tests for fresh creation, idempotent reopen, integrity failure, malformed digests, invalid transitions, concurrent-writer refusal, immutable events, bounded retention, and atomic current-observation replacement.
- [ ] Store one current observation per provider/runtime/configuration boundary plus append-only transition evidence containing only old/new identity digests, state, reason code, policy version, and timestamps.
- [ ] Add explicit invalidation evidence linking a lifecycle identity change to affected capability snapshot digests and unconsumed approval IDs without copying their payloads.
- [ ] Advance the routing authority database from schema v4 to v5. Bind new capability snapshots and approvals to a lifecycle identity digest; retain v4 records as historical evidence without granting authority.
- [ ] Require a verified pre-migration backup marker, stopped writers for destructive transitions, SQLite integrity and foreign-key checks, restrictive permissions, and a tested restore path.
- [ ] Fail closed for provider execution when lifecycle authority is corrupt or missing, while leaving canonical graph commands independent and operational.
- [ ] Add recovery codes for lifecycle backup failure, migration busy, unsupported schema, integrity failure, and rollback required.
- [ ] Run lifecycle storage, routing migration, recovery, and trigger tests with an explicit writable `--basetemp`.
- [ ] Commit: `feat: persist isolated provider lifecycle authority`.

## Task 3: Build the bounded non-inference probe boundary

**Files:**
- Add `src/graphite/routing/probe_runner.py`
- Modify `src/graphite/routing/process_runner.py`
- Add `tests/test_provider_probe_runner.py`
- Modify `tests/test_routing_process_runner.py`

- [ ] Write failing process-probe tests for fixed argv, no shell, minimal environment, canonical executable containment, timeout, cancellation, output caps, malformed UTF-8, nonzero exit, and process-tree cleanup.
- [ ] Reuse the existing sanitized `CliProcessFailureDiagnostics` contract. Persist only exit classification/code, duration, stdout/stderr SHA-256, and an allowlisted failure category.
- [ ] Add a bounded HTTP probe client with explicit schemes, host allowlists, port policy, DNS/address validation, response-size caps, content-type validation, deadlines, and no automatic redirects.
- [ ] Reject loopback/private/link-local destinations for OpenRouter and reject non-loopback destinations for Ollama. Revalidate the connected address to reduce DNS-rebinding risk.
- [ ] Ensure credential injection occurs only inside the request boundary. Normalized observations and exceptions must never expose header values or raw response bodies.
- [ ] Define an allowlisted endpoint-purpose enum. Tests must prove lifecycle probes cannot address known generation, chat, completion, embedding, response, or tool endpoints.
- [ ] Add deterministic fake process and local HTTP fixtures for success, timeout, malformed data, oversized bodies, redirects, auth failure, rate limits, and unavailable services.
- [ ] Run probe-runner, process-runner, and routing security tests.
- [ ] Commit: `feat: add bounded non-inference provider probes`.

## Task 4: Implement Claude, Codex, Ollama, and OpenRouter probe adapters

**Files:**
- Add `src/graphite/routing/claude_probe.py`
- Add `src/graphite/routing/codex_probe.py`
- Add `src/graphite/routing/ollama_probe.py`
- Add `src/graphite/routing/openrouter_probe.py`
- Modify `src/graphite/routing/claude_executor.py`
- Modify `src/graphite/routing/codex_executor.py`
- Modify `src/graphite/routing/registry.py`
- Add `tests/test_provider_claude_probe.py`
- Add `tests/test_provider_codex_probe.py`
- Add `tests/test_provider_ollama_probe.py`
- Add `tests/test_provider_openrouter_probe.py`

- [ ] Refactor reusable executable identity and semantic-version parsing out of the CLI executors without weakening their execution-specific structured-output rules.
- [ ] Claude and Codex adapters must canonicalize approved executables, reject workspace-local files/symlinks/reparse points, hash bytes, obtain bounded version/auth-health results, and test required non-inference flags using deterministic fake CLIs.
- [ ] Do not invoke CLI prompts or verification requests from a lifecycle probe. A help/capability check that could initialize a session, contact inference, or produce unbounded output is forbidden.
- [ ] Ollama must accept only canonical loopback endpoints, observe server/API compatibility, and bind configured tags to immutable model digests through non-generation metadata endpoints.
- [ ] OpenRouter must accept only the approved canonical HTTPS endpoint and approved redirects set to empty, bind the configured model plus routing policy, and use only documented non-inference health/contract metadata.
- [ ] If OpenRouter offers no safe endpoint for a desired health claim, report that capability as `unknown` or use credential presence only; never substitute an inference call.
- [ ] Normalize all four adapters into the same lifecycle identity contract and stable failure categories.
- [ ] Prove every adapter performs one bounded probe sequence, has no retry/fallback/provider switching, and cannot leak raw outputs or credentials.
- [ ] Run all adapter, security, and existing executor tests.
- [ ] Commit: `feat: observe provider identities without inference`.

## Task 5: Implement lifecycle coordination, invalidation, and lazy enforcement

**Files:**
- Add `src/graphite/routing/lifecycle_service.py`
- Modify `src/graphite/routing/profiles.py`
- Modify `src/graphite/routing/approval.py`
- Modify `src/graphite/routing/policy.py`
- Modify `src/graphite/routing/service.py`
- Modify `src/graphite/routing/shadow.py`
- Add `tests/test_provider_lifecycle_service.py`
- Modify `tests/test_routing_profiles.py`
- Modify `tests/test_routing_approval.py`
- Modify `tests/test_routing_service.py`

- [ ] Write failing tests for first discovery, unchanged identity, every drift class, isolated provider failure, snapshot invalidation, pending-approval invalidation, and explicit verification activation.
- [ ] Coordinate adapter observations, compatibility policy, lifecycle storage, and sanitized event persistence in one transaction boundary where authority changes.
- [ ] A successful compatible drift probe must end at `verification_required`; activation requires a matching accepted capability snapshot created by the existing separately approved verification flow.
- [ ] Bind newly issued capability snapshots and approvals to the exact lifecycle identity digest. Reject stale snapshots before recommendation and reject stale approvals before consumption.
- [ ] Perform a final live identity observation immediately before approval consumption. If the daemon is stopped or stale, the lazy check is authoritative and bounded.
- [ ] Prevent time-of-check/time-of-use drift by comparing provider, runtime kind, executable/endpoint digest, version, model digest, routing policy, required capabilities, and lifecycle policy version.
- [ ] Ensure invalidation is provider- and identity-scoped. Claude drift cannot disable Codex, Ollama, OpenRouter, or canonical graph operation.
- [ ] Prepare complete but unexecuted verification manifests for `verification_required` identities. Manifest preparation must not contact a model.
- [ ] Run lifecycle service, profiles, approval, policy, routing service, shadow, and security tests.
- [ ] Commit: `feat: enforce provider lifecycle at execution time`.

## Task 6: Add daemon observation without graph authority

**Files:**
- Add `src/graphite/provider_observer.py`
- Modify `src/graphite/daemon.py`
- Modify `src/graphite/daemon_health.py`
- Modify `src/graphite/config.py`
- Modify `tests/test_daemon.py`
- Modify `tests/test_daemon_health.py`
- Add `tests/test_provider_observer.py`

- [ ] Add bounded observer options for enabled providers, observation interval, per-provider timeout, maximum observations per cycle, and capped exponential backoff with deterministic jitter disabled in tests.
- [ ] Schedule observations independently from project scan/build scheduling. Provider delay or failure must not consume the graph build budget or postpone graph freshness work.
- [ ] Cache identical machine-wide CLI observations within one daemon cycle while keeping project-scoped endpoint/model/routing configuration isolated.
- [ ] Persist lifecycle transitions through the lifecycle service and expose only aggregate state/reason codes in bounded daemon health output.
- [ ] Mark matching snapshots and pending approvals stale after drift; prepare notifications/manifests but never verify, activate, retry, or fall back.
- [ ] Add tests with a stopped daemon, stale daemon, probe timeout, corrupt lifecycle database, all providers absent, and simultaneous independent drift.
- [ ] Prove daemon logs/status contain no executable paths from provider output, endpoint query data, headers, credentials, bodies, raw diagnostics, prompts, or source.
- [ ] Prove daemon graph builds produce the same canonical fingerprint with every lifecycle state and failure mode.
- [ ] Run observer, daemon, daemon-health, routing security, and scheduling tests.
- [ ] Commit: `feat: observe provider drift in the daemon`.

## Task 7: Make canonical graph operations inference-free by construction

**Files:**
- Modify `src/graphite/cli.py`
- Modify `src/graphite/config.py`
- Modify `src/graphite/daemon.py`
- Modify `src/graphite/watch.py`
- Modify `src/graphite/llm.py`
- Modify `tests/test_cli.py`
- Modify `tests/test_daemon.py`
- Modify `tests/test_watch.py`
- Modify `tests/test_llm.py`
- Add `tests/test_graph_provider_isolation.py`

- [ ] Write failing tests proving `scan`, `build`, `check`, `validate`, `query`, `context`, `impact`, watch builds, and daemon builds cannot instantiate an LLM client or read provider credentials.
- [ ] Remove `enrich_report` from the canonical `_build` path. Canonical `analysis`, manifests, reports, and fingerprints must contain only deterministic local analysis.
- [ ] Make canonical commands force an internal no-inference configuration. Ambient `GRAPHITE_LLM*` variables, inherited daemon config, project config, and CLI aliases cannot enable enrichment.
- [ ] Reject non-`none` `--llm` values on canonical commands with a stable migration message directing users to the explicit overlay command. Preserve `--llm none` temporarily for compatibility.
- [ ] Hard-code daemon child build commands to the canonical path and remove API keys and provider configuration from their environment and argv.
- [ ] Remove watch/daemon warnings that imply enrichment remains possible; replace them with tests and documentation of the enforced boundary.
- [ ] Build identical fixture graphs under healthy, unavailable, incompatible, drifted, and absent provider states. Compare graph nodes, edges, communities, manifest inputs, validation, context, impact, and fingerprint.
- [ ] Run CLI, build, check, validate, query, context, impact, watch, daemon, LLM, and isolation tests.
- [ ] Commit: `fix: isolate canonical graphs from AI providers`.

## Task 8: Move optional enrichment into non-authoritative overlays

**Files:**
- Add `src/graphite/overlays.py`
- Modify `src/graphite/llm.py`
- Modify `src/graphite/cli.py`
- Modify `src/graphite/config.py`
- Add `tests/test_overlays.py`
- Modify `tests/test_llm.py`
- Modify `tests/test_documentation.py`

- [ ] Define an overlay manifest binding canonical graph fingerprint, provider lifecycle identity digest, model identity digest, routing policy digest, limits, creation time, outcome category, and overlay schema version.
- [ ] Add an explicit `graphite enrich` or `graphite overlay build` command. It must require an existing fresh canonical graph and write only beneath `graph-out/overlays/<provider>/<identity-digest>/`.
- [ ] Canonicalize and contain overlay paths; reject traversal, symlinks, reparse points, digest collisions, writes outside the output root, and replacement of canonical artifacts.
- [ ] Write overlay files atomically with restrictive permissions. A failed enrichment leaves the last valid overlay intact or records a separate sanitized failure marker.
- [ ] Mark overlays stale when the canonical fingerprint or provider/model identity changes. Overlay staleness must not make the canonical graph stale.
- [ ] Keep overlay reads opt-in and non-authoritative. `query`, `context`, `impact`, validation, and routing authority must ignore overlays unless a future separately designed feature explicitly requests display-only data.
- [ ] Prove Ollama and OpenRouter enrichment failures, deletion, drift, missing credentials, and incompatible versions cannot alter canonical artifacts or exit status.
- [ ] Do not perform any live enrichment during implementation. Use fake local providers and deterministic fixtures only.
- [ ] Run overlay, LLM, path-security, documentation, and graph-isolation tests.
- [ ] Commit: `feat: write AI enrichment as isolated overlays`.

## Task 9: Add operator commands, migration guidance, and recovery evidence

**Files:**
- Modify `src/graphite/cli.py`
- Modify `README.md`
- Modify `ARCHITECTURE.md`
- Modify `tests/test_routing_cli.py`
- Modify `tests/test_documentation.py`
- Add `docs/superpowers/implementation-notes/2026-07-19-provider-lifecycle-evidence.md`

- [ ] Add read-only lifecycle status/list/history commands with bounded JSON and human-readable output. Do not expose raw executable paths, endpoint query strings, credentials, or diagnostics.
- [ ] Add explicit compatibility-policy inspection and promotion preparation. Promotion itself requires a separate human-authorized command and cannot activate a provider.
- [ ] Add a command that prepares a verification manifest for one exact `verification_required` identity and stops before inference.
- [ ] Document patch/minor/major behavior, all lifecycle states, daemon responsibilities, lazy enforcement, isolated failures, graph guarantees, overlay semantics, and the absence of automatic activation/fallback.
- [ ] Document schema-v4 to v5 backup, migration, integrity verification, stopped-writer requirement, restore, and forward-fix paths.
- [ ] Exercise lifecycle and routing rollback fixtures without altering the operator's active routing database.
- [ ] Record exact tests, schema drill results, graph determinism results, intentional skips, and remaining live-readiness gates in the implementation evidence note.
- [ ] Run CLI, documentation, migration, recovery, and security tests.
- [ ] Commit: `docs: record provider lifecycle acceptance evidence`.

## Task 10: Complete offline acceptance and prepare live manifests

**Files:**
- Update `docs/superpowers/implementation-notes/2026-07-19-provider-lifecycle-evidence.md`
- Update implementation files only for defects found by offline acceptance

- [ ] Run focused lifecycle, probe, adapter, daemon, routing, overlay, LLM, graph-isolation, recovery, and documentation tests with unique writable base-temp directories.
- [ ] Run the complete offline suite and record exact pass/skip counts.
- [ ] Run Ruff over `src` and `tests`, `git diff --check`, staged-diff review, secret-pattern review, and repository status review.
- [ ] Run a clean canonical rebuild with no inference configuration and verify `check`, validation, context, impact, and query statistics.
- [ ] Compare canonical artifacts across fake provider states and record the stable fingerprint or exact semantically excluded fields.
- [ ] Verify lifecycle and routing databases with SQLite integrity and foreign-key checks; repeat the schema-v4 restore drill.
- [ ] Confirm no provider request, subscription call, external network inference, retry, fallback, model substitution, merge, or push occurred.
- [ ] Commit final offline corrections and evidence in reviewable bounded commits.
- [ ] Prepare, but do not execute, a new exact Claude verification manifest and equivalent manifests for any other provider requiring activation.

## Separate live-acceptance gate

Stop after Task 10. For each proposed live action, display and obtain explicit approval for the complete manifest containing:

- provider and runtime kind;
- executable/endpoint identity digest and observed version;
- requested and expected effective model identity;
- effort, permission mode, and no-edit/edit scope;
- lifecycle identity digest and compatibility policy version;
- capability snapshot state and intended expiry;
- fixture repository commit and canonical graph fingerprint;
- prompt/response contract hashes without prompt or response bodies;
- maximum input/output usage, cost reservation if applicable, and timeout;
- no-retry, no-fallback, no-resume, and no-model-substitution declarations;
- exact sanitized evidence fields that may be persisted.

Each provider verification, edit smoke, and cross-provider review requires separate approval. A failed call records sanitized failed-closed evidence and stops. It never authorizes a retry or fallback.

## Final branch gate

- [ ] Do not claim production readiness until the current Claude profile passes bounded verification, both governed providers pass separately approved edit smokes, cross-provider high-risk review passes, final audit persistence is verified, the complete suite passes, and the final canonical graph is fresh.
- [ ] Confirm routine provider updates do not require patch-version source edits but always invalidate exact execution authority when identity changes.
- [ ] Confirm provider failures and lifecycle corruption cannot block or alter canonical graph operation.
- [ ] Confirm all enrichment is isolated, non-authoritative, stale independently, and removable without affecting the canonical graph.
- [ ] Confirm `main` remains unchanged and the feature branch remains unpushed until the user explicitly selects the next Git action.
