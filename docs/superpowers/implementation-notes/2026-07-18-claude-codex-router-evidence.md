# Claude Code and Codex Router Acceptance Evidence

**Status:** Offline implementation complete; bounded live acceptance remains open.

## Scope and authority

Governed development execution is limited to authenticated Claude Code and Codex
subscription CLIs. OpenRouter is reserved for application inference. Ollama report
enrichment remains a separate compatibility feature, while earlier Ollama routing
records are historical and non-replayable.

Capability evidence is not authorization authority. A verified snapshot proves only
the executable/profile facts observed at verification time. Every execution,
cross-provider review, acceptance, policy promotion, rollback, and cleanup retains a
separate default-No human authority gate. Historical Ollama documentation refers to
`routing.registry.BUNDLED_PROFILES`; that legacy allowlist does not authorize a
Claude Code or Codex execution.

## Offline evidence

Implementation through Task 8 is recorded at commit `12a1bfa`. Verification on
2026-07-18 produced:

- focused telemetry/policy/storage/service/CLI checks: 123 passed, 1 intentional
  platform or capability skip;
- complete routing selection: 355 passed, 1 intentional skip, 1143 deselected;
- complete offline suite: 1457 passed, 44 intentional platform or optional-tool
  skips;
- Ruff checks for changed routing, CLI, and test files: clean;
- Git whitespace/error check: clean;
- branch-source Graphite build and strict validation: 6196 nodes, 13582 edges,
  zero errors, and zero warnings.

The tests establish exact CLI identity checks, strict structured output, isolated
worktrees, immutable approval/audit bindings, fail-closed diff policy, bounded
validation, separate cross-provider review, telemetry field minimization, unknown
subscription cost, signed policy candidates, human-only promotion, and
evidence-preserving rollback.

## Schema migration and rollback drill

The disposable schema fixture reconstructs v3, creates representative legacy
history, migrates a copy to v4, and verifies the automatically created v3 backup and
SHA-256 marker. It confirms v4 integrity and foreign keys, ensures live legacy
provider attempts are quarantined, stops all fixture writers, restores the verified
v3 backup, and proves the v3 reader can recover the schema version and historical
rows. The exercised drill passed. No operator routing database is used by this
drill.

Operational rollback requires the same sequence: stop writers, verify the backup
digest and SQLite integrity, preserve the v4 database, atomically restore the v3
backup, deploy matching v3 code, validate history with a read-only path, and only
then resume writers. Missing or invalid backup evidence requires continued shutdown
and a tested forward fix.

## Remaining external gates

This capability is not yet production-ready. The first separately approved Claude
Code `sonnet`/high/read-only verification failed closed because a terminal
`modelUsage` map can contain more than one entry. After an offline contract fix,
a separately approved streaming verification bound every assistant event to
`claude-sonnet-5`, returned the exact expected response, reported 6 input and 2960
output tokens, made no edit, saved the verified snapshot, and received an accepted
human verdict. Both the initial blocked event and successful evidence remain
append-only; there was no automatic retry, fallback, or model substitution.

Codex
no-edit verification initially failed closed because the adapter expected an
invented `turn.completed.model` field that Codex's documented JSONL event does not
provide. No Codex snapshot was saved and a sanitized blocked event was appended.
The offline contract now binds Codex to the full non-alias model slug in the signed
snapshot and strict command, accepts the documented model-less terminal event, and
still rejects any conflicting future model echo.

A separately approved repeat used `gpt-5.6-sol` at high effort, read-only mode,
snapshot digest `b1be74a8ba19542246d524ef2f90815a47cef8546d17d0c747ca1d0f88b9dcbe`,
and the unchanged synthetic graph fingerprint. The exact response and full-slug
contract checks passed in 14.347 seconds, with 41,854 reported input tokens and 84
reported output tokens. The approved maxima were 32,768 input and 4,096 output.
The result is therefore invalid: it is not accepted capability evidence and cannot
authorize Codex routing.

The acceptance harness had saved the snapshot before performing its usage check.
The entire disposable fixture database was immediately moved out of the active path
to `events-overbudget-quarantined.sqlite3`; its SHA-256 is
`aaae2e5537a96eeb3d1122146272e42c05f68db340e71bfdb7ea22dc821723dc`.
The active `events.sqlite3` path is absent. The quarantine preserves the prior
append-only fixture audit, including valid Claude evidence, but exposes no active
snapshot. No operator database was involved.

The production boundary now requires reported input/output usage, validates each
against the approved limits, and persists only through an ordered verify-and-save
operation. Regression tests prove over-budget input or output leaves the capability
store empty. This offline correction did not retroactively validate the failed
attempt, and no automatic retry or substitute model was used.

