# Adaptive Development Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Do not begin an implementation task until its red test is demonstrated. Preserve unrelated working-tree changes.

**Goal:** Add an approval-gated, Ollama-Cloud-only development router that recommends a tested model and supported effort from validated Graphite evidence, records verified outcomes, performs explicitly budgeted shadow comparisons, and builds evidence for separately approved future autonomy.

**Architecture:** A new `graphite.routing` package owns immutable contracts, trusted graph/context inputs, deterministic risk classification, model capability profiles, scoring, approval, loopback Ollama execution, SQLite evidence, shadow evaluation, and promotion eligibility. Existing graph construction and optional `llm.py` report enrichment remain independent. OpenRouter and direct Claude/Codex invocation are outside this implementation.

**Tech stack:** Python 3.11+, standard-library `argparse`, `dataclasses`, `enum`, `hashlib`, `hmac`, `json`, `math`, `secrets`, `sqlite3`, `urllib`, and existing Graphite validation/process primitives. No new Python, npm, or Ollama package may be installed.

**Approved design:** `docs/superpowers/specs/2026-07-14-adaptive-development-router-design.md`

---

## Locked scope and invariants

- Development routing invokes only allowlisted Ollama Cloud models through `http://127.0.0.1:11434` or the IPv6 loopback equivalent selected by Graphite.
- OpenRouter remains reserved for a separate production in-application inference initiative.
- Claude Code and Codex are manual handoff recommendations only.
- `route recommend` performs no HTTP request and never invokes a model.
- `route run` requires interactive default-No approval in this release. JSON, CI, redirected stdin/stdout, and `--yes` never grant model-call consent.
- The router cannot edit source, run arbitrary commands, install packages, pull models, or add providers.
- Invalid/stale graph evidence, unavailable audit storage, unsupported effort, unapproved outbound context, exhausted budget, or changed model identity fails closed.
- Detailed evidence stays repository-local. Machine-wide evidence is opt-in and sanitized before it crosses the repository boundary.
- Recommendation learning cannot lower risk, expand context, grant execution authority, or promote autonomy.
- High-risk task classes remain approval-gated permanently under this design.
- Every task below uses tests first, fixed public reason codes, bounded inputs/outputs, and sanitized errors.

## Intended file structure

Create:

```text
src/graphite/engine_identity.py
src/graphite/graph_io.py
src/graphite/routing/__init__.py
src/graphite/routing/contracts.py
src/graphite/routing/settings.py
src/graphite/routing/storage.py
src/graphite/routing/registry.py
src/graphite/routing/effort.py
src/graphite/routing/classifier.py
src/graphite/routing/context_builder.py
src/graphite/routing/policy.py
src/graphite/routing/approval.py
src/graphite/routing/ollama_executor.py
src/graphite/routing/telemetry.py
src/graphite/routing/shadow.py
src/graphite/routing/service.py
tests/test_engine_identity.py
tests/test_graph_io.py
tests/test_routing_contracts.py
tests/test_routing_storage.py
tests/test_routing_registry.py
tests/test_routing_classifier.py
tests/test_routing_context.py
tests/test_routing_policy.py
tests/test_routing_approval.py
tests/test_routing_executor.py
tests/test_routing_telemetry.py
tests/test_routing_shadow.py
tests/test_routing_cli.py
tests/test_routing_security.py
benchmarks/realty_router/README.md
benchmarks/realty_router/tasks.json
benchmarks/realty_router/evaluate.py
```

Modify only where required:

```text
src/graphite/__init__.py
src/graphite/cli.py
src/graphite/config.py
src/graphite/freshness.py
src/graphite/mcp_server.py
src/graphite/bootstrap.py
.gitignore
README.md
ARCHITECTURE.md
CONTRIBUTING.md
tests/test_cli.py
tests/test_context.py
tests/test_documentation.py
tests/test_hardening.py
tests/test_mcp_server.py
```

Do not modify `src/graphite/llm.py` except for a narrowly justified regression required to preserve report enrichment. Do not combine routing configuration with `GRAPHITE_LLM_*` variables.

---

## Task 0: Isolate the initiative and establish the baseline

**Files:** none

- [ ] **Step 1: Verify the original worktree and preserve unrelated changes**

Run in `F:\Projects\graphite`:

```powershell
git status --short --branch
git diff -- src/graphite/config.py src/graphite/resolve.py tests/test_typescript_resolver.py
```

Expected: the three pre-existing resolver files may remain modified. Do not stage, restore, or edit them.

- [ ] **Step 2: Create an isolated feature worktree from the committed plan**

```powershell
git worktree add -b feat/adaptive-development-router F:\tmp\graphite-adaptive-router HEAD
```

Verify both absolute paths resolve under `F:\Projects\graphite` and `F:\tmp` before any later worktree removal.

- [ ] **Step 3: Run the controlled baseline in the feature worktree**

```powershell
python -B -m pytest -q --basetemp F:\tmp\graphite-router-baseline
python -B -m ruff check .
git diff --check
python -m graphite build .
python -m graphite check .
python -m graphite validate --graph-json graph-out/graph.json --json
```

Expected baseline at the committed `HEAD`: 1,072 tests pass and 43 skip, lint is clean, and the freshly built graph is valid. The isolated worktree intentionally excludes the three uncommitted resolver changes that raised the original worktree result to 1,074 passing tests. If independently completed work changes the committed baseline before execution, record the new clean result in the implementation log rather than forcing the old count.

- [ ] **Step 4: Confirm routing has no existing authority**

Run: `python -m graphite --help`

Expected: no `route` command exists.

