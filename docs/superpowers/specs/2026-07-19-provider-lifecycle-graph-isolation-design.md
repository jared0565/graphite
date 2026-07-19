# Provider Lifecycle and Canonical Graph Isolation Design

**Date:** 2026-07-19
**Status:** Approved for implementation planning
**Scope:** Provider-neutral runtime lifecycle management for Claude Code, Codex,
Ollama, and OpenRouter, with deterministic canonical graph isolation

## 1. Objective

Allow Graphite to use the currently installed or configured AI runtimes without
hard-coding individual patch versions, while ensuring that runtime updates cannot
silently inherit old execution authority. Provider discovery, compatibility, and
verification must remain independent from deterministic graph construction.

Graphite must continue to scan, build, validate, query, and analyze canonical
graphs when every AI provider is missing, updated, incompatible, unauthenticated,
or unavailable. Model inference remains an explicit, separately approved action.

## 2. Non-goals

- Automatically trusting an updated executable, endpoint, or model digest
- Automatically invoking inference after provider drift
- Automatic retries, fallbacks, provider switching, or model substitution
- Treating compatibility probes as capability verification
- Making the graph daemon a credential broker or inference orchestrator
- Allowing enrichment output to change canonical graph authority
- Promoting compatibility policies without explicit human approval

## 3. Chosen architecture

Graphite will add a central provider lifecycle registry with narrow provider probe
adapters. This registry is independent of graph construction and governed routing
execution.

The core contracts are:

- `ProviderRuntimeIdentity`: provider, runtime kind, version, executable or
  endpoint identity, immutable digest, observed capabilities, and observation time.
- `ProviderCompatibilityPolicy`: supported version ranges, required capabilities,
  update-classification rules, and policy version.
- `ProviderLifecycleState`: `discovered`, `compatible`,
  `verification_required`, `active`, `incompatible`, or `unavailable`.
- `ProviderLifecycleEvent`: append-only sanitized evidence for one state transition,
  including old and new identity digests and a stable reason code.

The registry determines compatibility state. Capability snapshots remain a
separate authority proving that one exact provider/model/profile passed a bounded,
explicitly approved verification. Execution approvals bind both the lifecycle
identity digest and the capability snapshot digest.

## 4. Provider probe adapters

Each adapter exposes only bounded, non-inference identity and capability probes.

### Claude Code and Codex

- Resolve and canonicalize the executable.
- Reject symlinks, reparse points, workspace-local executables, and invalid files.
- Calculate the executable SHA-256.
- Read and validate the semantic CLI version.
- Check required non-inference CLI flags and structured-output capabilities.
- Check credential health without retaining authentication payloads.

### Ollama

- Bind a canonical allowlisted endpoint.
- Observe the server version and required API capabilities.
- Bind each configured model tag to its immutable model digest.
- Treat a changed digest under the same tag as identity drift.
- Never use generation, chat, embedding, or tool endpoints for lifecycle probing.

### OpenRouter

- Require an approved canonical HTTPS endpoint.
- Bind configured model identifiers and provider-routing policy.
- Observe only bounded health or contract metadata that does not invoke inference.
- Record credential presence and health status without reading or retaining the
  credential value.
- Reject insecure transport, unexpected endpoints, and unapproved redirects.

All adapters return normalized records. Raw responses, diagnostics, auth payloads,
paths from provider output, and credential material are excluded.

## 5. Compatibility and identity changes

The registry classifies observed changes as follows:

- Same normalized identity: retain the existing lifecycle state.
- Hash-only or patch update: run the standard contract probes; if they pass,
  transition to `verification_required`.
- Minor update: run the expanded compatibility probe set; if it passes, transition
  to `verification_required`.
- Major update or missing required capability: transition to `incompatible` until
  the adapter policy is explicitly qualified and promoted.
- Missing executable, endpoint, model, or credential health: transition only that
  provider to `unavailable`.

Any identity change invalidates matching capability snapshots and pending
approvals. Successful non-inference compatibility probes never reactivate a
provider. Reactivation requires a new bounded model verification, a matching
identity, complete usage evidence, and explicit approval.

Compatibility policies use semantic ranges and required capabilities rather than
individual patch allowlists. Exact executable, endpoint, and model identities are
still bound per snapshot and per execution to prevent time-of-check/time-of-use
drift.

## 6. Lifecycle data flow and authority

1. Discovery resolves the configured executable, endpoint, and model identities.
2. The provider adapter performs bounded non-inference probes.
3. The registry validates and hashes the normalized identity.
4. The daemon compares the observation with the last stored identity.
5. The registry appends a sanitized transition event and updates lifecycle state.
6. If compatible verification is required, Graphite may prepare a manifest and
   notify the operator, but it stops before inference.
7. A separately approved verification may create a new capability snapshot and
   transition the exact matching identity to `active`.
8. Every routed execution rechecks the live identity against the lifecycle record,
   snapshot, and approval before authority is consumed.

The daemon improves detection latency but is not an authority dependency. Lazy
execution-time checks enforce the same identity rules if the daemon is stopped,
stale, or unavailable.

## 7. Daemon responsibilities and restrictions

The daemon may automatically:

- Hash approved local executables.
- Run bounded version, credential-health, and capability probes.
- Query allowlisted Ollama model manifests and OpenRouter contract metadata without
  inference.
- Record sanitized lifecycle transitions.
- Mark snapshots and pending approvals stale after identity drift.
- Prepare an unexecuted verification manifest and operator notification.

The daemon must not:

- Invoke generation, chat, embedding, tool, or other model inference.
- Save or accept a capability snapshot.
- Reactivate a provider.
- Retry, fall back, switch provider, or change model or effort.
- Promote a compatibility policy.
- Read or log credential contents.

Probe timeouts and temporary failures use bounded scheduling backoff. They do not
affect graph scheduling or trigger another provider.

## 8. Canonical graph isolation

Canonical commands include `scan`, `build`, `check`, `validate`, `query`,
`context`, `impact`, daemon builds, and watch builds. These paths must run with
inference disabled by construction. Ambient environment variables and inherited
daemon configuration cannot silently enable enrichment for canonical operations.

Canonical artifacts remain:

```text
graph-out/
  graph.json
  .graphite_graph.json
  .graphite_manifest.json
  .graphite_validation.json
  GRAPH_REPORT.md
  graph.html
```

Optional enrichment becomes a separate explicit overlay operation:

```text
graph-out/
  overlays/
    ollama/<provider-and-model-identity-digest>/
    openrouter/<provider-and-model-identity-digest>/
```

An overlay manifest binds the canonical graph fingerprint, provider identity,
model identity, limits, creation time, and sanitized outcome. Overlay data is
non-authoritative and cannot change canonical nodes, edges, communities,
fingerprints, freshness, validation, context, or impact results. When the canonical
graph changes, matching overlays become stale without making the graph stale.

Provider failure, deletion, identity drift, incompatible versions, or missing
credentials can disable an overlay but cannot block canonical graph operation.

## 9. Persistence and migration

Lifecycle observations and events use dedicated storage separate from capability
snapshots and execution attempts. Storage constraints enforce:

- Valid states and transitions.
- Canonical digest shapes.
- Provider and runtime-kind consistency.
- Immutable append-only transition evidence.
- One current observation per provider/runtime identity boundary.
- No activation without a matching verified snapshot.

Existing Claude and Codex snapshots are migrated as historical evidence. Migration
does not grant new authority. A snapshot becomes eligible only when its identity
matches a current lifecycle observation and all existing expiry and verification
rules pass.

Legacy Ollama routing records remain historical and non-replayable. OpenRouter
application-inference configuration does not become governed development-routing
authority through migration.

Migration requires a verified backup, integrity checks, foreign-key validation, a
tested rollback fixture, and stopped writers during destructive schema transitions.

## 10. Failure handling and security

- Probe failure affects only its provider.
- Identity drift invalidates only matching snapshots and approvals.
- Unsupported updates use `incompatible` with a stable reason code.
- Temporary endpoint or credential failure uses `unavailable` without deleting
  historical evidence.
- Corrupt lifecycle authority fails closed for provider execution but does not
  block canonical graph operations.
- Lifecycle errors contain no prompt, source, response body, auth payload,
  credential, or raw provider diagnostic text.
- Endpoint probes are allowlisted, HTTPS-only where applicable, redirect-restricted,
  response-size bounded, and deadline bounded.
- Every execution performs a final identity comparison before consuming approval.

No claim of perfect security is made. The design prioritizes prevention,
containment, detection, explicit recovery, and auditable human authority.

## 11. Testing strategy

Deterministic tests cover:

- Patch, minor, major, hash-only, model-digest, endpoint, and capability drift.
- Every valid transition and invalid-transition rejection.
- Snapshot and pending-approval invalidation after identity changes.
- Exact identity enforcement with a stopped or stale daemon.
- Fake Claude and Codex executables for version and capability probes.
- Fake local Ollama and OpenRouter HTTP servers with no external network access.
- Timeouts, malformed responses, redirects, insecure endpoints, rate limits, and
  unavailable providers.
- Sanitized persistence, migration, integrity enforcement, and rollback.
- Proof that lifecycle probes cannot reach inference endpoints.
- Proof that graph, context, impact, freshness, and validation outputs are
  byte-for-byte or semantically identical with providers healthy, incompatible,
  unavailable, or absent.
- Proof that enrichment writes only overlay artifacts.

Ordinary tests never consume subscription quota or contact external provider
networks.

## 12. Rollout sequence

1. Add lifecycle contracts, compatibility policies, persistence, and migration.
2. Add Claude, Codex, Ollama, and OpenRouter non-inference probe adapters.
3. Add daemon monitoring and lazy execution-time enforcement.
4. Split enrichment into an explicit non-authoritative overlay pipeline.
5. Migrate existing snapshot evidence without granting authority.
6. Run focused tests, full offline tests, schema rollback, graph determinism
   comparison, Ruff, security checks, and a zero-LLM graph rebuild.
7. Prepare new live verification manifests and stop for separate approvals.

## 13. Acceptance criteria

- All four providers use the shared lifecycle state machine.
- Routine patch updates require no source-code allowlist edit.
- Minor and major changes follow the defined compatibility gates.
- Every identity change invalidates old capability authority.
- The daemon performs only bounded non-inference probes.
- Successful probes stop at `verification_required` until approved verification.
- No provider failure can block or alter canonical graph operations.
- Canonical daemon and watch builds cannot inherit automatic enrichment.
- Ollama and OpenRouter enrichment exists only as a non-authoritative overlay.
- Execution rechecks exact runtime and snapshot identity even without the daemon.
- Lifecycle telemetry contains no secrets, prompts, source, model output, or raw
  provider diagnostics.
- Migration and rollback tests pass without granting legacy records new authority.
- No live provider request occurs without a complete manifest and explicit approval.
