# Provider lifecycle acceptance evidence — 2026-07-19

## Scope and authority

Task 9 was implemented on `feat/claude-codex-router` from commit
`0858a13c7256d28b3941057a0c75d62f7f46639a`. This work adds only local read-only
operator inspection and non-activating preparation. It did not invoke Claude,
Codex, Ollama, OpenRouter, or any network inference; it did not retry, fall back,
promote a policy, activate a provider, merge, push, or deploy.

The lifecycle operator opens only an existing `provider-lifecycle.sqlite3` with
SQLite `mode=ro` and `query_only`. It validates the repository-contained path,
schema version, integrity, and foreign keys before returning bounded records.
Missing storage fails with `lifecycle_storage_missing` and creates no `.graphite`
directory. Status, list, and history expose digests, enum values, versions, and
timestamps only. Executable paths, endpoints, query strings, credentials, prompts,
provider diagnostics, and source are outside the public contract.

Compatibility inspection accurately reports that only the policy version—not the
full policy parameters—is persisted. Policy preparation binds the exact current
incompatible identity to a canonical candidate digest. It does not write or
activate the candidate and explicitly records that separate human authority is
required. Verification preparation binds one exact `verification_required`
identity to fixed model, effort, fixture, graph, contract, token, time, and optional
cost limits. It fixes one attempt with fallback, resume, and substitution disabled,
then stops before inference.

## Offline test and recovery evidence

The initial focused operator and CLI run passed 31 tests. The lifecycle, lifecycle
storage/service, operator, and routing-storage regression run passed 101 tests with
one intentional platform/fixture skip. After logical identity-binding corruption
coverage was added, the final focused lifecycle/storage/service/CLI/documentation
run passed 127 tests. The broader routing, provider-lifecycle, daemon, overlay, LLM,
documentation, and security selection passed 701 tests with one intentional skip
and 1,024 deselected tests. All runs used unique writable task-scoped base-temp
directories outside the repository and disabled pytest cache writes.

The routing regression run exercised the disposable schema-v4-to-v5 fixture. It
proved that migration creates and verifies the schema-v4 backup and SHA-256 marker,
keeps historical rows lifecycle-unbound, passes integrity and foreign-key checks,
and can restore the verified v4 backup. Provider-lifecycle schema-v1 backup and
failure paths were exercised independently. Every drill operated on pytest
temporary databases; the operator's active routing database was not opened or
altered.

Canonical graph determinism remains a separate authority boundary. Provider state,
provider failure, lifecycle storage, and overlays are excluded from canonical graph
inputs. The final Task 9 rebuild and freshness check passed without warnings.

## Operational recovery contract

All routing writers must be stopped before schema migration or restore. The v5
cutover verifies the v4 backup, marker, SQLite integrity, and foreign keys before
adding lifecycle bindings. Rollback preserves the v5 database for incident analysis,
atomically restores the verified v4 backup, and resumes only with the matching v4
application. A missing marker, mismatch, lock, partial schema, or failed integrity
check keeps routing stopped for verified restore or a tested forward fix. Schema
metadata and historical evidence must never be hand-edited to manufacture authority.

## Remaining live-readiness gates

Graphite is not production-ready. A current Claude profile must pass a separately
approved bounded verification. Claude and Codex must each pass separately approved
edit smokes, followed by a cross-provider high-risk review. Final audit persistence,
the complete test suite, and the final canonical graph must also pass. No live
manifest authorizes a call until its complete contents are displayed and explicitly
approved by the operator.

## Final Task 9 verification

- Focused lifecycle/storage/service/CLI/documentation: 127 passed.
- Broad routing/lifecycle/daemon/overlay/LLM/documentation/security: 701 passed,
  1 skipped, 1,024 deselected.
- Ruff on all touched Python files: passed.
- `git diff --check`: passed.
- Canonical graph: 7,468 nodes, 16,530 edges, 154 communities, 184 scanned files.
- Freshness: passed with no added, changed, or removed source files.
- Engine fingerprint:
  `725e821e0a28071f85e53984f0ffc3ea6f7c77337f9c76935dde70cb11679adb`.

## Task 10 offline acceptance

The complete offline suite passed 1,683 tests with 44 intentional skips in
441.96 seconds. Repository-wide Ruff over `src` and `tests` passed. Unstaged and
staged diff checks passed; the post-Task-9 tree was clean before this evidence
update. The secret-pattern review found only a deliberately hostile redaction test
value and a non-secret task identifier in a historical plan. No credential material
was found in implementation or operator evidence.

The provider-state isolation selection passed 41 tests and proves byte-equivalent
canonical artifacts and unchanged read results across fake active, unavailable,
drifted, corrupt-persistence, and overlay-failure states. The dedicated v4-to-v5
backup/restore selection passed 2 tests with 31 unrelated routing-storage tests
deselected. The clean canonical graph validates with zero errors and warnings;
`check`, `context`, `impact`, and query statistics all succeeded with the Task 9
counts and engine fingerprint recorded above.

The disposable active fixture database was opened in SQLite read-only mode. It is
schema v4, reports `integrity_check=ok`, and has zero foreign-key violations. Its
quarantined over-budget database was not opened, restored, copied, or treated as
authority. Lifecycle schema-v1 backup/integrity behavior and routing v4 restore
were verified only in isolated test databases; no operator database was migrated or
altered.

No provider executable, subscription request, external network inference, retry,
fallback, or model substitution occurred. No merge, push, or deployment occurred.
Route-pool capacity behavior used deterministic fakes only. The feature branch is
still unpushed. The main worktree currently points at `edbd7e5`; this branch did not
modify it.

Exact live verification manifests cannot yet be truthfully issued from the new
operator boundary because the disposable fixture has no provider-lifecycle database
and the last accepted observations predate the restart. Fabricating a current
lifecycle identity would violate the exact-identity contract. Preparing a current
manifest therefore stops at the live gate until an explicitly approved bounded
provider observation establishes the installed identity; observation itself must
not perform inference or activation.

## Post-acceptance Claude help-format correction

A separately approved non-inference Claude lifecycle observation failed closed as
`probe_capability_missing`. It stopped during fixed metadata validation before
identity construction, persistence, verification, activation, retry, or fallback.
A second separately approved single-command diagnostic hashed the bounded help
output and compared only the fixed expected option names. It exited zero in 0.588
seconds with stdout SHA-256
`fcd5b45507c7c602d54d85a300eab288a8a3c6770c6def696ca19a3100725de4`.
The existing whitespace-token parser reported only `--allowedTools` and
`--max-turns` missing. Raw help, authentication data, paths, and provider diagnostics
were not retained or printed.

Offline reproduction proved that exact options enclosed by brackets or followed by
commas or `=` values are false negatives under whitespace splitting. The parser now
uses precompiled case-sensitive exact option-boundary patterns. Tests accept safe
punctuation while rejecting prefixed names and longer lookalikes such as
`--allowedToolsExtra` and `--max-turns-extra`. Focused Claude probe tests passed 6
tests; the broader Claude/Codex probe, observer, lifecycle storage/service, routing
security, and documentation selection passed 155 tests. Touched-file Ruff and diff
checks passed. The final canonical rebuild is fresh with 7,471 nodes, 16,540 edges,
157 communities, 184 scanned files, and engine fingerprint
`905d374a45fbe33e5f5439ead76f818f895549451751cac476e5fd3e106ebf10`.
No provider command was invoked by the correction itself.
