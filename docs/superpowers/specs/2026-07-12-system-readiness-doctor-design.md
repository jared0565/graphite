# System Readiness and Doctor Design

**Date:** 2026-07-12
**Status:** Implemented and verified
**Scope:** Core reliability, operational health semantics, and optional-integration readiness

## Objective

Make Graphite reliably operable through its normal command paths and give users one safe, built-in way to understand and activate optional capabilities. Preserve the local-first, deterministic, model-agnostic core: MCP, TypeScript compiler resolution, and LLM connectivity remain optional and disabled unless explicitly selected.

## Current Findings

The repository and installed system have a strong baseline:

- The editable Graphite 0.1.0 installation imports from the repository.
- The `graphite` and `graphite-mcp` command shims are on `PATH`.
- The full test suite and Ruff checks pass.
- The current graph validates with 2,053 nodes, 3,823 edges, and zero validation warnings.
- Deterministic graph queries work.
- The MCP dependency and server module import successfully.
- Node.js is installed.
- The Graphite daemon and Windows startup launcher are installed and running.

The remaining gaps are:

1. `python -m graphite check .` reproducibly fails during hardened Git enumeration while `python -B -m graphite check .` succeeds and reports the graph fresh. Normal and no-bytecode invocation must behave identically.
2. Daemon health treats a non-zero cumulative `failure_count` as an active failure even after `last_error` has cleared and a later build has succeeded.
3. Optional integration readiness requires several unrelated manual checks and has no single structured diagnostic surface.
4. TypeScript compiler resolution is unavailable when a target project does not contain a resolvable `typescript` package; fallback works, but activation guidance is fragmented.
5. MCP imports and automated tests exist, but system readiness is not exposed through a bounded user-facing protocol probe.
6. LLM configuration can be inherited from ambient process state even while LLM mode is disabled. A credential was exposed during diagnostics and must be treated as compromised.

The credential is present only in the current process environment, not in Windows user- or machine-scoped environment variables. A child process cannot remove it from its parent. Provider-side revocation and removal from the configuration that launches the parent process are required operational actions.

## Decision

Implement a staged hardening approach centered on a new built-in `graphite doctor` command:

- Fix the two core reliability defects first.
- Add fast, read-only readiness checks with structured output.
- Add explicitly requested, timeout-bounded deep probes for functional verification.
- Keep optional capabilities optional and make their activation discoverable.
- Never install optional dependencies automatically.
- Never display credential values or perform an LLM network request without explicit consent in the command invocation.

## Alternatives Considered

### Minimal defect patch and documentation only

This would fix the immediate CLI and daemon issues with the smallest code change. It would leave activation and system diagnosis fragmented across help text, imports, package-manager commands, daemon status, and provider-specific configuration. Rejected because the user explicitly wants optional capabilities to be easy to activate for later testing.

### Bundle TypeScript and model tooling

Bundling optional tooling would make some checks immediately available, but it would add cross-ecosystem dependencies, increase supply-chain and maintenance risk, and weaken Graphite's zero-LLM/model-agnostic contract. Rejected.

### Selected: core fixes plus opt-in doctor probes

This creates one stable operational interface without making optional integrations mandatory. It also makes absent tooling an explicit, non-failing state and keeps network/credential use behind a deliberate flag.

## Command Interface

### Fast doctor

```text
graphite doctor [PATH] [--json]
```

The default path is the current directory. The fast command is read-only and does not contact model providers. It reports independent checks for:

- Graphite import/version and command entry point.
- Python version compatibility.
- Hardened Git enumeration for the selected repository.
- Existing graph artifact presence, validation, and freshness.
- Daemon status, process, startup launcher, and selected-project health.
- MCP dependency and command availability.
- Node.js and project-local TypeScript package availability.
- LLM mode/provider configuration presence without revealing secret values.

### Deep doctor

```text
graphite doctor [PATH] --deep [--include-llm] [--json]
```

Deep mode adds functional probes:

- A temporary deterministic scan, build, validate, and query pipeline using synthetic files.
- An MCP stdio initialize/list-tools exchange.
- A TypeScript compiler-backed synthetic resolution probe only when project-local TypeScript is available.
- An LLM connectivity probe only when both `--deep` and `--include-llm` are present.

The LLM probe sends synthetic content only. It must not send repository source, graph data, filenames, Git metadata, or secrets.

### Exit behavior

Each check returns one of:

- `ready`: the capability passed.
- `optional`: the capability is absent or disabled by design and the core remains usable.
- `degraded`: the capability is configured but partially unhealthy or its deep check was not requested.
- `blocked`: a core capability failed or a selected deep probe could not complete safely.

The command exits non-zero only when at least one check is `blocked`. Optional or degraded integrations remain visible without making the model-agnostic core fail.

## Architecture and Components

### Core Git reliability

Reproduce the bytecode-sensitive Git enumeration failure in an integration test that launches the real module entry point with and without `-B`. Diagnose the actual child-process return code and failure source rather than adding retries or permanently recommending `-B`.

The fix must preserve the existing Git trust boundary:

- Canonical external Git executable.
- Argument-vector execution with `shell=False`.
- Isolated `GIT_*` environment.
- Disabled prompts and optional locks.
- Timeouts and output limits.
- Repository containment and normalized paths.

Normal and no-bytecode invocations must produce the same result on a clean repository.

### Daemon health semantics

`failure_count` remains cumulative telemetry. Active failure classification depends on current state:

- `last_error` present means failing.
- `needs_initial_build` means pending.
- No current error and a successful build means recovered, regardless of historical `failure_count`.
- Staleness is reported independently as a warning.

Status summaries, human-readable output, JSON output, and tests must use the same classification.

### Doctor orchestration

Add a focused doctor module with typed/structured check results and a small CLI adapter. Each checker owns one capability and returns data rather than printing directly. The orchestrator aggregates results, computes overall status, and renders text or JSON.

Checks must be independently understandable and testable. A failure in one optional checker must not prevent other checks from running.

### Deep-probe isolation

Deep probes use a newly created external temporary directory, not the selected repository. The probe verifies that its resolved path is within the operating system temporary root before cleanup.

Every subprocess uses:

- Argument vectors without a shell.
- Explicit timeouts.
- Bounded stdout/stderr capture.
- No interactive input.
- A minimal or sanitized environment appropriate to the probe.
- Typed, sanitized failure evidence.

No deep probe writes to the selected repository, its graph output, its cache, or its Git metadata.

## Optional Activation

### TypeScript

Graphite continues to use project-local TypeScript when available and heuristic resolution otherwise. Activation is performed in the target TypeScript project, not globally and not as a Graphite Python runtime dependency.

Before any installation, the required package validator must be run against the exact package name `typescript`. If validation succeeds, the user may add TypeScript as a development dependency using the target project's existing package manager. Doctor then reports the resolved compiler path/version and deep-probe result.

No package-manager command is executed automatically by Graphite.

### MCP

Activation uses the existing `mcp` Python extra. Documentation provides the validated installation command, command-shim check, fast doctor check, and bounded deep protocol probe. Doctor must distinguish:

- Python package missing.
- Command shim missing.
- Server launch failure.
- Protocol initialization failure.
- Tool-list failure.
- Ready.

### LLM

The default remains `llm_mode=none`. In disabled mode:

- Ambient credential presence is reported only as a boolean security warning.
- Credential values are never loaded into doctor output, logs, exceptions, or JSON.
- No network request occurs.

Preferred testing uses local Ollama without a credential. Cloud testing requires a newly rotated, session-scoped credential and explicit provider configuration. The connectivity probe additionally requires `--deep --include-llm`.

The exposed credential must be revoked in its provider dashboard. The replacement must not be persisted in repository files, command history, Windows user/machine environment, logs, fixtures, or generated artifacts.

## Security and Trust Boundaries

