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
