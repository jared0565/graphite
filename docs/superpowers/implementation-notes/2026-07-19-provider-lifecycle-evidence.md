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

Graphite is not production-ready. The current Claude profile has passed its
separately approved bounded verification. Claude and Codex must each still pass
separately approved edit smokes, followed by a cross-provider high-risk review.
Final audit persistence, the complete test suite, and the final canonical graph must
also pass. No live manifest authorizes a call until its complete contents are
displayed and explicitly approved by the operator.

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

The separately approved follow-up observation still failed as
`probe_capability_missing`, proving punctuation was not the complete cause. It again
stopped before authentication completion, identity construction, persistence,
verification, inference, retry, or fallback. Anthropic's current
[Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage) explicitly
states that `claude --help` does not list every flag and separately documents both
`--allowedTools` and `--max-turns` as supported. Their omission therefore cannot be
used as negative runtime-capability evidence.

The lifecycle probe now gates only the help-discoverable safety and structured-output
surface. The verification executor still supplies `--allowedTools` and
`--max-turns 1`; an installed CLI that rejects either fails closed during the exact
verification call and cannot create authority. Tests cover incomplete help, exact
boundary matching, lookalike rejection, and the immutable verification argv. The
focused probe/executor selection passed 30 tests; the broader probe, executor,
observer, lifecycle, process, routing-security, and documentation selection passed
197 tests. Repository-wide Ruff and diff checks passed. No provider command was
invoked by this offline correction. The final fresh canonical graph contains 7,472
nodes, 16,544 edges, 151 communities, and 184 scanned files; its engine fingerprint
is `19edcaa577a1fb0fe95eafa6aa6536ec21430ed8fe8077f44a6284cd0a3ee27c`.

## Current Claude bounded verification

The operator explicitly approved verification manifest
`58830ca00ef8e0e816c7a2f180f0ddd8851d8e5ff6cf0d50b1fb7eb2725c0871`.
Local preflight bound the call to Claude Code `2.1.215`, executable SHA-256
`f14452d1e199273795f2920c00fd7a7f818178ddf1f3efb4d005a7e3d4ec4eff`,
lifecycle identity
`a675534863c4e9a2e2205accf7ca60e0fea45894156fff4c5dbc597287e164c2`,
fixture commit `01a9603e54f0eb0c878c2c8e9729a82e8dd7c180`, and canonical graph
fingerprint `f329c348d125c03fb7bc6ecff2592e0f3682b96b4a2430f3355464c6d304ca8c`.
The manifest allowed exactly one read-only `sonnet` attempt, required effective
model `claude-sonnet-5`, fixed effort `high`, one turn, no fallback, no resume, no
substitution, 65,536 maximum input tokens, 4,096 maximum output tokens, and a
120-second timeout.

The single approved attempt passed the exact structured-response, model-identity,
read-only, and token-budget checks. Sanitized receipt evidence is:

- effective model: `claude-sonnet-5`;
- duration: 9,603 milliseconds;
- usage: 2 input tokens and 521 output tokens;
- stdout SHA-256:
  `5ce32e3a2058f0ebf09104398ab6d3a608f9b29a9f38b51b967bc771b1d40613`;
- stderr SHA-256:
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`;
- capability snapshot:
  `15f3fe9a6749925432a223162c107d2a5264e291f9c1cda2adb1d591f513ddcb`;
- snapshot expiry: `2026-07-19T20:24:30Z`.

The schema-v5 snapshot and lifecycle binding were saved only in a new isolated
acceptance store. The active schema-v4 fixture database was not opened, migrated,
or treated as authority. The lifecycle deliberately remained
`verification_required`; activation was false. The fixture and implementation
worktrees remained clean, and there was no retry, fallback, model substitution,
merge, push, or deployment. Earlier harness preflight corrections failed before
the isolated store was initialized and before any model attempt, so they consumed
no inference authority and retained no provider output.

## Edit-smoke authority promotion correction

The post-verification audit found that no-edit verification correctly creates a
read-only capability snapshot, while normal routed changes require an exact
workspace-write snapshot. There was no fail-closed boundary that could promote the
verified identity after a separately approved edit smoke. Persisting write
authority before the smoke would bypass the intended acceptance gate; leaving the
snapshot read-only would make the provider permanently ineligible for edits.

Graphite now exposes a separate edit-smoke verification boundary. It requires an
unexpired read-only snapshot already bound to the exact lifecycle identity and
refuses to invoke its verifier without explicit approval. One sanitized result must
match the effective model, input/output reservations, non-empty bounded diff, and
deterministic validation outcome. Only a passing result creates a new
workspace-write snapshot. The promoted snapshot, lifecycle binding, and append-only
telemetry event commit in one SQLite transaction; any audit or binding failure
rolls back all three. Telemetry retains only identity, usage, duration, diff SHA-256,
changed counts, validation, provenance, and unknown cost—not source, prompt,
response, diff content, credentials, paths, or provider diagnostics.

Focused profile tests passed 20 tests. The wider profile, routing storage,
telemetry, policy, service, and lifecycle-service regression passed 145 tests with
one intentional skip. Touched-file Ruff and `git diff --check` passed. All new
verification used deterministic callbacks and isolated test databases; no provider
or network inference call occurred.

## First combined live-acceptance attempt and Codex probe compatibility

The operator approved exact aggregate bundle
`497224b328ac9fd82c30ce36c7b19b623040808ef97ba0ee78b461bfbb7511ba`.
It allowed five ordered attempts with stop-on-failure: Claude verification, Codex
verification, isolated Claude and Codex edit smokes, and a read-only Claude review
of the Codex high-risk diff. It prohibited retries, fallback, resume, substitution,
merge, push, deployment, and persistence of raw provider output or repository
content.

The first Claude verification passed in 10,577 milliseconds using effective model
`claude-sonnet-5`, 2 input tokens, and 451 output tokens. The isolated store saved
read-only snapshot
`d44ec2d79bd753a15049517a3892d5ad6c8026601728e59e935620da5a437acb`,
one lifecycle binding, and two sanitized telemetry events. The batch then stopped
before a Codex model call because the fixed local Codex metadata probe returned
`probe_capability_missing`. Total model attempts were one. Neither edit smoke nor
the cross-provider review started, and all three live worktrees remained clean.

The stopped batch's routing and lifecycle databases both passed SQLite integrity
checks. They were removed from active authority and placed in a dedicated
recoverable quarantine. Their SHA-256 digests are
`7a4be6012b36147e285e4e081fea28e90869d03a0ecc88abac787cff74d56988`
and `9ec9a2ebd7d510be2196417654eee87c3077fb67fa728cc75d545bfb1bcb7010`.
They must never be reactivated or treated as complete acceptance evidence.

The installed Codex `0.144.6` help renders short options with punctuation, such as
`-a,`. The probe's whitespace-token comparison therefore rejected five exact short
flags even though their required options were present. Capability matching now uses
boundary-safe exact-flag patterns. Punctuated flags pass, while longer lookalike
tokens remain rejected. A fixed non-inference probe against the installed CLI now
passes with the unchanged lifecycle identity
`2e00cae0b551e10906838bed0a7ca7ce9eba77759286f7da93323dc2b1f928a5`.

The focused Codex probe tests passed 4 tests. The broader routing and provider
selection passed 573 tests with one skip and 1,171 deselected. The complete offline
suite passed 1,701 tests with 44 skips. Repository-wide Ruff and diff checks passed.
No Codex inference, retry, fallback, edit, review, merge, push, or deployment
occurred. The fresh canonical graph contains 7,489 nodes, 16,635 edges, 157
communities, and 184 scanned files. Its canonical fingerprint is
`230718d9c0d37bd94a2bdd231b4e318f6afcafbb74312fdfc69ed86765c3d8b7`
and its engine fingerprint is
`deccdf36f0856d0f44dd4ccfc0c39c830cd92898098fb37c82c9cdd93e216142`.
A new complete live-acceptance bundle remains required.

## Second combined attempt and deterministic validation environment

The operator approved replacement bundle
`f50b231c2c9909dc3f971efbbd55e91057b18f14c89ba81e3bf9bbbf5c5d43ba`.
The duplicate approval message authorized the same single execution, not a second
run. Claude verification passed in 8,436 milliseconds using 2 input and 467 output
tokens, creating read-only snapshot
`c28f5494ef8618aaf338268c749ba6a30e73ef99ff42255f93f764bf883226d3`.
Codex verification passed in 8,110 milliseconds using 20,843 input and 8 output
tokens, creating read-only snapshot
`8628387d59806bf2445e3dfb8df758261ebd016a5f63e161fd04a23385300220`.
Both exact model and budget contracts passed and each snapshot received one machine
and one accepted-human telemetry event.

The one approved Claude edit call then produced the exact expected two-file diff
and exact response marker. Diff policy and `git diff --check` passed, but the local
test subprocess failed before test execution because the harness's reduced
environment could not discover the user-installed pytest. Adding only `APPDATA`
restored pytest discovery but exposed an ambient third-party plugin startup failure.
The batch therefore stopped as `validation_failed` after three total model attempts.
No write snapshot was promoted. The Codex edit and Claude review did not run, and
there was no retry, fallback, resume, substitution, merge, push, or deployment.

The partial databases passed integrity and foreign-key checks and contained exactly
two read-only snapshots, two lifecycle bindings, four sanitized telemetry events,
two current observations, and six lifecycle events. They were removed from active
authority and placed in a dedicated recoverable quarantine. Their SHA-256 digests
are `4ec986da6c7cf2531a7f18f33f59f48c2b247ea893c3ddacb2ed76084eab5ec1`
and `1572cd173e05eab3728c3891080e122cac741be2b2934f5ff675e84487f18bda`.
The failed edit worktree remains preserved and must not be reused as fresh evidence.

The acceptance harness now allowlists `APPDATA` solely for Python user-site module
discovery and sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`. This prevents unrelated
ambient plugins from entering validation while retaining a reduced environment.
Diff and test failures now have separate allowlisted failure codes. Both expected
Claude and Codex target edits pass the corrected deterministic validation contract,
and three new detached worktrees were created for the next attempt. These harness
checks invoked no provider or network inference.

