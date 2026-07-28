<!-- aramid:managed -- regenerated verbatim by `aramid init` every time it runs;
     hand-edits here are overwritten on the next init. Repo-specific config
     (demoted rules, test command override, scan scope, ignore paths) belongs
     in `aramid.toml` instead, which init never overwrites once it exists. -->

# ARAMID.md

This repo is armed with **aramid** -- a deterministic security and quality gate
that runs in git hooks (`pre-commit`, `pre-push`) and is CI-ready via `aramid
check --strict --json`.

- **Detected stack:** python
- **Package manager:** none
- **Onboarded:** 2026-07-28

## What aramid checks

| Gate | Budget | Scope | Tools | Failure mode |
|---|---|---|---|---|
| pre-commit | `[timeouts].pre_commit` (default 5s) | staged files | gitleaks, ruff (security rules) | fail-open |
| pre-push | `[timeouts].pre_push` (default 300s) | changed files | gitleaks, semgrep, eslint, typecheck, dependency audit, tests | fail-closed |

Budgets are named rather than fixed here because a repo can raise them in
`aramid.toml`, and this file is regenerated from a template that cannot see
that config -- a hardcoded number would silently drift out of date.

Secrets (gitleaks) always **BLOCK**. Everything else is severity-tiered:
security-relevant findings block, quality findings warn.

**Slow test suite?** `tests` is BLOCK-tier at pre-push, so a suite that
overruns the budget blocks every push. Point the gate at a fast subset in
`aramid.toml` rather than living on `--no-verify` (which disables gitleaks
and the ratchet too):

```toml
[tests]
command = ["pytest", "-q", "tests/unit"]   # argv form: no shell, no quoting
timeout_s = 300                            # capped by [timeouts].pre_push
```

Note `timeout_s` cannot exceed the gate budget -- the gate abandons the
runner at `[timeouts].pre_push` regardless -- so raise both if you need a
longer run. `enabled = false` removes the gate entirely. Subsetting narrows
what the push gate covers, so keep the full suite running in CI.

**Suite not recognized?** Detection is literal: a real `test_*.py`,
`*_test.py`, or `conftest.py` file, or a `package.json` `scripts.test`
entry -- nothing more. A custom pytest `python_files` pattern,
unittest-style `testfoo.py` naming, or a doctest-only suite are all
invisible to it. If that's this repo's suite, the tests runner is never
*selected* at all (not degraded, just absent), so the gate can exit clean
without ever running it. aramid prints a stderr notice when a plausible
test setup exists but nothing was recognized in it -- if you see it, set
`[tests].command` to point aramid at your suite explicitly.

**Dual-stack repo?** If this repo has both a real Python test file
(`test_*.py`, `*_test.py`, or `conftest.py`) and a `package.json`
`scripts.test` entry, aramid runs **both** suites at pre-push and blocks
unless both pass -- not just whichever one it finds first. The npm side
only joins the run when a JS lockfile (`package-lock.json`,
`pnpm-lock.yaml`, or `yarn.lock`) is present; without one, aramid runs
pytest only and prints a notice rather than silently dropping the npm
suite. If either suite's own tool binary can't be found at all, the push
blocks with an explicit `tests-tool-missing` finding instead of an
unexplained failure.

A **two-week WARN-only bake** is in effect for semgrep on this repo (see
`aramid.toml`'s `bake_started` / `semgrep_block_armed`). While unarmed,
semgrep BLOCK-tier findings report as WARN so the operator can demote noisy
rules first. End the bake explicitly with `aramid arm` -- there is no
auto-promotion.

## Always-on triage (Phase 2a)

Every commit is scored at zero cost by a post-commit hook (security-surface
paths, risky content, novelty, graphite blast radius). Commits scoring >= 40
join a review queue drained on a schedule (`aramid drain`, Task Scheduler
task `aramid-drain`). The regression attack pack (`.aramid-rules/regression.yml`,
committed) replays rules compiled from resolved findings -- reintroducing a
rotated secret or banned dependency blocks at pre-push. `aramid status` shows
queue depth and drain history; `aramid pack list|add|compile` manages rules.

## Honesty note

Phase 1 is 100% deterministic: no LLM calls, zero tokens. It covers the
secrets, injection, vulnerable-dependency, and crypto-misuse slices of OWASP
via static analysis -- **not** the full OWASP Top 10. Access control (A01),
security misconfiguration (A05), and authentication (A07) are largely out of
scope for this deterministic layer; that coverage is Phase 2 red-team
territory. Don't mistake a clean `aramid check` for full security coverage.

## Commands

- `aramid check [--gate pre-commit|pre-push] [--staged|--range|--all]` -- run the gate manually.
- `aramid status` -- open findings, new-since-baseline, unrotated historical secrets, unreachable candidates (see `ledger mark-rotated` / `mark-not-a-secret` / `mark-unreachable` below).
- `aramid doctor [--fix]` -- verify/repair the toolchain and the installed hook's interpreter.
- `aramid override <id> --reason "..."` -- suppress a WARN finding (ledger-logged).
- `aramid ledger mark-unreachable <id> --reason "..."` -- retire a finding whose tool no longer runs in this repo (de-selected, disabled, or removed) -- see `aramid status`'s "unreachable candidates" section for which ids qualify.
- `aramid arm` -- end the semgrep WARN-only bake.
- `aramid arm --llm` -- end the LLM bake: confirmed-critical LLM findings block at pre-push.
- `aramid uninstall` -- remove aramid's hooks/ARAMID.md/gitignore entries (ledger kept).

## About the full-history secrets scan

`aramid init` ran a one-time full-history gitleaks scan across this repo. Any
hits are recorded in the ledger as historical, non-blocking findings -- see
`aramid status`. **Deleting the line does not fix a leaked credential --
rotate it**, then retire the finding with `aramid ledger mark-rotated <id>
--reason "rotated in <system>"` -- or, if it turns out not to be a secret at
all (common with gitleaks' `generic-api-key` rule), `mark-not-a-secret`
instead of `mark-rotated`.

<!-- /aramid:managed -->