No commit is created for this task.

---

## Task 1: Make graph freshness engine-aware

**Files:**
- Create: `src/graphite/engine_identity.py`
- Modify: `src/graphite/cli.py`
- Modify: `src/graphite/freshness.py`
- Modify: `src/graphite/__init__.py`
- Create: `tests/test_engine_identity.py`
- Modify: `tests/test_reliability.py`

- [ ] **Step 1: Write red tests for engine identity**

Cover these cases:

- The identity is deterministic across repeated calls.
- It contains no absolute package path or host metadata.
- Changing a trusted package source byte changes the fingerprint in an isolated fixture package.
- File ordering does not affect the fingerprint.
- Non-regular files, excessive file counts, oversized files, unreadable files, and package-root crossings fail with fixed reason codes.
- The public manifest records `engine.version`, `engine.cache_version`, `engine.schema_version`, and `engine.fingerprint`.
- `check_graph_freshness()` reports `engine_changed` when repository hashes match but the engine identity differs.
- Legacy manifests without engine identity are stale rather than silently accepted.

Use a bounded package-source inventory: relative `.py`, `.mjs`, and packaged query/resource files only; exclude caches, bytecode, artifacts, tests, and repository files outside the installed `graphite` package.

- [ ] **Step 2: Demonstrate the red state**

Run:

```powershell
python -B -m pytest -q tests/test_engine_identity.py tests/test_reliability.py -k "engine or freshness"
```

Expected: failures because no engine identity exists and freshness compares repository hashes only.

- [ ] **Step 3: Implement bounded engine identity**

Add immutable public constants in `engine_identity.py`:

```python
ENGINE_SCHEMA_VERSION = "1"
MAX_ENGINE_FILES = 512
MAX_ENGINE_FILE_BYTES = 8 * 1024 * 1024
MAX_ENGINE_TOTAL_BYTES = 64 * 1024 * 1024
```

Hash normalized relative names, lengths, and bytes with SHA-256. Include `graphite.__version__`, `Config.cache_version`, and `ENGINE_SCHEMA_VERSION` in the structured identity. Use descriptor reads with before/after `fstat` checks and reject symlinks/reparse points and root crossings.

Do not expose the package root or individual source hashes in public artifacts.

- [ ] **Step 4: Write the identity into build manifests and compare it in freshness**

Update the normal build path before public export. Freshness returns fixed fields:

```python
{
    "stale": True,
    "reason": "engine_changed",
    "added": [],
    "changed": [],
    "removed": [],
}
```

Repository changes continue to use the existing added/changed/removed contract. An engine mismatch takes precedence when the manifest is otherwise readable.

- [ ] **Step 5: Run focused and regression tests**

```powershell
python -B -m pytest -q tests/test_engine_identity.py tests/test_reliability.py tests/test_cli.py
python -B -m ruff check src/graphite/engine_identity.py src/graphite/freshness.py src/graphite/cli.py tests/test_engine_identity.py
```

- [ ] **Step 6: Commit**

```powershell
git add src/graphite/engine_identity.py src/graphite/freshness.py src/graphite/cli.py src/graphite/__init__.py tests/test_engine_identity.py tests/test_reliability.py tests/test_cli.py
git commit -m "feat: make graph freshness engine-aware"
```

---

## Task 2: Add one bounded, validated graph-read boundary

**Files:**
- Create: `src/graphite/graph_io.py`
- Modify: `src/graphite/cli.py`
- Modify: `src/graphite/mcp_server.py`
- Create: `tests/test_graph_io.py`
- Modify: `tests/test_context.py`
- Modify: `tests/test_mcp_server.py`
- Modify: `tests/test_hardening.py`

- [ ] **Step 1: Write red boundary tests**

Test one reusable loader that:

- Accepts a contained regular graph file under the selected root.
- Enforces a 128 MiB default cap before allocation and a bounded read after the size check.
- Rejects symlinks/reparse points, non-files, root crossings, replacement during read, invalid UTF-8, invalid JSON, and invalid graph bundles.
- Returns fixed error categories without raw parser text, absolute paths, or graph content.
- Calls `validate_graph_bundle()` before `graph_from_json()`.
- Is used by CLI query, impact, context, and MCP `_load` paths.

- [ ] **Step 2: Demonstrate the red state**

```powershell
python -B -m pytest -q tests/test_graph_io.py tests/test_context.py tests/test_mcp_server.py -k "bounded or validated or oversized or symlink"
```

- [ ] **Step 3: Implement `load_validated_graph_bundle()`**

The loader returns a validated bundle plus graph object or raises a fixed `GraphReadError(code)` from an allowlist. Read bytes through an open descriptor, compare identity before/after, decode strictly, parse once, validate once, and build the graph only after validation.

Keep review's stricter selected-root/custom-graph policy intact; refactor it to the shared primitive only if its existing contract remains unchanged.

- [ ] **Step 4: Replace direct consumers**

Update `cmd_query`, `cmd_impact`, `cmd_context`, and `GraphiteMCPServer._load`. Preserve current successful output formats. Sanitize all errors and cap MCP reload attempts.

- [ ] **Step 5: Run focused tests and security regressions**

```powershell
python -B -m pytest -q tests/test_graph_io.py tests/test_context.py tests/test_mcp_server.py tests/test_hardening.py tests/test_review.py
python -B -m ruff check src/graphite/graph_io.py src/graphite/cli.py src/graphite/mcp_server.py tests/test_graph_io.py
```

- [ ] **Step 6: Commit**