## Third combined attempt and terminal capacity classification

The operator approved bundle
`c28eccd1ed54638962d59f441210cba9db3b3708c59968367e31f6df786666e3`
against three new detached worktrees. Claude verification passed in 9,373
milliseconds with 2 input and 536 output tokens, creating read-only snapshot
`cb9b835fac0eb54bbaaf13896cc149e3955b1fb22eed0200e0ca5b2479125793`.
Codex verification passed in 8,528 milliseconds with 20,843 input and 8 output
tokens, creating read-only snapshot
`9f56bbb94197fe5383280741b4042476e03aa7dfddc1a347add48260be7f5e04`.

The Claude edit smoke passed the exact response, model, budget, diff-policy, and
deterministic test contracts. It ran for 10,561 milliseconds using 6 input and 647
output tokens. Its two-file, 862-byte diff matched
`b31c6cc3c237f6d4fc0d7107fcbb0e96a61592a0bfd3aea79c3ace9db3b7a91d`,
and atomic promotion created workspace-write snapshot
`e2ab30294971118a9ddcfd48bbb98e4de8d44edd0448527930e0fbe1df7b8728`.

The single Codex edit call then returned a successful process with a terminal agent
message that did not satisfy the exact response contract and made no filesystem
changes. The batch stopped as `codex_edit_response_invalid` after four total model
attempts. The message body was neither retained nor inspected, so this event cannot
be claimed as proven capacity evidence. The Claude review did not run, and there
was no retry, fallback, resume, substitution, merge, push, or deployment.

The partial databases passed integrity and foreign-key checks and contained exactly
three snapshots, three lifecycle bindings, five sanitized telemetry events, two
current observations, and six lifecycle events. They were removed from active
authority and quarantined with SHA-256 digests
`9b0e39ce3d612dee1e1c297b25505010459a5df7199560356057dc9f51ebf1cd`
and `97e3d6c324a4772017c355096b95a6459c823206554a938e52fb300d718a4a75`.
The successful Claude write snapshot is therefore evidence of that attempt only and
must not be reactivated from quarantine.

Codex's adapter previously classified the exact allowlisted capacity notice only
when the process exited nonzero. The same exact notice delivered as the sole
successful terminal agent message would instead look like ordinary output and
prevent governed capacity routing. The adapter now maps only that complete,
case-insensitive message to `capacity_unavailable`; embedded or extended lookalikes
remain ordinary untrusted output. Focused adapter, process-runner, and route-pool
tests passed 68 tests. The complete offline suite passed 1,703 tests with 44 skips,
and repository-wide Ruff and diff checks passed. The fresh canonical graph contains
7,492 nodes, 16,650 edges, 146 communities, and 184 scanned files. Its canonical
fingerprint is
`f6fd73d49d0d258a26abe10b14c3dd70648bae75d4f64ce7ffac6b5058eb0dcc`
and its engine fingerprint is
`28f72d3d745783df4f67d9fc222dbf1f657d7dd3fc23702c29db02950453b224`.

## Fourth combined attempt and schema-bound Codex output

The operator approved bundle
`823c614a4246859f40a0c230f5fdedc61c40aaf6f07369ca5aebf58182395c45`.
Claude verification passed in 10,228 milliseconds with 2 input and 637 output
tokens, creating read-only snapshot
`6e0f21679e261cefa9ae102c2ce7d31f23aec32cdf46fc7b0c49bda1dd6c547d`.
Codex verification passed in 8,043 milliseconds with 20,843 input and 8 output
tokens, creating read-only snapshot
`648e18d2b84d87e67a86a09d0bfa4f148ec15ccaef2df5d903b75493f7c82fc0`.

The Claude edit smoke again passed all contracts. It ran for 16,852 milliseconds
using 12 input and 766 output tokens. Its two-file, 862-byte diff matched
`b31c6cc3c237f6d4fc0d7107fcbb0e96a61592a0bfd3aea79c3ace9db3b7a91d`,
and promotion created workspace-write snapshot
`d7f276c1dbb1650e35499e5f401c49c923c7931e34c4ef0e1955cd064f97ec09`.

The Codex edit again returned a successful process with an unexpected terminal
message and no filesystem changes. Because it was not the exact allowlisted
capacity notice, fallback remained unauthorized. The batch stopped after four
model attempts, the review did not run, and there was no retry, resume,
substitution, merge, push, or deployment. The isolated partial store passed
integrity and foreign-key checks and contains exactly three snapshots, three
lifecycle bindings, and five sanitized telemetry events. It is retained only as
the explicitly bound source for a separately approved single-call Codex smoke; it
is not complete production authority.

Installed Codex `0.144.6` exposes `exec --output-schema`. The adapter now supports
an optional external JSON output schema bound by exact SHA-256. It rejects relative,
workspace-local, symlinked/reparse, empty, oversized, malformed, non-object, and
digest-mismatched schemas before provider invocation, and fails closed if the
schema changes across execution. Calls without a schema retain their exact prior
argv. This permits the remaining edit smoke to use a structured terminal marker
instead of free text. Focused adapter, process, and route-pool tests passed 73
tests; the complete offline suite passed 1,708 tests with 44 skips. Repository-wide
Ruff and diff checks passed. The fresh canonical graph contains 7,506 nodes,
16,694 edges, 153 communities, and 184 scanned files. Its canonical fingerprint is
`355af395f3576e50524f897db6df9b44c9ab0c68c2619de01a54dab28383f142`
and its engine fingerprint is
`b5cebb0d2458ddd4d1df439aff0bf6daea794985061da312395059e65f7284c3`.

## Targeted Codex schema edit and terminal-message correction

The operator approved the single-call schema edit manifest
`7bab2e7e20af90586f10cd173a494f464be151b6a07f6f85ca971a83d6ee06b9`.
Exactly one Codex attempt ran; there was no retry, fallback, resume, provider or
model substitution, merge, push, or deployment. The process exited successfully,
but the adapter rejected the combined agent-message stream as
`unexpected_terminal_response`. The call also exceeded its approved input
reservation, so it is invalid independently of the response failure.

Sanitized receipt evidence is:

- effective model: `gpt-5.6-sol`;
- duration: 35,522 milliseconds;
- usage: 126,807 input tokens and 987 output tokens;
- stdout SHA-256:
  `69d7b732c9d5ee631b8cb2bdf033346442b5bb643f7dbd15b893f7ef2c7a4a6f`;
- stderr SHA-256:
  `b194bfb598bd517d88ddc38e787c70f02f8e7c4f22d584b60de394ebd520f316`;
- terminal-message byte count: 59;
- terminal-message SHA-256:
  `1dfd4367b5feaa485b939168c7875ad25223e25301c59f45ccba7dafb1e313a8`.

The worktree remained unchanged. Its empty diff retained SHA-256
`2dc0166a04a1f3ab3c1598e8043761b04df70aa5be6a522709518218b73c0b15`,
and deterministic local validation passed only against that unchanged baseline.
No write snapshot was promoted, and the retained partial store was unchanged.

Codex JSONL may contain intermediate agent messages before the schema-bound final
message. The adapter now selects only the last agent message when an external
output schema is active. Unschemaed calls retain their prior concatenated-message
behavior. A deterministic regression test proves that an intermediate progress
message cannot contaminate the terminal structured result.

Focused adapter, process, and route-pool tests passed 73 tests. A first complete
suite run encountered a cold 20-second real-MCP doctor-probe timeout; the same two
real-server isolation checks then passed together, and a clean-base complete rerun
passed 1,708 tests with 44 skips. Repository-wide Ruff and diff checks passed.

The next live attempt will not repeat this failed `gpt-5.6-sol` edit contract.
OpenAI documents `gpt-5.3-codex` as its agentic coding model optimized for Codex,
whereas `gpt-5.6-sol` is the general flagship model. Any switch requires a new,
separately approved bounded verification and edit manifest:

- https://developers.openai.com/api/docs/models/gpt-5.3-codex
- https://developers.openai.com/api/docs/models/gpt-5.6-sol

## Codex 5.3 acceptance bundle stopped at verification

The operator approved the three-call bundle
`4564c83c41d9d4cb5157bd36034d1c8c45f38e36b339b7958c5a921c9c14afe1`.
Its ordered actions were a schema-bound `gpt-5.3-codex` read-only verification,
a schema-bound bounded edit smoke, and a Claude read-only cross-provider review.
The bundle allowed at most one attempt per action and required an immediate stop
on failure.

The first Codex process exited nonzero before a parseable terminal result. The
sanitized receipt is:

- failure category: `provider_process_failure`;
- exit classification: `nonzero_exit`;
- exit code: `1`;
- duration: 7,238 milliseconds;
- stdout SHA-256:
  `fadc54b53c55e73612d75874e892a5af1b0064156470356444dd5a20294b7558`;
- stderr SHA-256:
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

The effective model and token usage are unknown. Raw stdout, prompts, responses,
provider diagnostics, credentials, paths, and repository content were not retained
or inspected. The edit and Claude review did not run, and there was no retry,
fallback, resume, substitution, merge, push, or deployment.

Both authority-store SHA-256 values remained unchanged at
`11dd6d905e8fdbbd237b2bb381b1ff00c0cee1782f2a6927e801ce485e8b5c3f`
and `d7bd02146e98c9b79e102d75c400f046189a6424d30a225126f109489a258d1b`.
Both fresh task worktrees remained clean. No capability snapshot, lifecycle
binding, or telemetry record was persisted.

The disposable bundle omitted `provider_process_failure` from its declared
failure-category allowlist even though the production adapter correctly emitted
that sanitized category. This manifest inconsistency did not expose data or permit
continued execution, but it must be corrected in any replacement manifest.

## Corrected Codex 5.3 replacement stopped at verification

The operator approved replacement bundle
`421e1fce5dace5d517552c7f360c0b1b9d5e0adbf0b0a861eac8331b25c8b08f`, bound to
implementation commit `0ecb52d3bd9abf4d43a1b204df81d3e841b67364`. The manifest
corrected the prior allowlist omission and passed deterministic no-inference
preflight. Its first ordered action again stopped at the Codex 5.3 verification
process, before a parseable terminal result.

