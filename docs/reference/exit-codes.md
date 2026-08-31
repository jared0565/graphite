# Exit codes

Every command returns an integer from its handler; `graphite` exits with it.
Two rules apply to all of them, from `cli.main`:

| Code | Meaning |
|---|---|
| `2` | Usage error from argument parsing (unknown subcommand, bad flag), or a legacy `--llm`/provider flag on a canonical graph command. |
| `1` | Any unexpected exception: printed as one redacted line `[graphite] error: …`. Set `GRAPHITE_DEBUG=1` to get the traceback. |

Zero results are not failures: `query`, `search`, `impact` and `context`
exit 0 with an empty result — branch on the JSON, and on `answer.grade`,
never on the exit code.

## Graph commands

| Command | 0 | 1 | Other |
|---|---|---|---|
| `scan`, `build`, `report` | completed | error | `build` under the daemon (`GRAPHITE_BUILD_LOCK_REPORT_REFUSAL` set): `75` when another builder holds the lock (`EX_TEMPFAIL`); outside the daemon a held lock is a skipped build and exits 0 |
| `check` | graph is fresh | graph is stale; `--json` names the reason (`engine_changed` vs source changes) | |
| `validate` | `graph.json` is valid | integrity violation | |
| `query`, `search`, `capabilities` | executed (including zero matches) | — | not-found and plan errors are reported inside the JSON with exit 0 |
| `impact`, `context` | executed | input could not be resolved | |
| `watch` | stopped cleanly | — | |
| `debt` | listed | — | |
| `channel` | the channel path was printed (or the requested channel action completed) | the shared channel does not exist at the resolved path | channel actions report their own outcome in the JSON |
| `savings` | estimate printed / display toggled | — | |

## Readiness and health

| Command | 0 | 1 | Other |
|---|---|---|---|
| `doctor` | overall `ready`, `optional` or `degraded` | overall `blocked` (a core safety or execution requirement failed) | |
| `daemon-status` | status read | no status or unreadable | |
| `daemon-health` | health evaluated | `--fail-on-error` and errors are present, or the report could not be produced | |
| `daemon-task-status`, `daemon-startup-status`, `daemon-service-status` | supervisor installed | not installed (or the wrong platform) | |
| `daemon-install-windows`, `daemon-uninstall-windows`, `daemon-install-linux`, `daemon-uninstall-linux`, `daemon-install-macos`, `daemon-uninstall-macos` | supervisor command succeeded | refused (wrong platform, or a launcher that cannot carry `-P`) or the supervisor command failed | |
| `daemon-install-startup-windows`, `daemon-uninstall-startup-windows` | launcher written / removed | — (a failure to write raises and exits 1 through the universal rule) | |
| `incidents list` | listed | — | |
| `incidents ack`, `incidents resolve` | recorded | the incident id was not found or the ledger could not be written | |

## Onboarding

| Command | 0 | 1 |
|---|---|---|
| `init`, `bootstrap` | onboarding files written; activation outcomes `installed`, `already_available`, `not_applicable`, `declined` and `guidance_only` all count as success | an explicitly approved activation ended `validation_failed`, `installation_failed` or `verification_failed` (the onboarding files are kept) |
| `hooks` | trampolines installed / status read | installation failed |
| `audit-replacement` | ready to replace legacy graph tooling | not ready |
| `activate` | activation recorded | — |
| `agent-hook` | **always** — a hook endpoint never blocks the agent | — |

## Review evidence

| Command | 0 | 1 |
|---|---|---|
| `review-changes` | packet built; risk does not affect the exit status | `--fail-on-blocker` hit an evidence blocker, or invalid input / operational error |

## Model overlay and routing

These commands are the approval-gated model boundary; their non-zero codes
are deliberately distinct so a caller can tell a policy refusal from a
failure.

| Command | 0 | Codes |
|---|---|---|
| `overlay build` | overlay written and `outcome_category` is `succeeded` | `2` request rejected (identity digest, provider or routing policy mismatch); `3` canonical graph missing or stale (`canonical_*`); `4` the provider run did not succeed |
| `route recommend` | recommendation produced | `3` the recommendation is a manual hand-off |
| `route run` | executed | `1` invalid request; `2` approval or policy refusal; `3` manual hand-off |
| `route review` | reviewed | `1` invalid request; `2` policy refusal |
| `route policy` | policy applied | `2` policy refusal; recovery errors per the routing docs |
| `route record-outcome` | recorded | `6` the outcome could not be recorded |
| `route accept`, `route reject`, `route cleanup`, `route reconcile`, `route recoverable`, `route status`, `lifecycle *` | performed | recovery and lifecycle error codes are documented with the routing operator docs (`docs/superpowers/implementation-notes/`); a stable error code is always in the JSON |

`tests/test_exit_codes_reference.py` checks that every subcommand the parser
knows appears on this page, so a new command cannot ship without a row.