```powershell
git add src/graphite/graph_io.py src/graphite/cli.py src/graphite/mcp_server.py tests/test_graph_io.py tests/test_context.py tests/test_mcp_server.py tests/test_hardening.py
git commit -m "fix: validate and bound direct graph reads"
```

---

## Task 3: Define immutable routing contracts and settings

**Files:**
- Create: `src/graphite/routing/__init__.py`
- Create: `src/graphite/routing/contracts.py`
- Create: `src/graphite/routing/settings.py`
- Create: `tests/test_routing_contracts.py`
- Modify: `src/graphite/config.py`

- [ ] **Step 1: Write red serialization and validation tests**

Define exact `StrEnum` values for task category, risk tier, effort, evidence provenance, execution outcome, and fixed failure reason. Test:

- Frozen dataclasses reject mutation.
- `to_dict()` emits a stable key order and only JSON-safe primitives.
- Absolute paths, parent traversal, NULs, unknown enum values, negative budgets, non-finite scores, excessive strings/lists, and booleans passed as integers are rejected.
- Public records never contain raw exceptions, credentials, prompts, source, response bodies, or repository absolute paths.
- Routing settings use `GRAPHITE_ROUTE_*`, never `GRAPHITE_LLM_*`.

- [ ] **Step 2: Demonstrate the red state**

Run: `python -B -m pytest -q tests/test_routing_contracts.py`

- [ ] **Step 3: Implement contracts**

Create the approved records: `TaskRequest`, `TaskProfile`, `ModelProfile`, `RoutingDecision`, `ApprovalManifest`, `ExecutionReceipt`, and `VerifiedOutcome`. Add explicit maximums for objective length, target count, reason count, alternative count, and outbound item count.

Public serialization uses fixed reason codes and relative paths only. Sensitive internal structures stay private and are not returned from `to_dict()`.

- [ ] **Step 4: Implement isolated settings**

Create `RoutingSettings` with conservative defaults for context bytes, input/output tokens, request timeout, concurrency, repository quota, machine quota, shadow rate, shadow quota, approval TTL, retention, and aggregate opt-in. Parse environment values with bounded numeric helpers and reject invalid security-sensitive values rather than silently widening limits.

Add only a `routing: RoutingSettings` bridge or equivalent factory to the CLI configuration path; do not add routing secrets to `Config.to_dict()`.

- [ ] **Step 5: Run tests and lint**

```powershell
python -B -m pytest -q tests/test_routing_contracts.py tests/test_cli.py
python -B -m ruff check src/graphite/routing src/graphite/config.py tests/test_routing_contracts.py
```

- [ ] **Step 6: Commit**

```powershell
git add src/graphite/routing src/graphite/config.py tests/test_routing_contracts.py tests/test_cli.py
git commit -m "feat: define development routing contracts"
```

---

## Task 4: Build repository-local and sanitized aggregate storage

**Files:**
- Create: `src/graphite/routing/storage.py`
- Create: `tests/test_routing_storage.py`
- Modify: `src/graphite/bootstrap.py`
- Modify: `.gitignore`
- Modify: `tests/test_bootstrap.py`

- [ ] **Step 1: Write red storage tests**

Cover:

- Repository database is exactly `.graphite/routing/events.sqlite3` under the selected root.
- Machine aggregate uses the platform-local Graphite state directory and cannot be redirected into a selected repository.
- Parent directories and database files receive current-user-only permissions where supported.
- Schema creation and each migration are transactional and idempotent.
- WAL, foreign keys, busy timeout, bounded statement inputs, and integrity checks are enabled.
- Duplicate idempotency keys cannot create duplicate calls or receipts.
- Corruption and lock timeout return fixed categories without silently deleting/resetting data.
- Aggregate rows reject paths, repository names, symbols, prompts, responses, stable repository fingerprints, and free-form strings.
- Aggregate writes do not occur without explicit opt-in.
- Retention deletes expired evidence transactionally and recomputes confidence from retained admissible evidence.

- [ ] **Step 2: Demonstrate the red state**

Run: `python -B -m pytest -q tests/test_routing_storage.py tests/test_bootstrap.py -k "routing or graphite_directory"`

- [ ] **Step 3: Implement schema v1**

Use normalized tables for tasks, decisions, approvals, executions, outcomes, shadow comparisons, policy versions, and budget ledger entries. Store hashes for prompts/responses when needed for correlation, never their content by default.

All budget reservation and execution-record insertion occurs in one immediate transaction. Completion, cancellation, and expiry release or settle reservations idempotently.

- [ ] **Step 4: Add ignore/onboarding support**

Add `**/.graphite/` to Graphite's managed ignore entries and this repository's `.gitignore`. Preserve existing `.graphite` files if users already have them; bootstrap only appends missing ignore rules.

- [ ] **Step 5: Run tests and lint**

```powershell
python -B -m pytest -q tests/test_routing_storage.py tests/test_bootstrap.py
python -B -m ruff check src/graphite/routing/storage.py src/graphite/bootstrap.py tests/test_routing_storage.py
```

- [ ] **Step 6: Commit**

```powershell
git add src/graphite/routing/storage.py tests/test_routing_storage.py src/graphite/bootstrap.py tests/test_bootstrap.py .gitignore
git commit -m "feat: add isolated routing evidence storage"
```

---

## Task 5: Add the Ollama Cloud registry and tested effort profiles

**Files:**
- Create: `src/graphite/routing/registry.py`
- Create: `src/graphite/routing/effort.py`
- Create: `tests/test_routing_registry.py`
- Modify: `src/graphite/routing/storage.py`

- [ ] **Step 1: Verify current official Ollama contracts**