The sanitized receipt is:

- failure category: `provider_process_failure`;
- exit classification: `nonzero_exit`;
- exit code: `1`;
- duration: 6,917 milliseconds;
- stdout SHA-256:
  `58251f7b5adea403e7afd22c1c3968fd518bcae7145a1fa1dbb389f298797747`;
- stderr SHA-256:
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

Effective model and token usage are unknown. Raw stdout, prompts, responses,
provider diagnostics, credentials, paths, and repository content were not retained
or inspected. The edit and Claude review did not run. There was no retry, fallback,
resume, substitution, merge, push, or deployment.

The routing and lifecycle stores remained unchanged at their approved SHA-256
digests, `11dd6d905e8fdbbd237b2bb381b1ff00c0cee1782f2a6927e801ce485e8b5c3f` and
`d7bd02146e98c9b79e102d75c400f046189a6424d30a225126f109489a258d1b`. Integrity and
foreign-key checks remained clean, with three snapshots, three bindings, and five
telemetry events. Both fresh task worktrees remained clean.

## Codex model availability diagnosis

The two bounded `gpt-5.3-codex` verification attempts failed before JSONL parsing
because that model slug is not present in the installed Codex 0.144.6 model cache.
The cache lists `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`,
`gpt-5.4`, `gpt-5.4-mini`, and `gpt-5.3-codex-spark`; it does not list
`gpt-5.3-codex`. A read-only `codex login status` check independently reports
`Logged in using ChatGPT`.

This is a model-selection failure, not an authentication or Graphite JSONL parser
failure. The next manifest must select an installed slug and bind it to a fresh
verification snapshot. `gpt-5.6-sol` is the selected candidate because it is
installed, supports high reasoning, and has prior bounded read-only evidence.

## Installed Codex 5.6 verification passed; edit smoke made no change

The operator approved installed-model bundle
`78ebdfc48d46533e1bfa0a459be9e990c99f742c91bf553b752d115401bee8b8`.
Codex `gpt-5.6-sol` verification passed in 8,646 milliseconds using 20,876 input
and 18 output tokens. It created read-only snapshot
`d0abf24d1cc07a3ebbaf72a9448523d619a690c7d346be089f3c6cfcf8707460` and two
sanitized telemetry events.

The following schema-bound Codex edit process returned a successful structured
response but produced no filesystem changes. The bounded diff was empty with
SHA-256 `2dc01697677c6dfe69b7f6af1b4b7cb8e5ee8681d44f47886b9675be8035a997`,
so the batch stopped as `edit_diff_mismatch` after two total attempts. No write
snapshot was promoted, no validation or Claude review ran, and there was no retry,
fallback, resume, substitution, merge, push, or deployment.

The routing store remains integrity-clean with four snapshots, four lifecycle
bindings, and seven telemetry events. The lifecycle store remains unchanged and
integrity-clean. The edit worktree is clean. The next attempt must use a separately
approved installed model and must still require a non-empty exact diff before any
write authority is granted.

## Codex Spark verification passed; edit exceeded budget

The operator approved Spark bundle
`d4a76ce81f0c83d353ecab849ce7e4f23f9d89ef4f1ac4db6429cfc1c2422500`.
The read-only `gpt-5.3-codex-spark` verification passed in 9,507 milliseconds
using 15,143 input and 175 output tokens, creating snapshot
`fbe724da951184f9069c35eb1090c0be66638eb1933a7b643ee9d292518958d1`.

Its edit smoke was stopped as `edit_budget_exceeded`: 263,655 input tokens and
5,342 output tokens exceeded the approved 196,608 / 4,096 limits. Sanitized
receipt hashes were stdout
`1125f80f6833fd7b5e67e8f4f99e75e865f173a7f6540683ce27e983a6bf8b70` and stderr
`9747cb5f7dbdf3d7c02661756fe9edf322dc16a8aabf4843f9a4576e8ff01643`, with a
36,110-millisecond duration. No write promotion, validation, or Claude review
ran; no retry, fallback, resume, substitution, merge, push, or deployment occurred.

The routing store remains integrity-clean with five snapshots, five bindings, and
nine telemetry events. The lifecycle store and worktree remain clean. The budget
was not weakened to accommodate the provider response.

## Codex Terra verification passed; edit smoke made no change

The operator approved Terra bundle
`46960ffe870888432f06c342e816c9d4ec38e798fe74fa2544ec3bb72c8e1f09`.
The read-only `gpt-5.6-terra` verification passed in 12,912 milliseconds using
20,876 input and 18 output tokens, creating snapshot
`5dfbfccf956a981137554d0228ac19111a31a8fcf81524a2a65767eed55ee0cc`.

The schema-bound Terra edit smoke returned successfully but produced no filesystem
changes. The bounded diff was empty with SHA-256
`2dc01697677c6dfe69b7f6af1b4b7cb8e5ee8681d44f47886b9675be8035a997`, so the batch
stopped as `edit_diff_mismatch` after two total attempts. No write snapshot was
promoted, no validation or Claude review ran, and there was no retry, fallback,
resume, substitution, merge, push, or deployment.

The routing store remains integrity-clean with six snapshots, six bindings, and
eleven telemetry events. The lifecycle store remains integrity-clean and the edit
worktree remains clean. Three installed Codex models have now passed verification,
but two independent edit smokes (`gpt-5.6-sol` and `gpt-5.6-terra`) produced no
change, while Spark exceeded the hard input budget. Production readiness remains
blocked on a verified Codex edit smoke.

## Codex no-op edit root cause and offline argv correction

Offline diagnosis on 2026-07-20 identified why every Codex edit smoke made no
filesystem change while read-only verifications passed. The installed Codex
0.144.6 enables its Windows write-capable sandbox only through the user
configuration key `[windows] sandbox = "elevated"` in the operator's
`CODEX_HOME/config.toml`. The immutable execution argv passes
`--ignore-user-config`, which strips that enablement from every bounded exec
child. Codex's own local sandbox logs show constant successful
`codex-windows-sandbox-setup.exe` write-ACE grants for interactive sessions and
zero sandbox engagements for any harness exec child or edit worktree, ever.
Without the sandbox, and with `-a never` making escalation impossible, every
write tool call is auto-denied: `gpt-5.6-sol` and `gpt-5.6-terra` completed
their turns and returned the schema-bound terminal message without writing
(empty diff, `edit_diff_mismatch`), while `gpt-5.3-codex-spark` repeatedly
fought the denials across turns until cumulative input usage (263,655 tokens)
exceeded its approved budget. No raw provider output was read or persisted
during this diagnosis, and no provider, network inference, retry, fallback, or
substitution was invoked.

The offline correction binds the Windows sandbox mode explicitly in the
immutable argv: a workspace-write execution now appends
`-c windows.sandbox="elevated"` after the reasoning-effort override, on every
platform, so the contract remains deterministic and survives
`--ignore-user-config` under `--strict-config`. Read-only argv is byte-for-byte
unchanged from the contract that passed live verification. Budgets were not
weakened, empty diffs still fail closed at both the harness diff comparison and
the `profile_edit_smoke_diff_invalid` promotion boundary, and over-budget usage
still fails closed before any write authority exists.

Deterministic fake-process acceptance now asserts the exact workspace-write
argv including the sandbox binding, the exact schema-bound edit argv with
`--output-schema`, and the exact read-only argv with the binding absent. The
focused adapter, process-runner, and profile selection passed 89 tests. The
full routing, lifecycle, probe, route-pool, daemon, and overlay selection
passed 659 tests with 5 intentional skips, and the complete offline suite
passed 1,710 tests with 44 intentional skips. Repository-wide Ruff over `src`
and `tests` and `git diff --check` passed.

## Codex Terra sandbox-bound edit smoke passed; Claude review timed out

The operator explicitly approved bundle
`b59a99feee2dee993eb4c96dd58abd2e11e152855e36606949a0867a4c6979ba`
(`graphite_codex_terra_sandbox_bound_acceptance_r7`, implementation commit
`9a5e7b4db45ae7c44d8c12c946e089f6101210ef`). Preflight verified the manifest
digest, fixture commit and graph, both store SHA-256 values and row counts,
clean worktrees, unchanged executable digests, and matching live lifecycle
identities.

The `gpt-5.6-terra` schema-bound read-only verification passed and created
read-only snapshot
`ca79928e51fb2880921ec6ca427cb2ac337cc1e55c22fed4b5f2e540bcc39718`.
The sandbox-bound schema edit smoke then passed for the first time: the
bounded process succeeded, the terminal structured message matched exactly,
and the worktree diff matched the pinned expectation
`53b31139102f8b2a324ea331285dfb0e39e9b54c8430e6cee113187c67895b8b` with two
changed files inside the byte bound. Deterministic local validation (diff
check plus pytest) passed, and atomic promotion created workspace-write
snapshot `ad449131682185c3ea11ca0039384585864b04d6734bf5a5edc086b898735723`,
confirming the Windows sandbox argv binding as the root-cause fix for the
earlier no-op edits. An independent post-run comparison confirmed the edited
worktree and the expected worktree produce byte-identical normalized patches
under identical git configuration.

The third action, the first-ever live Claude cross-provider review, failed
closed as `timeout` after its 120-second bound with three total model
attempts. No review telemetry was persisted, no raw output was retained, and
there was no retry, fallback, resume, substitution, merge, push, or
deployment. The routing store is integrity-clean with eight snapshots, eight
bindings, and fourteen telemetry events; the lifecycle store is unchanged.
The review worktree remained clean at baseline, and the edited Codex worktree
retains exactly the accepted diff.

## Read-only execution turn bound

Offline analysis of the timed-out review path found that read-only execution
maps to `--permission-mode plan` with only the `Read` tool allowlisted and no
turn bound: plan-mode guidance steers the model toward the non-allowlisted
`ExitPlanMode` tool, so a denial-retry loop can only end at the process
timeout, indistinguishable from legitimately slow high-effort analysis. The
adapter now passes `--max-turns 8` for read-only execution calls, converting
any such loop into a distinguishable fail-closed max-turns error while
leaving bounded room for reads. The verification path keeps `--max-turns 1`
and the live-proven workspace-write edit argv is unchanged. Focused adapter
selection passed 90 tests, the full routing selection passed 660 tests with
5 intentional skips, and the complete offline suite passed 1,711 tests with
44 intentional skips; Ruff and `git diff --check` passed. The next review
attempt requires a new manifest with a longer review timeout so a slow
legitimate review and a turn-bounded loop produce different outcomes.