After a new exact manifest and separate approval, a fresh `gpt-5.6-sol` high-effort
read-only verification used the rebuilt synthetic graph fingerprint
`f494a0da32593b6daff69aeca667c724e5e83345c00a9fb68467c6afd79c6d0f` and candidate
snapshot digest `cda9c0a8ac530fd8ae6f7d640497b8e92c4fb479b4e4c99bbc3f00df65816292`.
Local harness preconditions initially stopped before provider execution because of
an incorrect graph-metadata lookup and then an incorrect credential-home binding;
neither stop invoked a model. After those local corrections, exactly one provider
request returned the exact expected response and bound full model slug in 12.792
seconds. It reported 20,851 input and 8 output tokens against approved maxima of
65,536 and 4,096, made no edit, and passed the hardened pre-save budget checks.
Exactly one active snapshot was saved. Machine-verified telemetry and the user's
subsequent accepted verdict were appended as two sanitized immutable events; cost
remains `unknown`.

Both isolated edit smokes, the read-only cross-provider high-risk review, and final
live audit persistence remain unexecuted. Codex profile verification is accepted,
but the active fixture has no current Claude snapshot and snapshots are short-lived.
Any diagnostic repeat, profile refresh, edit smoke, review, or subsequent provider
call requires a new bounded manifest and explicit approval. The offline schema
rollback drill remains passed.

After Codex acceptance, local preflight found that Claude Code had upgraded from
2.1.208 to 2.1.214 and its executable digest had changed, invalidating the earlier
snapshot identity. A separately approved `sonnet` high-effort read-only refresh
bound the updated executable, graph fingerprint
`f494a0da32593b6daff69aeca667c724e5e83345c00a9fb68467c6afd79c6d0f`, and candidate
snapshot digest `8ce9d71b5f9c03103f6cd70ae79690719aabcc48036c7b7ea18ba12159ef50ac`.
Exactly one provider request completed in 4.898 seconds and reported
`claude-sonnet-5`, 2 input tokens, and 45 output tokens within the approved limits.
It failed closed because the result was not exactly the required verification
marker. No Claude snapshot was saved, no file changed, and no retry or model
substitution occurred. One sanitized machine-failure event was appended; raw output
was neither persisted nor committed. A fresh Claude verification remains an
external gate and requires a new exact manifest and approval.

Offline follow-up confirmed that Claude Code 2.1.214 exposes the documented
`--json-schema` contract and returns validated data in terminal
`structured_output`. The adapter now has a verification-only, one-turn structured
path with a fixed single-field schema. It preserves assistant-event model binding,
read-only permissions, bounded usage, and pre-save validation while refusing to
treat free text as authority. Focused tests cover the exact canonical arguments and
reject missing, wrong, or additional structured fields. No live retry was made as
part of this correction.

A new exact manifest and explicit approval authorized one live structured refresh
against graph fingerprint
`793e9f18e0b27df5706334795a30ff817b82eb68f92f78dc7a6b9fd46022e53e` and candidate
snapshot digest `d08c66908ee55f5494da4abba21cce02df544214b4db721cbb94c962dea95be2`.
The bounded CLI process exited without a parseable terminal result after 45.8
seconds. Effective model and input/output usage are therefore unknown, no snapshot
was saved, and the existing Codex snapshot remained the only active snapshot at
that time. A sanitized failed event records those fields as unknown. No source file
changed, no raw output was persisted, and no retry or fallback occurred. Current
Claude debug-log metadata contained no diagnostic record for the call, so the exact
external CLI failure remains unresolved rather than inferred. The structured path
is offline-tested but has not passed live acceptance.

The bounded CLI transport now handles a completed nonzero provider process without
asking the lower-level runner to discard its bounded streams first. It validates
the returned transport record, hashes stdout and stderr, and propagates only an
immutable allowlisted diagnostic record: exit classification, numeric exit code,
duration, stdout SHA-256, stderr SHA-256, and the fixed
`provider_process_failure` category. Raw stdout, stderr, prompts, credentials,
paths, and provider diagnostic text are not attached to either the transport or
adapter exception. Deterministic fake-process coverage proves that a nonzero Claude
structured-verification exit is invoked once, is normalized to `unavailable`, and
retains exactly those sanitized fields through the adapter boundary.

Offline acceptance after this hardening passed 56 focused process/Claude/Codex
adapter tests and the full routing selection with 371 passed, 1 skipped, and 1,144
deselected. A final focused adapter/documentation selection passed 123 tests.
Repository-wide Ruff and `git diff --check` passed. No Claude, Codex, credentialed
provider, network inference, retry, or fallback was invoked by this correction.