Before coding, record links and access dates for Ollama's local API model-list, chat, cloud-model, authentication, thinking/effort, and usage fields in the implementation notes. Use official Ollama documentation only. Do not infer an effort parameter from another provider.

- [ ] **Step 2: Write red registry tests**

Test:

- Bundled profiles use exact model names and explicit capability/effort maps.
- Unknown models and mutable aliases are ineligible until operator-approved evaluation metadata exists.
- Recommendation reads only a cached registry snapshot and makes no HTTP/process call.
- `route policy --refresh-models` is the only first-release discovery action; it requires explicit invocation and queries loopback only.
- Registry snapshots are size/count bounded, sanitized, timestamped, and expire conservatively.
- `route run` rechecks the exact model immediately before execution.
- Unsupported effort is ineligible rather than downgraded silently.
- Provisional profiles are restricted to approval-gated low/medium tasks and cannot contribute autonomy eligibility until evaluated.

- [ ] **Step 3: Implement model profiles**

Ship conservative, versioned capability profiles only for exact Ollama Cloud identifiers verified during implementation. At minimum evaluate the locally available `kimi-k2.7-code:cloud`, `kimi-k2.6:cloud`, and `glm-5:cloud`; do not add a `glm-5.2` Ollama profile unless the exact identifier is available and verified.

Represent normalized effort separately from the provider payload. Each map entry includes the exact supported request fragment and evidence source/version. A model with no verified effort control exposes only `default`.

- [ ] **Step 4: Implement bounded loopback inventory refresh**

Use standard-library HTTP with redirects disabled. Permit only canonical loopback hosts and the fixed Ollama port configured within a narrow allowlist. Cap status line, headers, body, model count, and string lengths. Store the sanitized snapshot locally; never store authentication material.

- [ ] **Step 5: Run tests and lint**

```powershell
python -B -m pytest -q tests/test_routing_registry.py tests/test_routing_storage.py
python -B -m ruff check src/graphite/routing/registry.py src/graphite/routing/effort.py tests/test_routing_registry.py
```

- [ ] **Step 6: Commit**

```powershell
git add src/graphite/routing/registry.py src/graphite/routing/effort.py src/graphite/routing/storage.py tests/test_routing_registry.py
git commit -m "feat: add verified Ollama routing profiles"
```

---

## Task 6: Implement deterministic task classification and bounded context

**Files:**
- Create: `src/graphite/routing/classifier.py`
- Create: `src/graphite/routing/context_builder.py`
- Create: `tests/test_routing_classifier.py`
- Create: `tests/test_routing_context.py`
- Create: `tests/test_routing_security.py`

- [ ] **Step 1: Write red classifier tests**

Use table-driven fixtures for documentation, isolated code, feature, refactor, architecture, authentication, authorization, tenant isolation, migration, deployment, infrastructure, concurrency, financial, legal, and unknown tasks.

Prove:

- A user hint can raise but never lower risk.
- Unknown or conflicting evidence selects the safer tier.
- File names alone do not lower risk when graph/path/content-category evidence indicates high risk.
- Impact radius, reverse dependencies, community crossings, and test proximity are bounded and deterministic.
- High-risk categories always require approval and manual frontier-handoff eligibility.

- [ ] **Step 2: Write red context/security tests**

Cover containment, symlinks/reparse points, descriptor replacement, traversal, NULs, absolute paths, excessive files/bytes, binary files, generated artifacts, `.env*`, keys/certificates, credential patterns, configured exclusions, graph artifacts, `.git`, `.graphite`, caches, and ambiguous encodings.

The outbound manifest contains relative path, byte count, content hash, selection reason, and redaction/exclusion count. It never contains excluded content or an absolute path.

- [ ] **Step 3: Demonstrate the red state**

```powershell
python -B -m pytest -q tests/test_routing_classifier.py tests/test_routing_context.py tests/test_routing_security.py
```

- [ ] **Step 4: Implement classification**

Use fixed rules and bounded feature extraction from a fresh validated graph. Keep categories and risk rules data-driven in immutable tables. Do not use an LLM or stored model output for classification.

- [ ] **Step 5: Implement context construction**

Start from explicit targets, add bounded Graphite dependency/impact neighbors, then include only contained regular text files that pass exclusion and sensitive-content checks. Read through descriptors with identity revalidation. Sort deterministically before applying byte/file caps.

Return a private payload plus public manifest. Only the private payload may contain source, and it exists in memory only until execution completes.

- [ ] **Step 6: Run tests and lint**

```powershell
python -B -m pytest -q tests/test_routing_classifier.py tests/test_routing_context.py tests/test_routing_security.py tests/test_hardening.py
python -B -m ruff check src/graphite/routing/classifier.py src/graphite/routing/context_builder.py tests/test_routing_classifier.py tests/test_routing_context.py tests/test_routing_security.py
```

- [ ] **Step 7: Commit**

```powershell
git add src/graphite/routing/classifier.py src/graphite/routing/context_builder.py tests/test_routing_classifier.py tests/test_routing_context.py tests/test_routing_security.py
git commit -m "feat: classify routing risk and bound context"
```

---

## Task 7: Implement deterministic policy scoring and confidence

**Files:**
- Create: `src/graphite/routing/policy.py`
- Create: `tests/test_routing_policy.py`

- [ ] **Step 1: Write red eligibility and scoring tests**

Test hard gates for graph validity/freshness, registry expiry, model availability, capability, task evaluation status, context fit, effort support, data policy, budget, and risk.

Test deterministic ranking for:

- Cold start with provisional profiles.
- Repository evidence overriding sanitized global priors.
- Expected retry/escalation cost changing the cheapest choice.
- Latency and quota scarcity changing rankings.
- Stable tie breaking by exact model and effort identifiers.
- Non-finite/negative inputs failing closed.
- No eligible Ollama configuration producing a manual Claude/Codex handoff decision.

- [ ] **Step 2: Write red Wilson-confidence tests**

Implement known-vector tests for a two-sided 95% Wilson lower bound. Verify low-risk eligibility requires at least 50 admissible machine-verified outcomes and lower bound >= 0.90; medium requires 100 and >= 0.95; high is never eligible. One severe failure starts a new evidence window only after recorded incident review.

- [ ] **Step 3: Demonstrate the red state**

Run: `python -B -m pytest -q tests/test_routing_policy.py`

- [ ] **Step 4: Implement policy v1**

Use integer/fixed-point or `decimal.Decimal` internal weights so results are stable across hosts. Clamp every component before combination. Emit component scores and fixed reasons without leaking private features.

The policy may update calibrated success estimates from admissible evidence, but its rules, weights, risk gates, and capability profiles change only through a versioned policy record.

- [ ] **Step 5: Run tests and lint**

```powershell
python -B -m pytest -q tests/test_routing_policy.py tests/test_routing_contracts.py
python -B -m ruff check src/graphite/routing/policy.py tests/test_routing_policy.py
```

- [ ] **Step 6: Commit**

```powershell
git add src/graphite/routing/policy.py tests/test_routing_policy.py
git commit -m "feat: score development routing decisions"
```

---

## Task 8: Add single-use approval and atomic budget reservation

**Files:**
- Create: `src/graphite/routing/approval.py`
- Create: `tests/test_routing_approval.py`
- Modify: `src/graphite/routing/storage.py`
- Modify: `src/graphite/routing/contracts.py`

- [ ] **Step 1: Write red approval tests**

Cover:

- Prompt defaults to No and accepts only explicit case-insensitive `y`/`yes`.
- Empty input, EOF, malformed input, redirected streams, JSON mode, CI, and `--yes` decline without execution.
- Approval is bound to task, graph engine/repository hashes, context manifest, model, effort, budgets, policy, and expiry.
- Changed evidence, changed registry, changed model, changed effort, changed context, expired TTL, reuse, concurrent consume, and database replacement fail closed.
- Repository and machine quota reservation is atomic with approval consumption.
- Primary and shadow calls use independent approvals and reservations.
- Public approval output contains no HMAC key, source, prompt, response, or absolute path.

- [ ] **Step 2: Demonstrate the red state**

Run: `python -B -m pytest -q tests/test_routing_approval.py tests/test_routing_storage.py`

- [ ] **Step 3: Implement approval integrity**

Create a machine-local HMAC key with current-user-only permissions in the platform state directory. Sign canonical JSON with HMAC-SHA256. Store only nonce hash, manifest hash, status, expiry, and budget reservation in SQLite. Compare signatures with `hmac.compare_digest`.

Treat same-user modification as a local trust limitation; signatures protect accidental/cross-file tampering, not a hostile process running as the same account.

- [ ] **Step 4: Implement atomic consume/settle**

Use `BEGIN IMMEDIATE` to consume a nonce and reserve quota exactly once. Settle actual usage or release on pre-launch failure. A timeout/ambiguous provider outcome settles the reserved maximum unless usage can be proven safely.

- [ ] **Step 5: Run tests and lint**

```powershell
python -B -m pytest -q tests/test_routing_approval.py tests/test_routing_storage.py
python -B -m ruff check src/graphite/routing/approval.py src/graphite/routing/storage.py tests/test_routing_approval.py
```

- [ ] **Step 6: Commit**

```powershell
git add src/graphite/routing/approval.py src/graphite/routing/storage.py src/graphite/routing/contracts.py tests/test_routing_approval.py tests/test_routing_storage.py
git commit -m "feat: require single-use routing approval"
```

---

## Task 9: Implement the bounded loopback Ollama executor

**Files:**
- Create: `src/graphite/routing/ollama_executor.py`
- Create: `tests/test_routing_executor.py`
- Modify: `tests/test_routing_security.py`

- [ ] **Step 1: Write red fake-server contract tests**

The fake server must exercise:

- Exact loopback URL and fixed `/api/chat` path.
- Redirect rejection without forwarding context.
- DNS names, userinfo, fragments, alternate schemes, non-loopback IPs, and unapproved ports rejected.
- Exact allowlisted model and verified effort payload.
- Non-streaming fixed request shape, bounded headers/body, and no ambient credentials.
- Revalidation of model inventory and approval immediately before launch.
- Timeouts, cancellation, connection failure, HTTP errors, invalid UTF-8/JSON/schema, excessive headers/body, truncated output, and token overflow.
- Sanitized fixed errors with no response body, prompt, source, model output, credentials, or absolute paths.
- No automatic retry, fallback, model pull, shell, or filesystem mutation.

- [ ] **Step 2: Demonstrate the red state**

Run: `python -B -m pytest -q tests/test_routing_executor.py tests/test_routing_security.py -k "ollama or executor or redirect or response"`

- [ ] **Step 3: Implement the transport**

Use a no-redirect `urllib` opener and a total monotonic deadline shared across model revalidation and chat. Bound request bytes before connection. Read response incrementally to a configured maximum and parse once.

The prompt contains a fixed system contract, task objective, approved context manifest, and approved private context. It instructs the model to return analysis or suggested changes as text and explicitly states that the model has no execution authority.

- [ ] **Step 4: Return a sanitized receipt and ephemeral result**