## Second review attempt completed but exceeded its output budget

The operator approved single-action bundle
`22ba48a30ae5e6bb9f757ec4c85efffeac40a141c0578c6b043893c4ec43b7a2`
(`graphite_claude_review_completion_r8`, implementation commit
`3298e1f9b71c726e2ca582e76d2283ad68cbd34f`, review timeout 300 seconds).
Preflight verified both store digests and row counts, the retained Codex edit
diff, the clean review worktree, the unexpired Claude read snapshot, and the
live Claude identity. Codex was not invoked.

The single review attempt completed without timing out and without tripping
the eight-turn bound. Sanitized receipt evidence is:

- effective model: `claude-sonnet-5`;
- duration: 153,485 milliseconds;
- usage: 6 input tokens and 5,002 output tokens;
- stdout SHA-256:
  `b04fda08be35d0dd75c671817ab4461df67e56791942485179ec842d73b9cdba`;
- stderr SHA-256:
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

The 5,002 reported output tokens exceeded the approved 4,096 maximum, so the
attempt failed closed as `review_budget_exceeded` before the terminal message
was inspected and before any persistence. The routing and lifecycle stores
remained byte-identical at
`e45a40a06efcb2086707e10fd5cce780a88c534df595b38e68cc44c4381bbec2` and
`d7bd02146e98c9b79e102d75c400f046189a6424d30a225126f109489a258d1b` with
integrity ok, and the review worktree remained clean. There was no retry,
fallback, resume, substitution, merge, push, or deployment.

This result also explains the prior r7 timeout: a legitimate high-effort
review runs roughly 150 seconds and emits roughly 5,000 thinking-inclusive
output tokens, so the earlier 120-second bound killed it mid-generation. The
4,096-token output cap was calibrated for terse verification and edit
responses, not a thinking-inclusive high-risk review. The replacement
manifest proposes an 8,192-token review output maximum — a bounded increase
subject to separate explicit operator approval, not a harness-side
relaxation — with every other limit unchanged.

The separately approved 8,192-token replacement
(`graphite_claude_review_completion_r9`, bundle
`81196e83137cf2718ef88dd383db38ea90d63aff509a19a5eda6c88e235427d5`,
implementation commit `cfff02dafaa2429acd38098f2e6b3cd1b59db34d`) also
completed without timing out or tripping the turn bound and again failed
closed as `review_budget_exceeded`. Sanitized receipt evidence is: effective
model `claude-sonnet-5`; duration 211,047 milliseconds; usage 6 input and
8,242 output tokens; stdout SHA-256
`1a4e35e3085804d19b0800b22ad8f3bc1301e9112ea64f5a9f3dc22a6e2b2c15`; stderr
SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
No terminal message was inspected, nothing was persisted, the stores remained
byte-identical and integrity-clean, the review worktree remained clean, and
there was no retry, fallback, resume, substitution, merge, push, or
deployment.

Two clean completions now measure the true high-effort review workload at
5,002 and 8,242 thinking-inclusive output tokens with matching duration
growth, demonstrating high run-to-run variance rather than a loop or
truncation defect. The next manifest proposes a 16,384-token review output
maximum — roughly twice the worst observed sample — and a 480-second
timeout, both explicit operator-approval items, with attempts, input, tools,
turn bound, and every persistence rule unchanged.

The separately approved 16,384-token, 480-second replacement
(`graphite_claude_review_completion_r10`, bundle
`6cbc81cde9e1c7cf28f917e750bbcd700647d6262681a9b63b2f112871bbce36`,
implementation commit `e63962dcbf5ff5ec2cb7521f1cea8d9769ad93ba`) completed
within every bound and failed closed as `review_not_accepted`. Sanitized
receipt evidence is: effective model `claude-sonnet-5`; duration 76,171
milliseconds; usage 10 input and 6,110 output tokens; stdout SHA-256
`40ced7b5abe21f8f6183915a0348ee4fb39694c04f238518b16c583b658cb2cb`; stderr
SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
The review worktree remained clean, so the terminal message was either a
findings verdict or not exactly the required JSON object; the harness
discarded which, reported no verdict detail, and persisted nothing. The
stores remained byte-identical and integrity-clean, and there was no retry,
fallback, resume, substitution, merge, push, or deployment.

Because the review response contract sanitizes findings by construction —
each finding carries only a category enum, a severity enum, and an opaque
summary SHA-256 — the next harness reports the parsed verdict and any
shape-valid findings as allowlisted failure evidence, and distinguishes a
parse failure from a findings verdict. Acceptance criteria, bounds, and
persistence rules are unchanged; a findings verdict still fails closed and
is put to the operator.

## Review format root cause and structured-output correction