- Repository paths, graph artifacts, Git output, subprocess output, MCP messages, TypeScript output, and model responses are untrusted.
- Doctor JSON has a stable schema and contains no secret values, absolute credential locations, environment dumps, or raw provider responses.
- Errors are sanitized and bounded.
- The LLM probe sends a constant synthetic prompt and accepts no repository-derived input.
- MCP messages are framed and size-bounded; malformed, oversized, or unexpected responses fail only the MCP check.
- TypeScript discovery is limited to the selected project and trusted external executable boundaries already used by Graphite.
- Doctor never installs packages, modifies startup configuration, restarts the daemon, changes provider credentials, or writes into the selected repository.

## Error Handling

- Core Git or deterministic pipeline failure produces `blocked` with a stable issue code and safe remediation text.
- Missing optional MCP, TypeScript, or LLM tooling produces `optional` with activation guidance.
- Configured optional tooling that fails its deep probe produces `degraded`, unless the user explicitly selected that capability as a required deep check in a future extension.
- Timeouts and output-limit failures are distinct issue codes.
- Cleanup failure is reported without recursively deleting an unverified path.
- Credential presence is never proof of validity; connectivity success is never treated as authorization or model correctness.

## Testing Strategy

### Core reliability tests

- Launch `python -m graphite check` and `python -B -m graphite check` against the same temporary Git repository and assert equivalent successful results.
- Exercise Git executable discovery, environment isolation, output limits, timeouts, and unsafe repository paths.
- Prove a recovered daemon project with historical failures is healthy.
- Prove a current `last_error` remains failing and initial-build state remains pending.

### Doctor unit tests

- Stable JSON schema and deterministic check ordering.
- Overall status and exit-code calculation.
- Missing/available optional tooling.
- Secret redaction across configuration, errors, text, and JSON.
- No LLM request without both deep mode and explicit inclusion.
- No repository-derived data in the LLM probe.

### Deep integration tests

- Synthetic scan/build/validate/query succeeds in an external temporary directory.
- MCP initialize/list-tools succeeds and malformed, oversized, closed, and timed-out sessions fail safely.
- TypeScript compiler probe succeeds when a fake/project-local compiler is available and reports optional fallback otherwise.
- LLM tests use a local fake HTTP server; no live provider is required in the automated suite.

### System acceptance

- Ruff passes.
- Complete pytest suite passes.
- Normal and `-B` CLI freshness checks both pass.
- Fresh core build, validation, and query pass.
- `doctor` text and JSON modes report core ready.
- `doctor --deep` passes deterministic and MCP probes.
- Missing TypeScript and disabled LLM are `optional`, not blockers.
- Daemon health no longer classifies recovered projects as actively failing.
- Git worktree is clean.

## Operational Credential Remediation

The code change cannot revoke or remove a credential injected by the parent process. Completion therefore has two parts:

1. Graphite containment: disabled-mode key non-use, doctor redaction, synthetic-only opt-in probe, and tests.
2. Human/provider action: revoke the exposed key, remove it from the parent application's secret configuration, restart affected processes including the daemon if they inherited it, and create a session-scoped replacement only when cloud testing is required.

Acceptance evidence must never include the credential value.

## Acceptance Record

Offline repository verification passed. Recovered-daemon classification is verified, while live
daemon process observation remains permission-limited in the acceptance environment. The live LLM
probe was intentionally skipped pending credential rotation, whose completion remains unproven.

## Documentation

Update:

- `README.md` with doctor examples and optional activation paths.
- `CONTRIBUTING.md` with tests and package-validation expectations for doctor/integrations.
- `ARCHITECTURE.md` with doctor boundaries and deep-probe data flow.
- Relevant help text with remediation commands that do not reveal secrets.

## Out of Scope

- Automatically installing Python or Node packages.
- Bundling TypeScript with Graphite.
- Making an LLM provider, endpoint, SDK, or key mandatory.
- Rotating provider credentials on the user's behalf.
- Changing daemon installation/startup configuration automatically.
- Treating connectivity as proof of model quality or authorization.

## Acceptance Recommendation

Accept the implementation only when normal invocation parity, recovered-daemon classification, doctor security boundaries, optional activation guidance, and the complete test/lint suite all pass. Provider-side credential rotation remains an explicit operational prerequisite for future cloud LLM testing and cannot be satisfied by repository code alone.