Return model text to the immediate caller but store only its SHA-256 and bounded metadata by default. Normalize provider usage fields defensively. Unknown/missing usage settles the reserved maximum.

- [ ] **Step 5: Run tests and lint**

```powershell
python -B -m pytest -q tests/test_routing_executor.py tests/test_routing_security.py tests/test_llm.py
python -B -m ruff check src/graphite/routing/ollama_executor.py tests/test_routing_executor.py tests/test_routing_security.py
```

- [ ] **Step 6: Commit**

```powershell
git add src/graphite/routing/ollama_executor.py tests/test_routing_executor.py tests/test_routing_security.py
git commit -m "feat: execute approved Ollama routing calls"
```

---

## Task 10: Record outcomes, sanitize aggregates, and calculate promotion eligibility

**Files:**
- Create: `src/graphite/routing/telemetry.py`
- Create: `tests/test_routing_telemetry.py`
- Modify: `src/graphite/routing/storage.py`
- Modify: `src/graphite/routing/policy.py`

- [ ] **Step 1: Write red outcome-provenance tests**

Test machine-verified, CI-imported, human, pairwise, reversion, and ambiguous evidence. Prove:

- Machine/CI evidence must correlate to task, repository state, decision, and execution.
- Human evidence is retained but weighted separately.
- Missing provenance is excluded from confidence/promotion.
- Severe failure categories reset eligibility only after explicit incident-review closure starts a new evidence window.
- A reversion can invalidate prior success without rewriting history.
- Recommendation estimates update from admissible evidence, but risk/authority do not change.

- [ ] **Step 2: Write red aggregate-sanitization tests**

Fuzz bounded strings containing repository names, paths, emails, symbols, secrets, UUIDs, hashes, prompts, and response fragments. The aggregate encoder must emit only enums, approved model/effort identifiers, booleans, bounded integers, coarse buckets, and version identifiers.

Prove opt-out creates no machine-wide file or row.

- [ ] **Step 3: Demonstrate the red state**

Run: `python -B -m pytest -q tests/test_routing_telemetry.py tests/test_routing_policy.py`

- [ ] **Step 4: Implement append-only outcome events and derived views**

Never update historical outcome content in place. Append corrections/reversions and derive current admissible evidence transactionally. Rebuild recommendation statistics from retained events after retention or schema migration.

- [ ] **Step 5: Implement sanitized aggregate export**

Construct aggregate rows from typed fields, not by redacting serialized detailed rows. Reject any unexpected string field. Machine priors are advisory and cannot identify a repository.

- [ ] **Step 6: Run tests and lint**

```powershell
python -B -m pytest -q tests/test_routing_telemetry.py tests/test_routing_policy.py tests/test_routing_storage.py
python -B -m ruff check src/graphite/routing/telemetry.py tests/test_routing_telemetry.py
```

- [ ] **Step 7: Commit**

```powershell
git add src/graphite/routing/telemetry.py src/graphite/routing/storage.py src/graphite/routing/policy.py tests/test_routing_telemetry.py tests/test_routing_policy.py tests/test_routing_storage.py
git commit -m "feat: learn from verified routing outcomes"
```

---

## Task 11: Add explicitly budgeted shadow evaluation

**Files:**
- Create: `src/graphite/routing/shadow.py`
- Create: `tests/test_routing_shadow.py`
- Modify: `src/graphite/routing/approval.py`
- Modify: `src/graphite/routing/storage.py`

- [ ] **Step 1: Write red shadow-policy tests**

Cover:

- Default disabled without explicit opt-in.
- Configurable rate constrained to 0-10% in the first release.
- Only low/medium risk, never sensitive/high-risk categories.
- Cryptographically random selection cannot exceed a transactional rolling budget.
- Alternative differs materially in model or effort and remains independently eligible.
- Independent approval, nonce, quota reservation, timeout, and receipt.
- Shadow output cannot replace the primary result or count as machine-verified without controlled benchmark execution.
- Human pairwise outcomes are blinded to model identity until the verdict is recorded.

- [ ] **Step 2: Demonstrate the red state**

Run: `python -B -m pytest -q tests/test_routing_shadow.py tests/test_routing_approval.py`

- [ ] **Step 3: Implement shadow selection and accounting**

Select only after primary approval and before execution so the operator sees the incremental quota. A declined shadow leaves the primary approval usable. A failed shadow never changes primary success/failure.

- [ ] **Step 4: Implement pairwise evidence contract**

Store hashes and blinded labels locally. Reveal model identities only after verdict persistence. Exclude pairwise evidence from autonomy confidence until a later validated evaluator explicitly promotes its provenance class.

- [ ] **Step 5: Run tests and lint**

```powershell
python -B -m pytest -q tests/test_routing_shadow.py tests/test_routing_approval.py tests/test_routing_storage.py
python -B -m ruff check src/graphite/routing/shadow.py tests/test_routing_shadow.py
```

- [ ] **Step 6: Commit**

```powershell
git add src/graphite/routing/shadow.py src/graphite/routing/approval.py src/graphite/routing/storage.py tests/test_routing_shadow.py tests/test_routing_approval.py
git commit -m "feat: add budgeted routing shadow evaluation"
```

---

## Task 12: Orchestrate the service and CLI without expanding authority