The separately approved discriminating replacement
(`graphite_claude_review_completion_r11`, bundle
`a1f4c91170c0f75f03a51e664f65b9b336915c4a5656cbaf7961325b5d299f42`,
implementation commit `f864dc02df707d7f0146d407419f61bc9dd460e6`) completed
within every bound and failed closed as `review_parse_failed`. Sanitized
receipt evidence is: effective model `claude-sonnet-5`; duration 32,058
milliseconds; usage 4 input and 2,347 output tokens; terminal message 1,742
bytes with SHA-256
`06bf23ba80d38cfb0b90a6c09367e6d8c1f5811b9aeada75ba6855d81a4ac648`; stdout
SHA-256 `19c36f0806954568fbe64bffb9d25f5a0be485fbdb6b7fc60abec319e1a77644`;
stderr SHA-256
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`. The
review worktree remained clean, nothing was persisted, the stores remained
byte-identical, and there was no retry, fallback, resume, substitution,
merge, push, or deployment.

This isolates the final review defect class: a free-text terminal message
cannot guarantee the bare-JSON response contract. The prior classes are
closed — timeouts ended with the 480-second bound, budget rejections ended
with the measured 16,384-token cap, and the eight-turn bound never tripped.
The offline correction extends the Claude adapter with an external
output-schema mode for read-only execution, mirroring both the Codex
`--output-schema` binding and Claude's own live-proven verification path:
the argv gains `--json-schema` with the canonical serialized contract, the
CLI validates the response, and the adapter accepts only a terminal
`structured_output` object, returned as canonical JSON. Requests combining
an output schema with workspace-write, the verification marker, or a
non-object schema fail closed as invalid before any process starts. The
workspace-write edit argv and the verification path are unchanged.

Focused adapter, process, and profile selection passed 95 tests; the full
routing selection passed 665 tests with 5 intentional skips; the complete
offline suite passed 1,716 tests with 44 intentional skips; repository-wide
Ruff and `git diff --check` passed, and the canonical rebuild is fresh. The
next review manifest binds the same review contract schema to the CLI
itself, with all bounds unchanged from the approved r11 values.

## Cross-provider review passed; live acceptance complete

The operator approved schema-bound bundle
`9b656e36a078a2124247a5d98d41d04c5cd7116bd0d444fc45fdcf7c359f3f43`
(`graphite_claude_review_completion_r12`, implementation commit
`c38119857ed9fb3215774d99e6b8a7e98076db8b`). The single approved review
attempt passed. Sanitized receipt evidence is:

- effective model: `claude-sonnet-5`;
- duration: 108,574 milliseconds;
- usage: 2 input tokens and 9,436 output tokens against 65,536 / 16,384;
- review verdict: `pass` with zero findings, CLI-validated against the
  pinned response contract `16c1816b92cd620e4a8d9a1f1a01d2e537c9e4e477da9563f78c30a82adf32c9`;
- primary diff: `53b31139102f8b2a324ea331285dfb0e39e9b54c8430e6cee113187c67895b8b`;
- stdout SHA-256:
  `c44ea53991ba0079febffde2c3dbdb0b2d02614a6ddca0802207b34057a4127c`;
- stderr SHA-256:
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

The review worktree remained clean and exactly one sanitized telemetry event
was appended. The final audit passed with the exact expected store contract:
routing integrity ok, zero foreign-key violations, eight capability
snapshots, eight lifecycle bindings, fifteen telemetry events; lifecycle
integrity ok with two current observations and six lifecycle events; both
provider states active. There was no retry, fallback, resume, substitution,
merge, push, or deployment.

Every live acceptance gate defined for this capability has now passed:

- current Codex profile verification (`gpt-5.6-terra`, snapshot
  `ca79928e51fb2880921ec6ca427cb2ac337cc1e55c22fed4b5f2e540bcc39718`);
- Codex edit smoke with the exact expected diff and atomic promotion of
  workspace-write snapshot
  `ad449131682185c3ea11ca0039384585864b04d6734bf5a5edc086b898735723`;
- Claude edit smoke with the exact expected diff and promoted
  workspace-write snapshot
  `d7f276c1dbb1650e35499e5f401c49c923c7931e34c4ef0e1955cd064f97ec09`,
  retained in the same active store;
- read-only cross-provider high-risk review with a schema-validated pass
  verdict;
- final audit persistence matching the approved store contract.

The complete offline suite passed 1,716 tests with 44 intentional skips at
the final implementation state (`c38119857ed9fb3215774d99e6b8a7e98076db8b`);
subsequent commits on this branch are documentation-only. The branch remains
unpushed and unmerged; integration, promotion beyond this fixture, and any
production deployment remain separate operator decisions with their own
approvals. The canonical `--llm none` rebuild is
fresh with 7,508 nodes, 16,713 edges, 161 communities, and 183 scanned files.
This correction is offline only: the next Codex edit smoke still requires a
complete fresh manifest and explicit operator approval, and production
readiness remains blocked on that live acceptance.

## Offline OpenRouter development-participation implementation (2026-07-20)

Following the operator's revocation of the OpenRouter application-only scope
(recorded in `2026-07-18-claude-codex-router-evidence.md`), the offline
implementation of OpenRouter as a third governed development provider was
completed per
`docs/superpowers/specs/2026-07-20-openrouter-development-participation-design.md`
and its 9-task plan. All work is deterministic-fake TDD; no live, network,
or credentialed call of any kind was made.

Delivered, each with its own failing-test-first cycle and commit:

- `ProviderId.OPENROUTER` with evidence host `openrouter.ai`,
  `operator_openrouter_profile`, and a CLI-transport guard rejecting
  non-CLI providers with `provider_invalid` in both
  `build_cli_environment` and `run_cli_process`;
- one governed inference purpose `OPENROUTER_CHAT_COMPLETIONS`
  (POST `/api/v1/chat/completions`) in the pinned HTTP transport with
  purpose-aware ceilings (1 MiB request, 4 MiB response, 600 s) while every
  non-inference purpose keeps the exact 64 KiB / 30 s probe caps; the
  no-inference-endpoint guard test now scopes to non-inference purposes and
  additionally pins the inference set to exactly that one purpose;
- catalog pricing capture (`OpenRouterPricing` decimal strings bounded to
  [0, 1] USD/token, canonical pricing digest) via
  `observe_openrouter_with_pricing`, and exact-`Decimal` ceiling cost
  arithmetic `completion_cost_microunits`;
- `openrouter_executor` preflight binding endpoint, model, routing-policy,
  and pricing digests into one composite identity digest carried as
  `CliIdentity(openrouter, <digest>, 1.0.0, 1.0.0)`;
- `execute_openrouter`: exactly one canonical, schema-bound, temperature-0
  chat completion with strict `response_format`, fail-closed usage parsing,
  and a hard cost ceiling (`cost_ceiling_exceeded`); schema-digest mismatch
  and credential absence fail before any transport call;
- the atomic whole-file edit engine `apply_whole_file_edit`: full validation
  (exact scope-set equality, hostile-path battery including traversal,
  absolute, drive, backslash, symlinked-parent, temp-name collision, and
  byte caps) completes before any filesystem change; staged temps are
  promoted with `os.replace` and a mid-set failure restores every
  already-replaced file byte-identically (`edit_scope_violation` /
  `edit_apply_failed`);
- snapshot, lifecycle-binding, promotion, and route-pool parity proven by
  tests. Two provider-assumption gaps surfaced and were fixed minimally:
  the `capability_snapshots` and `cli_execution_attempts` provider CHECK
  now admits `openrouter` (newly created stores only — existing stores,
  including the current live fixture store, retain the narrower baked-in
  constraint and will reject OpenRouter rows until deliberately migrated
  or replaced under an approved manifest), and model-name validation gained
  a dedicated rule permitting one `vendor/model` slash without widening the
  global identifier charset;
- the pre-existing guard test asserting `ProviderId` covers only the two
  CLI adapters was updated to the approved three-provider set.

Verification at the final offline state:

- focused new selections (openrouter executor/probe, probe runner,
  profiles, process runner): 127 passed;
- full routing selection (35 modules): 621 passed, 1 intentional skip;
- complete offline suite with out-of-repo `--basetemp`: 1,780 passed,
  44 intentional skips (previously 1,716/44 — 64 new tests);
- repository-wide Ruff and `git diff --check`: clean;
- canonical `--llm none` rebuild fresh: 7,679 nodes, 17,142 edges,
  152 communities.

Claude and Codex adapter argv and behavior are byte-unchanged; their
existing exact-argv tests pass unmodified. No live acceptance for
OpenRouter has occurred: catalog probing, verification bundles, edit
smokes, reviews, and pool registration each require a displayed manifest
and explicit operator approval, and the store-constraint note above must
be resolved as part of that live phase.

## OpenRouter catalog-probe attempt 1: fail-closed, root-caused, fixed offline (2026-07-20)

The operator approved catalog-probe bundle
`19a889534816a7a6489db249b9a74bd990ee53374ce943384a18df865a179b9b`
(purpose `graphite_openrouter_catalog_probe_batch`, five slugs, zero
inference, persistence none). Execution failed closed on the first slug with
sanitized receipt `{action: openrouter_catalog_probe_1, slug:
moonshotai/kimi-k3, failure_category: probe_unavailable, duration_ms: 344}`;
no receipts advanced, the store hashes were untouched, and no retry was
performed under that manifest.

Root cause (reproduced deterministically without the credential): OpenRouter
serves chunked responses with no `Content-Length`. Under the transport's
`Connection: close` discipline, `http.client` closes the socket when the
final chunk is consumed, and `_read_response`'s next loop iteration called
`settimeout` on the dead socket — `OSError` WinError 10038 on Windows —
which the transport sanitizes to `probe_unavailable`. The path was never
exercised live before: Ollama responses carry `Content-Length` (the
declared-size break avoids the extra iteration), an unauthenticated auth
call fails on HTTP status before any body read, and the offline fakes did
not model socket closure.

Two offline corrections, each test-first:

- `_read_response` now stops when the response reports itself complete
  (`isclosed`) before touching the socket again; a deterministic fake
  reproducing the closed-socket-after-final-chunk sequence fails on the old
  code with `probe_unavailable` and passes with the fix.
- A second latent fault was found immediately behind the first and fixed in
  the same pass: the live models catalog measured 524,192 bytes / 338
  entries (diagnostic, unauthenticated, size-and-count only), exceeding the
  64 KiB metadata cap and guaranteeing `probe_response_limit` on the next
  attempt. The transport gains `MAX_CATALOG_RESPONSE_BYTES` (4 MiB) scoped
  to the `OPENROUTER_MODELS` purpose only, and `observe_openrouter` passes
  that allowance solely on the models call; every other non-inference
  purpose keeps the exact 64 KiB / 30 s caps, and the entry-count bound
  (2,048) is unchanged.

Verification after the fixes: affected modules 86 passed; full routing
selection 625 passed, 1 intentional skip; complete offline suite 1,784
passed, 44 intentional skips; Ruff and `git diff --check` clean. A fresh
catalog-probe manifest pinned to the new implementation commit is required
before any further live attempt.

## OpenRouter catalog-probe r2: passed (2026-07-20)

The operator approved fresh bundle
`6a6a71892bc5dbecec6dbf2cd2a835173574d8a10eebb1a273fe69548a59a9bf`
(purpose `graphite_openrouter_catalog_probe_batch_r2`, implementation
commit `c2f2f675222e9186cea1df7b1c930f4163ea7426`). Execution passed with
zero inference requests, persistence none, and both store hashes verified
unchanged post-flight. All five requested slugs exist in the catalog with
pricing captured at probe time (USD per token, exact catalog strings):

| slug | prompt | completion | duration_ms |
| --- | --- | --- | --- |
| moonshotai/kimi-k3 | 0.000003 | 0.000015 | 473 |
| moonshotai/kimi-k2.7-code | 0.00000085 | 0.0000038 | 310 |
| moonshotai/kimi-k2.6 | 0.000000684 | 0.00000342 | 326 |
| z-ai/glm-5.2 | 0.000000973 | 0.000003058 | 331 |
| meta/muse-spark-1.1 | 0.00000125 | 0.00000425 | 314 |

Composite identity digests (endpoint+model+pricing+routing) per slug:
kimi-k3 `51b617d9078e248c8e0622b4ce0a0b2a4ab426ef7598261d566c994497b5518e`;
kimi-k2.7-code `b6db813804d86c546a6bab908675e25b2638e074a977136f70c86212f774a9c7`;
kimi-k2.6 `54210070138299ac4930de833999aaf44e66d1eba7dfba94771107385911a238`;
glm-5.2 `5caaa90d4785a3fb1999dcd7b274644ffa30abfacdd66236324113e63dd08007`;
muse-spark-1.1 `375618987b94aa93060016b1549e52f02e19f102e3d2293a357a70a1cd6cdcf8`.
Shared endpoint digest
`76ef4ad6f0c8a4ae66efb13875c107cee40c78997a212353d379acfbb2f45591`;
shared routing-policy digest
`916df225733c25f6d9976d29edb6b7a2050f61457fa978e178544bcd53615b39`
(policy `{"allow_fallbacks": false}`). Both r1 root-cause fixes are
therefore live-proven: the authenticated chunked auth response was read
cleanly and the 512 KiB models catalog was accepted under the scoped
4 MiB allowance.

The next live phase (read-only verification bundle for the five slugs) is
blocked on the fixture-store provider constraint recorded above: the live
routing store predates the widened provider CHECK and cannot persist
OpenRouter capability snapshots until it is migrated or replaced under an
approved manifest.

## Fixture routing store migrated to schema v6 (2026-07-20)

The operator approved migration bundle
`d403a5c576c8c0a5e564f7d5e92df8fcf811e1c7a23b4b5e6dd9139944b582fd`
(purpose `graphite_routing_store_schema_v6_migration`, implementation
commit `57057b541bc39231f6a9dddcbf3d69a1d1cd06c2`). Execution ran
`RepositoryStore(fixture).initialize()` once, in 786 ms, with zero
network, inference, or credential access and zero row changes to any
table. The store contract before and after the migration is identical
(8 capability snapshots, 8 lifecycle snapshot bindings, 15 telemetry
events, 0 foreign-key violations, integrity `ok`); only the schema
version (5 to 6) and the routing store's own file hash changed, since the
two provider-constrained tables were rebuilt in place. Evidence:

- routing store SHA-256 before/after:
  `ff60056cca576a282fdd0e1069ffb8ed43f21ddc2be71da47b08a25aca706f05` to
  `1bfaae1813288b08bd3c73911ab1a36be231f65ffb10521b892ad11cd698572b`;
- lifecycle store SHA-256 unchanged:
  `d7bd02146e98c9b79e102d75c400f046189a6424d30a225126f109489a258d1b`;
- verified pre-v6 backup SHA-256:
  `0a1dbdc3fb59f185d495b3082b6fc62d8850e246ba4d3f72caccc6d3a9783fe1`
  (`events-schema-v5-pre-v6.sqlite3`, integrity `ok`, stamped schema
  version 5), enabling database-restore rollback if ever needed.

The fixture routing store now accepts OpenRouter capability snapshots.
The next live phase is a read-only verification bundle for the five
probed slugs, each requiring its own approved manifest with token and
cost bounds.

## OpenRouter profile verification: partial crash, root cause, and repair (2026-07-20)

The operator approved verification bundle
`999f62c727fb18bd46d6aa147a826c9de351c7849c17ef6411159133936da6c9`
(purpose `graphite_openrouter_profile_verification_batch`, implementation
commit `69d23a92ad27da0caa1a173a4d2c99f83de41f8e`, five independent
per-slug actions, `stop_on_failure: false`). This was the first live
inference call to OpenRouter under this feature.

**Slug 1 (moonshotai/kimi-k3):** failed closed with `failure_category:
unavailable` at the transport/execute layer, sanitized receipt only, zero
persistence. The partial-success design worked exactly as intended: the
harness recorded the failure and moved to the next slug.

**Slug 2 (moonshotai/kimi-k2.7-code):** the real call succeeded --
schema-validated response matched exactly -- and its capability snapshot
and lifecycle binding persisted correctly. The harness then crashed
writing telemetry: `telemetry.py`'s `_SAFE_MODEL` regex was a sibling
copy of the pattern already widened in `storage.py`'s `_model_identifier`
(commit 52bca89) but was never itself widened, so
`CliTelemetryRecord.__post_init__` raised an uncaught `ValueError` for
any vendor/model slug. Root-caused and reproduced offline deterministically
(no live call needed); fixed in commit `1ce1492` with a failing test
first. Full offline verification after the fix: telemetry module 10
passed; routing selection 628 passed, 1 intentional skip; complete suite
1,787 passed, 44 intentional skips; Ruff and `git diff --check` clean;
graph fresh.

**Remaining slugs 3-5** (kimi-k2.6, glm-5.2, muse-spark-1.1) were never
attempted; the crash happened mid-batch on slug 2.

**Store state and repair.** Before investigating any fix, the store was
checked read-only: the kimi-k2.7-code capability snapshot
(`99db4a4a53aac5a97aae36d1d2ace5de3b194c63a8605c59dbd4837446a205f5`) and
its lifecycle binding are genuinely valid -- nothing fabricated -- but
the exact input/output token counts from that call existed only in the
crashed process's memory and are unrecoverable. The operator's first
choice, retracting the orphaned rows and re-verifying fresh, was checked
against the schema before any action was taken: `capability_snapshots`
and `lifecycle_snapshot_bindings` both carry unconditional
`BEFORE DELETE ... RAISE(ABORT, '...immutable')` triggers -- the store is
append-only by design, and the delete would simply have aborted. This was
surfaced back to the operator, who then approved the alternative: complete
the record with the token counts recorded as unknown, matching the
existing `cost_status: "unknown"` precedent already used for every CLI
provider call.

Repair bundle
`c7b3ffa6899b361c6ed97788756c4b8cedbb55464d0f24b42f63426f6bac432f`
(purpose `graphite_openrouter_telemetry_repair_kimi_k2_7_code`, zero
network, zero inference, zero cost) wrote exactly the two missing
telemetry records (machine-verified and human-accepted, `input_tokens`,
`output_tokens`, and `latency_ms` all `null`) for the existing snapshot
digest, verified against the exact persisted snapshot facts
(provider, requested_model, verified_at) before writing. Final audit
matched exactly: capability_snapshots 9 (unchanged), lifecycle_snapshot_bindings
9 (unchanged), telemetry_events 15 to 17, zero foreign-key violations,
integrity ok. Duration 123 ms.

The fixture store now holds one complete, promoted-to-active OpenRouter
capability snapshot (`kimi-k2.7-code`, read-only, RiskTier.HIGH) with a
full and honest evidence trail. The next live phase is a fresh
verification attempt for the four remaining slugs (kimi-k3 retry,
kimi-k2.6, glm-5.2, muse-spark-1.1), each requiring its own approved
manifest.

## OpenRouter profile verification round r2: one more verified, a pattern to chase (2026-07-20)

The operator approved bundle
`0220b2fe10538f6dfbaab28e1378d10d65a4931b84209d542b22c4046f5dd79b`
(purpose `graphite_openrouter_profile_verification_batch_r2`,
implementation commit `fd8012c3d035e00be6e92319db73c59993de3c3f`,
covering the four slugs round r1 left unresolved). Before executing,
the per-model exception handling was hardened with a catch-all inside
the per-slug loop (any exception type, not just the three anticipated
classes, now records that slug's failure and continues the batch) --
a direct, code-only response to the r1 crash that does not alter the
already-approved bundle content (confirmed by an unchanged bundle
digest before and after the change).

The batch ran to completion (no crash). Result: `moonshotai/kimi-k2.6`
verified successfully -- real call, 23 input tokens, 1,389 output
tokens, cost 4,767 microunits (~$0.005), 22.2 s -- and is now the
second active OpenRouter capability. `moonshotai/kimi-k3`,
`z-ai/glm-5.2`, and `meta/muse-spark-1.1` all failed closed with
`failure_category: unavailable`, zero persistence each. Final audit
matched the delta formula exactly: capability_snapshots 9 to 10,
lifecycle_snapshot_bindings 9 to 10, telemetry_events 17 to 19, zero
foreign-key violations, integrity ok.

The three failures share one exact characteristic that the two
successes (kimi-k2.7-code, kimi-k2.6) don't: a 1,048,576-token context
window (the successes are both 262,144). `execute_openrouter`'s
transport-failure handling collapsed every non-2xx/timeout/oversized-body
outcome into a single opaque `unavailable` code -- a real gap against
the design spec's own stated intent ("HTTP failure ... provider-process-failure
class with status class and body hash only") that Task 5's
implementation never delivered. Fixed offline (commit `f54f21e`):
`probe_response_limit` now maps to the existing precedent code
`response_limit` and `probe_timeout` to `timeout` (both already used by
the Codex adapter), leaving `unavailable` for genuine HTTP-status
failures. No raw provider output is exposed by this change. Full
verification: openrouter_executor module 50 passed; routing selection
632 passed, 1 intentional skip; complete suite 1,791 passed, 44
intentional skips; Ruff and `git diff --check` clean; graph fresh.

A round r3 with this diagnostic improvement live, targeting the same
three slugs, is the next step to determine whether they are genuinely
unavailable on OpenRouter right now or hitting the response cap on
verbose reasoning output.

## OpenRouter profile verification round r3: diagnostic, inconclusive on cause (2026-07-20)

The operator approved bundle
`99b78b74e63bef071eca3fa68477033c6e61026f7bce593ff9de1405cc397b4c`
(purpose `graphite_openrouter_profile_verification_batch_r3`,
implementation commit `e3a0abf5407f0312e9b97cbbbe8fd7d2c6b74231`), a
diagnostic-only re-attempt of the three still-unresolved slugs with the
newly split `response_limit`/`timeout`/`unavailable` codes live, run
purely to observe which bucket occurred, not to force a different
outcome.

All three (`moonshotai/kimi-k3`, `z-ai/glm-5.2`, `meta/muse-spark-1.1`)
again failed with `failure_category: unavailable`, zero persistence
each. This rules out response-truncation from verbose reasoning output
and rules out a deadline/timeout cause -- the two leading hypotheses
after round r2. `kimi-k3` has now failed identically in three
consecutive rounds; `glm-5.2` and `muse-spark-1.1` in two. Final audit
confirmed the store was untouched: capability_snapshots 10,
lifecycle_snapshot_bindings 10, telemetry_events 19, unchanged from
before the round, integrity ok.

`unavailable` in `execute_openrouter` covers a genuine HTTP-status-level
rejection or any other non-response-limit/non-timeout transport failure.
No raw provider diagnostics are captured by design, so this cannot be
narrowed further without either a live capacity check against OpenRouter
directly (outside the governed non-inference probe's current scope) or
another status-class split, at the cost of a fourth approval round for
uncertain diagnostic payoff. The consistent, repeatable pattern across
three attempts on the same 1,048,576-context axis is more consistent
with these three models genuinely having no active serving capacity on
OpenRouter right now than with a bug in this implementation -- the two
successfully verified models (`kimi-k2.7-code`, `kimi-k2.6`) are both
262,144-context and passed cleanly.

Two OpenRouter capability snapshots remain active:
`moonshotai/kimi-k2.7-code` (`99db4a4a...`) and `moonshotai/kimi-k2.6`
(`6816edaf...`). Whether to keep chasing the remaining three, or proceed
to edit-smoke and pool registration with the two verified models, is an
operator decision.

## OpenRouter edit smoke r1: harness bug plus a model-quality miss (2026-07-20)

The operator approved bundle
`67525c07c998213cde9da5c393287b1d291b471f548313e9613c0fd85187b736`
(purpose `graphite_openrouter_edit_smoke_kimi27_r1`), the first live
write-authority test for OpenRouter -- `moonshotai/kimi-k2.7-code`
against `src/access.py` and `tests/test_access.py` in a fresh isolated
worktree, using the whole-file replacement engine for the first time
against a real model. Execution failed with the opaque
`failure_category: harness_failed`; the routing store was confirmed
unchanged (10 capability_snapshots, 10 lifecycle_snapshot_bindings, 19
telemetry_events -- no partial write this time).

Read-only inspection of the created worktree (a harmless disk artifact,
not a governed store) showed both files had been overwritten with the
literal text `"===== src/access.py ====="` / `"===== tests/test_access.py ====="`
-- the section-header delimiters from the prompt, not real code. The
model's structured response was schema-valid (a non-empty string under
every length cap), so `apply_whole_file_edit` wrote it correctly; the
content itself was wrong. This is very unlikely to be response-budget
truncation (16,384 output tokens against files of a few hundred bytes
each) and looks instead like the delimiter format in the prompt
confusing the model's completion for the `content` field specifically.

Separately, and this is what actually produced the opaque
`harness_failed` rather than the intended `test_validation_failed`: the
harness script's `run_validation` helper (reused from
`_execute_live_batch.py`) raises that module's own locally-defined
`HarnessFailure` class, which is a distinct class object from this
script's own identically-named `HarnessFailure` -- Python's `except
HarnessFailure` did not match it, so the real failure code and evidence
were discarded into the generic catch-all. Fixed in the harness script
(not the graphite implementation -- this bug is confined to disposable
acceptance-test code, not the codebase under test): the imported
exception class is now caught explicitly and translated into this
script's own evidence-carrying exception. Confirmed the bundle digest
is unchanged by this fix (prepare-script content untouched).

The prompt's delimiter format is being revised before any further
attempt (explicit instruction that delimiter lines are not file content)
under a fresh manifest with a new worktree task id, since a byte-identical
retry at temperature 0 would likely reproduce the same output.

## OpenRouter edit smoke r2: harness fix confirmed, new truncation pattern (2026-07-20)

The operator approved bundle
`38f9a05456efc06df09c84f062aa25853b564cff4bf674130e32cb7665782a1e`
(purpose `graphite_openrouter_edit_smoke_kimi27_r2`), a retry with a
fresh worktree and a revised prompt (explicit BEGIN/END delimiters with
an instruction never to echo them, replacing r1's ambiguous `=====`
markers). Execution failed with `failure_category: test_validation_failed`
-- a real, specific code this time, confirming the r1 harness bug
(`run_validation` raising a same-named-but-distinct exception class) is
fixed. Store confirmed untouched: capability_snapshots 10,
lifecycle_snapshot_bindings 10, telemetry_events 19, unchanged.

The delimiter-echo pattern from r1 did not recur. Instead, both files
were written as exactly their first line and nothing else, with no
trailing newline: `src/access.py` contains only
`def can_read_record(actor_tenant: str, record_tenant: str) -> bool:`
and `tests/test_access.py` contains only
`from src.access import can_read_record`. The response was schema-valid
(passed strict json_schema validation, exactly the two required files,
each parsed to a well-formed content string) so this is not a
transport-level truncation -- `execute_openrouter` would have raised
`response_limit` before any write if it were. Both fields stopping at
exactly one line, independently, with a 16,384-token budget dramatically
larger than either file needs, does not look like ordinary
budget-exhaustion truncation; it looks like the model terminating each
`content` string early for some other reason specific to this
two-file, whole-file-replacement request shape.

Two edit-smoke attempts have now failed for two different reasons
(prompt-format confusion in r1, early truncation in r2). Whether to
try a third attempt -- e.g. splitting the request into two independent
single-file edit calls to reduce per-call output complexity -- or take
a different approach is an operator decision recorded separately.

## OpenRouter edit smoke r3: transient transport failure on first sub-call (2026-07-20)

The operator approved bundle
`93df22386f69abb0174f9bbe71f88f30225bf4d6e8bc962acd1f28ed544c6db7`
(purpose `graphite_openrouter_edit_smoke_kimi27_r3`), the two-single-file-call
redesign. Execution failed on the first sub-call (`access_py`) with
`failure_category: unavailable` before any content was received; the
second sub-call never ran, `apply_whole_file_edit` was never invoked, and
the store was confirmed untouched (capability_snapshots 10,
lifecycle_snapshot_bindings 10, telemetry_events 19). The worktree's
`src/access.py` is byte-identical to the pristine baseline.

Unlike the earlier three-model `unavailable` pattern (repeated
identically across 2-3 rounds each for kimi-k3/glm-5.2/muse-spark-1.1),
this is the first `unavailable` result for `moonshotai/kimi-k2.7-code`
across every round so far -- it passed profile verification cleanly and
both prior edit-smoke attempts got well past this exact call (through to
schema-valid responses, in both cases). Nothing in this result points at
the split-call design or the prompt; it reads as an isolated transient
transport failure rather than a structural issue. A same-design retry
under a fresh manifest is the next step.

## OpenRouter edit smoke r4: first full content, deterministic first-line truncation (2026-07-20)

The operator approved bundle
`9aae26d85e53564e29db1c01fedda7813bf5e4f4c8749d449649e2ed3c46ef5c`
(purpose `graphite_openrouter_edit_smoke_kimi27_r4`), a byte-identical
same-design retry of r3 (prompt and schema hashes match r3 exactly)
under a fresh worktree (`openrouter-edit-kimi27-r4`) and a fresh
manifest -- a pure retry-with-new-approval on the hypothesis that r3's
first-sub-call `unavailable` was transient. Execution failed with
`failure_category: test_validation_failed`. For the first time across
all four rounds, *both* sub-calls completed and returned content:
`access_py` (263 input / 401 output tokens, 1748 microunits, 6848 ms)
and `test_access_py` (325 input / 196 output tokens, 1022 microunits,
3249 ms); total 2770 microunits, each call under its per-call ceiling.
The store was confirmed untouched (capability_snapshots 10,
lifecycle_snapshot_bindings 10, telemetry_events 19, integrity ok).

The failure is the r2 truncation pattern recurring: both files were
overwritten with exactly their first line and no trailing newline.
`src/access.py` contained only
`def can_read_record(actor_tenant: str, record_tenant: str) -> bool:`
(a function signature with no body -- a `SyntaxError`, so pytest cannot
import it, which is why the test gate, not `git diff --check`, is the
one that failed), and `tests/test_access.py` contained only
`from src.access import can_read_record`. The r3/r4 split-call redesign
therefore did *not* fix the truncation: r4 is the first round in which
the split design actually produced content, and it truncated exactly as
the single-call r2 did.

### Root cause is the endpoint, not graphite (code-traced)

The whole-file content path was read end to end. `execute_openrouter`
extracts the model completion, does `json.loads(content)` and returns
`json.dumps(structured)` -- a faithful JSON round-trip with no
line-based logic, so a multi-line `content` string survives intact
(newlines become `\n` escapes and back). `apply_whole_file_edit`
writes the full `content.encode("utf-8")` bytes; there is no
`splitlines`, first-line, or newline-boundary logic anywhere in the
parse or apply path. The single-file schema's `content` field is
`{"maxLength": 1048576, "type": "string"}` (a full file is permitted),
and the prompt explicitly demands the complete runnable source with the
original embedded between BEGIN/END markers. The 401 / 196 output
tokens against a one-line result are `reasoning: {effort: HIGH}`
reasoning tokens counted in `completion_tokens` -- 401 is far below the
16,384 `max_output_tokens`, and a transport-level cap would have raised
`response_limit` before any write rather than silently truncating. So
the one-line `content` field arrived from OpenRouter already truncated;
graphite reproduced it faithfully.

### What "verification" actually proved -- and the decisive consequence

The OpenRouter profile-verification round that promoted
`kimi-k2.7-code` and `kimi-k2.6` as active capability snapshots did call
`execute_openrouter` through the *same* structured-output content
channel, but its `VERIFY_SCHEMA` field is
`{"verification": {"const": "GRAPHITE_PROFILE_OK"}}` -- a single
`const`-pinned field -- and it only checks
`json.loads(result.message) == {"verification": "GRAPHITE_PROFILE_OK"}`.
That is ~40 bytes with zero free-form generation: strict json_schema
pins the only field to one allowed value. So "verified" establishes
only that the endpoint returns a const acknowledgment; it is *not*
evidence the model can emit a large free-form string field. The precise
boundary across all evidence: strict `json_schema` structured output on
this endpoint reliably carries a const / trivial field but does not
carry a large free-form `content` string (r1 echoed delimiter text, r2
and r4 truncated to line one; three distinct content failures, never
one correct file).

This matters because OpenRouter models have no local CLI. Claude and
Codex deliver edits by writing into the worktree and letting graphite
read the resulting diff; an OpenRouter model can only return
work-product through this structured-output content channel. That
channel is therefore the *only* possible edit path for any OpenRouter
model, and it has never carried a real free-form payload. The current
pool-readiness of the two verified models does not establish edit
capability.

### Operator decision (2026-07-20): request-shape experiment

Re-running the same approved digest was ruled out: unlike r3's transient
`unavailable`, this failure is deterministic at temperature 0 and would
truncate identically. The operator chose a controlled request-shape
experiment as the next step: change `execute_openrouter` to stop using
`strict` json_schema for the content field (a `json_object` contract, or
plain text with graphite parsing a fenced code block), changing that one
variable while holding reasoning effort and everything else constant.
This is simultaneously the candidate fix and the only remaining way to
disambiguate the model itself from OpenRouter's strict-structured-output
layer, because raw provider output is not persisted
(`raw_provider_output_persistence: false`) and graphite cannot observe
the completion before that layer. A full free-form file returned under
the relaxed contract would prove the strict-structured layer was the
cause and would fix the channel for every OpenRouter model; a repeated
line-one truncation would localize the fault to the model. The change
touches graphite implementation code and so requires a fresh manifest
and explicit operator approval before any live call.

## OpenRouter edit smoke r5: json_object transient (2026-07-20)

Implementation commit `d5ac22c` added a keyword-only
`response_format_type` parameter to `execute_openrouter` (default
`json_schema` preserves the strict block and every existing caller; the
new `json_object` value sends `{"type": "json_object"}` instead). The
output schema is still canonicalized and its digest pinned in both modes,
and response parsing is unchanged, so switching modes changes exactly one
variable. The operator approved bundle
`bb0384f46786a023776249a9abf07dbad18a8af38127276e3d11c7b71bca0b1c` (r5,
the split two-call design under json_object). Execution failed
`unavailable` on the first sub-call before any content, after preflight
and worktree creation succeeded; zero persistence (store 10/10/19,
worktree pristine). This is the r3 transient class; the json_object
hypothesis was not yet tested.

## OpenRouter edit smoke r6: json_object accepted, per-call flakiness (2026-07-20)

`execute_openrouter` previously collapsed every transport failure except
`response_limit`/`timeout` into one opaque `unavailable`. Implementation
commit `6906a7d` split provider HTTP rejections
(`probe_http_status`/`probe_redirect_rejected`) out to a distinct
`http_status` code so a rejection is diagnosable from a genuine transient
(`dns_busy`/`unavailable`/`failed` stay `unavailable`). The operator
approved bundle
`eef9df321dae0733e585f3f5a0273e99a1498bf299a39c402abcdd5d3b196a28` (r6,
split json_object, diagnostic-hardened). Result: the first sub-call
(`access_py`) **succeeded** -- a schema-valid `GRAPHITE_EDIT_OK` response
came back (263 input / 270 output tokens) -- and the second sub-call
failed `http_status`. Two conclusions: json_object is **accepted** by the
endpoint (the rejection hypothesis is dead), and the failure is an
intermittent per-call transport flakiness, not a contract problem --
across rounds, r3 dropped call one, r5 dropped call one, r6 dropped call
two, and only r4 landed both. The split design's requirement that *both*
flaky calls land is why the pytest gate was almost never reached. Zero
persistence (store 10/10/19, worktree pristine).

## OpenRouter edit smoke r7: json_object fixes the truncation -- PASSED (2026-07-20)

Because the split design multiplied the per-call flakiness, r7 returned
to a **single** two-file call (the r1/r2 structure) with the r2 prompt
and schema byte-for-byte unchanged, differing from r2 in exactly one
variable: `response_format_type=json_object` instead of the strict
json_schema block. Only one completion has to land to reach
apply->diff->pytest, making it robust to the per-call flakiness. No
graphite change was required (`execute_openrouter` already supports
json_object and `apply_whole_file_edit` already accepts a multi-file
payload), so `IMPLEMENTATION_COMMIT` stayed `6906a7d`.

The operator approved bundle
`113f0f9ea2524cc1b267112f4421e80ff712cbc5ce459cbe4ae8aa808349b67f`.
Execution **passed**: a single json_object completion (414 input / 451
output tokens, 2066 microunits, 7547 ms) returned complete, correct
source for both files; `apply_whole_file_edit` wrote exactly the two
scoped files (`changed_file_count` 2, `changed_byte_count` 1007 -- real
content, not a first-line truncation); `git diff --check` was clean and
`pytest` passed against the genuinely edited function
(`validation_outcome: passed`). The edit profile for
`moonshotai/kimi-k2.7-code` was promoted
(`capability_snapshot_digest 500cda19a907c53df9433dde2ec4cab58f0aa10e109420417fbbd3bac7ec9574`,
`diff_sha256 005f1ae8ae072d35b003b3804cb9b3c0dee49058f4e488eddb3cb3031702b93e`
now pinned), and the final audit confirmed the store moved 10/10/19 ->
11/11/20 with integrity ok and no foreign-key violations.

This is the first end-to-end OpenRouter edit-smoke success across seven
rounds, and it **confirms the root cause**. The same model with a
byte-identical prompt truncated its free-form `content` field to the
first line under strict json_schema structured output (r2, r4) and
returned the complete file under json_object (r7). Strict-schema-
constrained decoding was therefore the truncator, not the model and not
any graphite parse/apply defect; relaxing the response contract to
json_object fixes the whole-file edit channel for every OpenRouter model,
since the channel is shared. r1's delimiter echo was a separate,
already-fixed prompt-format issue; the r3/r5/r6 `unavailable`/`http_status`
results were intermittent transport flakiness on individual completions,
independent of the contract question. The two verified models
(`kimi-k2.7-code`, now also edit-promoted, and `kimi-k2.6`) can proceed
to pool registration; a future edit-smoke for `kimi-k2.6` should reuse
this single-call json_object shape.

## OpenRouter edit smoke r8: kimi-k2.6 edit-promoted, fix proven on a second model -- PASSED (2026-07-20)

r8 reused r7's single-call json_object shape unchanged -- same prompt and
schema byte-for-byte -- with the model swapped to `moonshotai/kimi-k2.6`
and the manifest re-pinned to the post-r7 state (implementation commit
`29ee64a`, existing store contract 11/11/20, current routing-store hash;
the lifecycle store is untouched by the edit-smoke path). No graphite
change was needed. The cost ceiling was recomputed from k2.6's own
(cheaper) pricing.

The operator approved bundle
`579c69e41b42c76cdb159e30c87cb5f256f62465efee4af69e84104588fd43db`.
Execution **passed**: a single json_object completion (414 input / 3281
output tokens -- k2.6 spent far more reasoning than k2.7-code's 451, and
took 42.6 s vs 7.5 s -- 11505 microunits) returned complete, correct
source for both files; `apply_whole_file_edit` wrote exactly the two
scoped files (`changed_file_count` 2, `changed_byte_count` 1007); git
diff --check was clean and pytest passed (`validation_outcome: passed`).
The edit profile for `moonshotai/kimi-k2.6` was promoted
(`capability_snapshot_digest 1b2a7c8e99452b1ff1132545b160e88f4f6abd7864154e05b413faa310f15936`),
and the final audit confirmed the store moved 11/11/20 -> 12/12/21 with
integrity ok and no foreign-key violations.

The decisive corroboration: r8's `diff_sha256` is
`005f1ae8ae072d35b003b3804cb9b3c0dee49058f4e488eddb3cb3031702b93e` --
**byte-identical to r7's**. Two different OpenRouter models produced the
exact same edit, which both validates the pinned reference diff for
cross-provider review and strengthens the causal finding: the json_object
contract, not any model-specific behaviour, is what carries a complete
free-form edit. The shared-channel fix is now proven on two of two
governed OpenRouter models (`kimi-k2.7-code` r7, `kimi-k2.6` r8), both of
which are now verification- and edit-promoted. Both can proceed to pool
registration as edit-capable; any additional OpenRouter model added later
should still get its own single-call json_object edit-smoke before
edit-promotion.

## OpenRouter ISOLATED_CODE edit-pool registration (offline loadability + selectability proof)

2026-07-20. With both OpenRouter edit profiles promoted (r7 `kimi-k2.7-code`,
r8 `kimi-k2.6`), this step proved -- offline, read-only, with zero store
mutation -- that the two edit-promoted models are loadable, correctly ordered
`ApprovedRoutePool` candidates for the ISOLATED_CODE edit category and are
selected in order by `select_route` against their live ACTIVE
`RouteAuthority`s. This closes the pool-coordinator gap that r7/r8 bypassed by
calling `execute_openrouter` directly; it spent no budget and made no network
call. Design and plan:
`docs/superpowers/specs/2026-07-20-openrouter-pool-registration-design.md`,
`docs/superpowers/plans/2026-07-20-openrouter-pool-registration.md`. The
harness pair `_prepare/_execute_openrouter_pool_registration.py` follows the
rN convention but is non-inference and read-only.

Resolution recorded: the design's step 3 framed approvability as an
`ApprovalAuthority.issue` call, but reading the code showed `issue()` calls
`store.save_approval_record(...)`, which writes the routing store and would
violate the `mutated: false` guarantee. The proof therefore does NOT call
`issue()`; approvability is proven by successful `ApprovedRoutePool`
construction, which enforces the full cross-candidate
permission/risk/trust/capability/expiry/budget envelope. `select_route`
requires no signature; the signed-approval path remains covered by
`test_route_pool`/`approval` unit tests and by the out-of-scope live routed
smoke through `execute_approved_route_pool`.

Read-live was verified byte-safe before the run: both stores are already
SQLite WAL, so the store's `journal_mode = WAL`-on-connect is a no-op on the
main file and read-only queries append zero frames; a hash round-trip left
both `.sqlite3` files byte-identical with no sidecars. The harness therefore
reads the live stores directly -- no copy.

The operator approved bundle
`e9510df6e8d4feca40a54ca3b84c9019eb450bb9bdf67d999770657abd26ad20`. Execution
**passed**. Pool composition (category ISOLATED_CODE, WORKSPACE_WRITE,
`pool_digest 41af6a65404f93177143910d3ad24a79b334a7c72eea54495ea9b910aeecbf08`):

- `candidate[0]` primary `openrouter-kimi27-code-primary` --
  `moonshotai/kimi-k2.7-code`, edit snapshot
  `500cda19a907c53df9433dde2ec4cab58f0aa10e109420417fbbd3bac7ec9574`, ACTIVE
  identity `c8cece35646deec30fa9538ba998722781074027a6bdd2dabafd1986359439ab`,
  candidate digest
  `da0e80ea4352decc741fbefe1af23dd68788e6ba0faaaf6925817df1fec1763f`;
  `loadable: true`, `selectable: true`, `authority_state: active`.
- `candidate[1]` capacity fallback `openrouter-kimi26-fallback` --
  `moonshotai/kimi-k2.6`, edit snapshot
  `1b2a7c8e99452b1ff1132545b160e88f4f6abd7864154e05b413faa310f15936`, ACTIVE
  identity `6333c4d577f8fcb111f57f23c4c2f6b3ab889cc3ba19edfb78cc082a63f6bade`,
  candidate digest
  `6f6c3b63be272a69741ced869d8f39bd54c4aa798bca69d0ce972e13054220a1`;
  `loadable: true`, `selectable: true`, `authority_state: active`.

`select_route` selected `candidate[0]` with empty attempts, and `candidate[1]`
after one synthesized `capacity_unavailable` attempt on the primary
(`accepted_output: false`, `side_effect_state: none`): `selection.primary =
openrouter-kimi27-code-primary`, `selection.fallback_after_capacity_unavailable
= openrouter-kimi26-fallback`.

Decisive diagnostic -- **positive** (`diagnostic:
edit_snapshots_directly_selectable`): each edit snapshot's
`lifecycle_snapshot_binding` equals the current ACTIVE identity, because
edit-promotion (`promote_bound_capability_snapshot_record`) re-bound the edit
snapshot to the same identity that verification activated, and the edit-smoke
path never touched the lifecycle store. No governed re-activation to the edit
snapshot is required for these two models to be directly selectable.

No store mutation: the receipt reported `mutated: false` with audit 12/12/21,
integrity ok, 0 foreign-key violations; an independent pre/post file hash
confirmed `events.sqlite3` unchanged at `21e73d3f...` and
`provider-lifecycle.sqlite3` unchanged at `a99f7cc4...`, with no `-wal`/`-shm`
sidecars left behind. The proof called neither `execute_openrouter` nor
`ApprovalAuthority.issue`, made no network call, and required no graphite
source change (`test_route_pool` + `test_routing_profiles` remained green, 55
passed). `IMPLEMENTATION_COMMIT 3040c18`, feature worktree clean. Deferred
(out of scope): the read-only review/authorization pool, a live routed smoke
through `execute_approved_route_pool`, and the three unverified models
(`kimi-k3`, `glm-5.2`, `muse-spark-1.1`).