The first final graph-refresh command mistakenly inherited the repository's local
automatic enrichment configuration and reported one local Ollama enrichment. That
local model invocation was not authorized, is not acceptance evidence, and did not
use Claude, Codex, provider credentials, or network inference. The mistake was
reported immediately. The graph was then replaced by an explicit `--llm none`
build; its manifest records `llm_mode` as `none` and `llm_status` as `disabled`, and
the resulting fresh graph contains 6,233 nodes, 13,683 edges, and 152 files. No
current Claude snapshot was created; production readiness remains blocked on a new
separately approved bounded verification and the remaining live acceptance gates.

The provider-lifecycle design now supports an immutable, ordered
`ApprovedRoutePool` spanning Claude Code, Codex, Ollama, and OpenRouter. Runtime
availability cannot add, reorder, or authorize candidates. The initial policy
allows one fallback only after an exact sanitized `capacity_unavailable` result,
before accepted output or any tool, edit, or external side effect, within one
aggregate approval and budget. Cross-provider selection and every exact candidate
must be explicitly authorized; verification calls remain single-route.

The prerequisite non-inference probe boundary is implemented without contacting a
provider. CLI nonzero exits retain only the existing hashed diagnostics and now
classify capacity only when a complete provider-allowlisted diagnostic line matches;
ambiguous or embedded capacity text remains `provider_process_failure`. The HTTP
boundary uses fixed non-inference purposes, scheme/host/port policy, bounded DNS
workers, public-address enforcement for OpenRouter, loopback-only enforcement for
Ollama, address-pinned connections with peer revalidation before credential
injection, one deadline, response/header caps, JSON content validation, and no
redirects. Exceptions suppress raw endpoint, body, header, credential, and resolver
details.

Offline verification passed 37 focused process/probe tests, 50
Claude/Codex/service compatibility tests, 67 documentation tests, and the full
routing selection after the final cases with 448 passed, 1 skipped, and 1,144
deselected. Repository-wide Ruff and `git diff --check` passed.
All HTTP activity used deterministic fakes or a local loopback metadata fixture;
no Claude, Codex, Ollama inference, OpenRouter inference, credentialed external
request, retry, or live fallback occurred.

An explicit pre-commit `--llm none` rebuild after the implementation and before
the final evidence edits was fresh with 6,599 nodes, 14,559 edges, 162 scanned
files, 136 communities, and zero validation warnings. Its then-current engine
fingerprint was
`2ed615565078bac0ef11040b78b254a8b82c9cf972079a02807a09d838484649`;
the manifest records `llm_mode` as `none` and `llm_status` as `disabled`.

The governed cross-provider selector is now active as a provider-neutral authority
boundary before adapter rollout. One signed `ApprovedRoutePool` binds one or two
ordered exact candidates, current lifecycle and capability digests, model and
routing identity, trust/permission/risk requirements, expiry, and aggregate token,
duration, and optional cost limits. The existing HMAC and repository/machine quota
authority consumes the entire pool once; concurrent or replayed consumption fails.

The coordinator invokes the preferred candidate and automatically advances at most
once only after exact sanitized `capacity_unavailable`, no accepted output, and
proven zero side effects. It reloads current lifecycle authority before the second
attempt. Every other failure, stale identity, expired snapshot, under-capability
candidate, trust/risk/permission mismatch, exhausted budget, unknown side effect,
or evidence-sink failure stops closed. Tests exercise Claude Code, Codex, Ollama,
and OpenRouter identities entirely through deterministic fakes. No provider was
invoked. The existing `route run` single-model persistence path is intentionally
unchanged; adapter-backed command wiring requires the later audited multi-attempt
persistence integration rather than weakening its current one-model constraints.

Offline acceptance for this activation passed 151 focused route-pool, approval,
lifecycle, and documentation tests. The final full routing selection passed 477
tests with 1 intentional skip and 1,144 deselected. Repository-wide Ruff and
`git diff --check` passed. All selection and execution behavior used deterministic
fake runners; no provider, external network, retry outside the approved pool,
merge, or push occurred.

## Provider lifecycle adapters

Task 4 added normalized lifecycle observations for Claude Code, Codex, Ollama,
and OpenRouter without invoking inference. CLI adapters use canonical external
executables, byte digests, exact semantic versions, bounded local help and auth
commands, aggregate deadlines, and post-probe digest checks. The executor paths
now share those identity primitives while retaining their existing strict output
contracts. Ollama uses only loopback version, tags, and show metadata and binds an
exact tag to its immutable digest. OpenRouter permits only the canonical HTTPS API
root, follows no redirects, and binds the configured model and routing-policy
digests using auth and model metadata endpoints only.