**Files:**
- Create: `src/graphite/routing/service.py`
- Create: `tests/test_routing_cli.py`
- Modify: `src/graphite/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write red CLI grammar and authority tests**

Lock these commands:

```text
graphite route recommend PATH --objective TEXT [--target RELPATH ...] [--json]
graphite route run PATH --objective TEXT [--target RELPATH ...] [--shadow] [--json]
graphite route record-outcome PATH --execution-id ID --provenance TYPE ...
graphite route status PATH [--json]
graphite route policy PATH [--refresh-models] [--promote VERSION] [--rollback VERSION] [--json]
```

Test:

- `recommend` never opens HTTP, prompts, writes approvals, or executes a model.
- `run` performs classification/recommendation first, prints the outbound manifest and cost/quota estimate, then prompts exactly once for the primary call.
- JSON/non-interactive/CI/redirected modes never prompt or call a model.
- `--yes` does not grant routing consent.
- Shadow requires a separate explicit prompt and budget display.
- Fixed exit codes distinguish success, declined, handoff, blocked evidence, provider failure, and invalid input.
- Text/JSON outputs never contain source, prompt, raw response in status, secrets, database paths, or absolute host paths.
- `record-outcome` cannot claim machine verification without a supported evidence import.
- `policy --promote` changes recommendation policy only; no command grants autonomous execution.

- [ ] **Step 2: Demonstrate the red state**

Run: `python -B -m pytest -q tests/test_routing_cli.py tests/test_cli.py -k "route or routing"`

- [ ] **Step 3: Implement service orchestration**

The service performs: trusted graph load -> freshness -> classification -> context manifest -> registry snapshot -> policy -> public recommendation. The execution path then obtains approval, reconstructs/revalidates private context, rechecks model/effort/budget, atomically consumes approval, invokes Ollama, and records the receipt.

No CLI function imports provider transport directly; it calls the service boundary.

- [ ] **Step 4: Implement manual frontier handoff**

When no Ollama option is eligible, return a stable handoff result with `recommended_channels: ["claude_code", "codex"]`, reasons, and verification requirements. Do not detect credentials, inspect subscription data, or launch either CLI.

- [ ] **Step 5: Run CLI and no-authority regressions**

```powershell
python -B -m pytest -q tests/test_routing_cli.py tests/test_cli.py tests/test_typescript_activation.py tests/test_llm.py
python -B -m ruff check src/graphite/routing/service.py src/graphite/cli.py tests/test_routing_cli.py
```

- [ ] **Step 6: Commit**

```powershell
git add src/graphite/routing/service.py src/graphite/cli.py tests/test_routing_cli.py tests/test_cli.py
git commit -m "feat: add approval-gated route commands"
```

---

## Task 13: Document and diagnose the routing boundary

**Files:**
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`
- Modify: `CONTRIBUTING.md`
- Modify: `tests/test_documentation.py`
- Modify: `src/graphite/doctor.py` or existing readiness modules only if a read-only routing check fits their current boundary
- Modify: `tests/test_doctor.py` only when doctor changes

- [ ] **Step 1: Write red documentation contract tests**

Require documentation to state:

- Ollama Cloud only for development execution.
- OpenRouter reserved for later production in-application inference.
- Claude/Codex manual handoff only.
- Approval defaults No and non-interactive modes cannot execute.
- Source context leaves the machine only after an outbound manifest and approval.
- Detailed versus sanitized aggregate storage boundaries.
- Shadow consent/budget behaviour.
- High-risk permanent approval gate.
- Model output is untrusted and cannot mutate code.
- Exact operator commands, retention controls, reset/rollback, and incident response.

- [ ] **Step 2: Add a read-only readiness check if justified**

Doctor may report routing as optional with fixed statuses for local registry snapshot, storage availability, Ollama loopback reachability only under `--deep`, and policy readiness. Fast doctor must not contact Ollama, create databases, or expose model/repository detail.

- [ ] **Step 3: Update architecture and operator docs**

Document trust zones, data flow, limits, reason codes, storage, quotas, approval, manual handoff, shadow evidence limitations, and future autonomy thresholds. Do not advertise measured savings before benchmark evidence exists.

- [ ] **Step 4: Run documentation/readiness tests**

```powershell
python -B -m pytest -q tests/test_documentation.py tests/test_doctor.py
python -B -m ruff check src/graphite/doctor.py tests/test_documentation.py tests/test_doctor.py
```

- [ ] **Step 5: Commit**

```powershell
git add README.md ARCHITECTURE.md CONTRIBUTING.md tests/test_documentation.py src/graphite/doctor.py tests/test_doctor.py
git commit -m "docs: explain adaptive development routing"
```

Stage only files actually changed; omit doctor files if no readiness change was justified.

---

## Task 14: Build the reproducible Realty routing benchmark

**Files:**
- Create: `benchmarks/realty_router/README.md`
- Create: `benchmarks/realty_router/tasks.json`
- Create: `benchmarks/realty_router/evaluate.py`
- Create: `tests/test_routing_benchmark.py`

- [ ] **Step 1: Define a versioned, non-proprietary task corpus**

Include bounded synthetic tasks across:

- Documentation and schema work.
- Listing CRUD and media metadata.
- Search/filter/geospatial design.
- Lead, enquiry, favourite, and appointment workflows.
- Multi-tenant authorization and isolation.
- Database migrations and rollback.
- Queues, idempotency, notifications, and retries.
- Deployment, observability, backup, and recovery.
- Accessibility, performance, and security review.

Fixtures contain no customer data, credentials, licensed MLS/IDX content, or copied proprietary repositories.

- [ ] **Step 2: Write red benchmark-schema tests**

Validate stable task IDs, category/risk labels, expected targets, allowed evidence, verification rubric, maximum context, and absence of secrets/absolute paths. The evaluator must reject duplicate IDs, unknown fields, missing verification, and mutable model aliases.

