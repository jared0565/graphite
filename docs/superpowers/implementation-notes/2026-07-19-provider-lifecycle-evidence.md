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
legitimate review and a turn-bounded loop produce different outcomes. The canonical `--llm none` rebuild is
fresh with 7,508 nodes, 16,713 edges, 161 communities, and 183 scanned files.
This correction is offline only: the next Codex edit smoke still requires a
complete fresh manifest and explicit operator approval, and production
readiness remains blocked on that live acceptance.