Focused adapter, process-boundary, executor, registry, and routing-security tests
passed 124 tests. The broader offline routing/provider selection passed 489 tests
with 1 intentional skip and 1,145 deselected. Repository-wide Ruff and
`git diff --check` passed before graph acceptance. All adapter tests used
deterministic fake transports; no provider process, external request, inference,
retry, fallback, model substitution, merge, or push occurred.

The canonical acceptance rebuild used the feature-branch source and explicit
`--llm none`. It completed with 6,911 nodes, 15,236 edges, 174 scanned files,
137 communities, zero build warnings, and fingerprint
`8d223b44a4679b1d502f2b055c1fcd8714c89c6fa7d450ef8d94ad229eee0482`;
the freshness check passed with `llm_status=disabled`.

## Lifecycle execution authority

Task 5 added the provider-neutral lifecycle coordinator. Compatible discovery and
drift stop at `verification_required`; only a separately accepted, exact,
lifecycle-bound capability snapshot can activate an identity. Identity or health
changes atomically change the lifecycle authority first, making old snapshots and
approvals ineligible, then append provider-scoped invalidation evidence and mark
matching pending approvals invalidated. A failure in the secondary evidence step
therefore remains fail-closed rather than restoring authority.

Lifecycle-enabled CLI routing filters recommendations to active bound snapshots,
performs an injected bounded live runtime observation before approval consumption,
and binds the approval and attempt to the same lifecycle digest. Route-pool
authority loaders can derive the same exact active binding for Claude Code, Codex,
Ollama, and OpenRouter. Verification-manifest preparation is local and contains
only hashes, bounds, exact identity/model policy, one-attempt/no-fallback rules,
and allowlisted evidence fields; it does not invoke a provider.

Focused lifecycle, profile, approval, policy, service, shadow, route-pool, and
security acceptance passed 202 tests. The broader offline routing/provider
selection passed 504 tests with 1 intentional skip and 1,145 deselected. Ruff and
diff checks passed. All observations and executions used deterministic fakes; no
provider process, external request, inference, retry, fallback, merge, or push
occurred.

The Task 5 canonical rebuild used explicit `--llm none` and completed with 7,017
nodes, 15,523 edges, 176 scanned files, 136 communities, zero build warnings,
and fingerprint
`c714a6569b185a9938e9b19b5dd25bb1237b07c430275a112714092ccaf04192`.
The freshness check passed with `llm_status=disabled` and zero LLM tokens.

## Daemon provider observation

Task 6 added a bounded provider-neutral observer with explicit enabled-provider,
interval, timeout, per-cycle, backoff-cap, and jitter limits. Each due target is
observed at most once per cycle; failures receive capped exponential backoff and
never trigger retry, fallback, activation, verification, provider switching, or
model execution. Exact machine-wide CLI observations may be reused only within
one cycle, while endpoint, model, routing-policy, lifecycle database, and
authority state remain repository scoped.

The daemon runs the observer on a separate daemon worker, so a delayed or failed
provider probe cannot consume the graph build budget or block project scanning or
canonical graph freshness. Lifecycle changes persist through the lifecycle
service, which retains the Task 5 provider-scoped snapshot and pending-approval
invalidation behavior. Daemon status, logs, and health output expose only bounded
aggregate state and allowlisted reason counts. Raw diagnostics, provider output,
executable paths, endpoint query data, headers, credentials, bodies, prompts, and
source are not accepted into that boundary. Provider degradation is a health
warning and does not degrade canonical graph operation.

Focused observer, daemon, daemon-health, lifecycle, probe, route-pool, and
routing-security acceptance passed 228 tests, including three real offline daemon
builds whose canonical manifest engine and file inventories were identical for
active, unavailable, and lifecycle-persistence-failure states. The broader offline
routing/provider/daemon selection passed 623 tests with 1 intentional skip and
1,043 deselected. Repository-wide Ruff and `git diff --check` passed. All
provider behavior used deterministic fakes; no provider process, external
request, inference, retry, fallback, activation, merge, or push occurred.

The pre-evidence Task 6 canonical rebuild used the feature-branch source and
explicit `--llm none`. It completed with 7,144 nodes, 15,785 edges, 178 scanned
files, 144 communities, zero build warnings, and fingerprint
`bcb3f7b89932d63cfef7d472f658a79ddb8b94c08e24ca654f7d32f6d27fc525`.
The freshness check passed with no added, changed, or removed files.