- [ ] **Step 3: Implement offline evaluation first**

`evaluate.py` consumes previously captured, explicitly supplied result records and calculates completed-task cost equivalent, latency, acceptance, repair, escalation, severe failure, and Wilson confidence. It performs no provider call by default.

An explicit `--live --approve-cost` mode may call the already implemented routing service, remains outside CI, uses hard task/run/quota caps, and records the exact policy/model/profile versions.

- [ ] **Step 4: Compare policies without overstating causality**

Support labeled result sets for frontier-only, native automatic, and Graphite-routed policies. Report sample sizes and confidence intervals. Do not claim schedule or quality improvement when evidence is insufficient.

- [ ] **Step 5: Run benchmark tests and lint**

```powershell
python -B -m pytest -q tests/test_routing_benchmark.py
python -B -m ruff check benchmarks/realty_router tests/test_routing_benchmark.py
```

- [ ] **Step 6: Commit**

```powershell
git add benchmarks/realty_router tests/test_routing_benchmark.py
git commit -m "test: add Realty routing benchmark"
```

---

## Task 15: Complete acceptance, graph refresh, and release evidence

**Files:**
- Modify: `docs/superpowers/specs/2026-07-14-adaptive-development-router-design.md`
- Modify: this plan only to check completed boxes during execution

- [ ] **Step 1: Run focused routing suites**

```powershell
python -B -m pytest -q tests/test_engine_identity.py tests/test_graph_io.py tests/test_routing_contracts.py tests/test_routing_storage.py tests/test_routing_registry.py tests/test_routing_classifier.py tests/test_routing_context.py tests/test_routing_policy.py tests/test_routing_approval.py tests/test_routing_executor.py tests/test_routing_telemetry.py tests/test_routing_shadow.py tests/test_routing_cli.py tests/test_routing_security.py tests/test_routing_benchmark.py
```

- [ ] **Step 2: Run authority-boundary regressions**

```powershell
python -B -m pytest -q tests/test_typescript_activation.py tests/test_llm.py tests/test_doctor.py tests/test_mcp_server.py tests/test_review.py tests/test_hardening.py tests/test_bootstrap.py tests/test_init.py
```

- [ ] **Step 3: Run the controlled full suite and static checks**

```powershell
python -B -m pytest -q --basetemp F:\tmp\graphite-router-acceptance
python -B -m ruff check .
git diff --check
```

Expected: all tests pass; skips must be explained and must not include routing security/authority tests.

- [ ] **Step 4: Prove no-network and no-mutation defaults**

Run recommendation, JSON, CI, redirected, declined, expired-approval, unavailable-storage, stale-graph, unsupported-effort, and manual-handoff cases under tests that fail on any unexpected socket, subprocess, or repository write.

- [ ] **Step 5: Run an explicitly approved bounded Ollama smoke test**

Only after the package/model profile and cost/quota display are reviewed, execute one low-risk synthetic benchmark task with the selected Ollama Cloud model. Do not use repository secrets or production code. Record model identity, effort, usage, latency, receipt, and human verdict. If approval is declined or Ollama is unavailable, record the smoke test as an external readiness limitation; do not weaken offline acceptance.

- [ ] **Step 6: Rebuild and validate Graphite's own graph**

```powershell
python -m graphite build .
python -m graphite check .
python -m graphite validate --graph-json graph-out/graph.json --json
```

Expected: fresh graph with zero validation errors and warnings.

- [ ] **Step 7: Audit public artifacts for sensitive data**

Search generated reports, JSON fixtures, logs, and test artifacts for absolute workspace paths, credentials, raw prompts, source payloads, response bodies, database paths, and user identifiers. Remove test artifacts; do not delete user files.

- [ ] **Step 8: Mark the design implemented and commit acceptance evidence**

Update the design status to `Implemented` only when all mandatory offline acceptance passes. List the real Ollama smoke-test state separately so unavailable optional cloud access cannot be confused with core correctness.

```powershell
git add docs/superpowers/specs/2026-07-14-adaptive-development-router-design.md docs/superpowers/plans/2026-07-14-adaptive-development-router.md
git commit -m "docs: mark adaptive development router implemented"
```

- [ ] **Step 9: Review branch history and integrate safely**

```powershell
git status --short --branch
git log --oneline --decorate --max-count 20
git diff main...HEAD --stat
git diff main...HEAD --check
```

Reconcile the independently owned TypeScript resolver changes before merging. Do not use destructive reset/checkout commands. Push only after explicit user direction or an already active push instruction.

---

## Implementation completion criteria

The initiative is complete only when:

1. Engine changes make existing graphs stale.
2. Every direct graph consumer uses the bounded validated loader.
3. Recommendation is deterministic, read-only, and offline.
4. Execution is Ollama-Cloud-only, loopback-only, default-No, approval-bound, quota-bound, and audited.
5. No router path can mutate source, execute shell commands, install packages/models, invoke OpenRouter, or launch Claude/Codex.
6. Context is contained, bounded, sensitive-file filtered, manifested, and revalidated before transmission.
7. Detailed telemetry remains repository-local and sanitized aggregate learning is opt-in.
8. Shadow evaluation is separately approved, bounded, and excluded from high-risk tasks.
9. Promotion eligibility follows the approved Wilson thresholds and cannot grant authority.
10. The Realty benchmark is reproducible and reports uncertainty rather than unverified savings claims.
11. Full tests, Ruff, graph freshness, graph validation, documentation tests, and sensitive-data audit pass.
12. Unrelated resolver changes are preserved and reconciled separately.
