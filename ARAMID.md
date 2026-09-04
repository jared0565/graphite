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
| pre-push | `[timeouts].pre_push` (default 300s) | changed files | gitleaks, semgrep, eslint, clippy, typecheck, dependency audit, tests | fail-closed |

Budgets are named rather than fixed here because a repo can raise them in
`aramid.toml`, and this file is regenerated from a template that cannot see
that config -- a hardcoded number would silently drift out of date.

Secrets (gitleaks) always **BLOCK**. Everything else is severity-tiered:
security-relevant findings block, quality findings warn.

**"WARN tier" does not mean "will not block you."** At pre-push a
no-new-warnings ratchet escalates every finding that is NEW in this run from
WARN to BLOCK. It applies to all tools -- ruff, eslint, clippy, semgrep,
dependency audit -- so the tier describes what a *pre-existing* finding does.
Anything you are about to write blocks the push on its first appearance,
whatever its tier, and the intended response is to fix it, override it
(`aramid override <id> --reason "..."`, ledger-logged) or configure the rule
away in the tool's own config.

Two consequences worth stating outright, because both surprise people:

- The ledger baseline, not the tier, is what keeps day-one lint from blocking
  a repo. A brand-new runner reports its existing findings once, they land in
  the baseline, and only later additions escalate.
- **The semgrep WARN-only bake is not a ratchet exemption.** It stops
  pre-existing BLOCK-tier findings from blocking; it does not stop a newly
  written one, which escalates like any other new WARN. That is deliberate,
  not an oversight: the bake exists to absorb the **existing backlog** when a
  repo turns on a large ruleset, and it still does that in full. End it with
  `aramid arm`. The other disarmable producers ARE ratchet-exempt while
  disarmed -- `tdd` and `red-proof` by name, and the LLM and mutation gates
  structurally, by being appended after the ratchet runs.

**What decides whether something is exempt.** One rule, and it is falsifiable
per candidate:

> A new WARN finding is ratchet-exempt if and only if the push's author cannot
> make it go away by changing what they are pushing.

The deps shape-drift advisory qualifies (aramid cannot parse the audit tool's
output; the fix belongs to aramid). `cargo-audit-warnings` qualifies (an
upstream RUSTSEC publication event, usually with no fix available). A semgrep
finding on new code does not, and neither does a clippy lint -- you wrote it and
you can fix it.

`tdd` and `red-proof` are a **documented exception** rather than an application
of that rule: they fail it outright -- you caused them and you can fix them --
and are exempt only because an operator deliberately disarmed the producer. The
LLM and mutation gates are the same exception, implemented structurally. If you
are adding an entry, it belongs under the rule or under that named exception;
"it is noisy" is not a third category.

The full exemption list, since "everything escalates" above is otherwise
absolute: `tdd`, `red-proof`, the deps shape-drift advisory, and
`cargo-audit-warnings`. That last one is the opt-in
`[deps].cargo_audit_warnings`, which surfaces RUSTSEC's informational
advisories (unmaintained/unsound/yanked crates). It is off by default, and
when on it can never block by three independent mechanisms -- it sits outside
the tunable `deps.block_severity` comparison, `policy.classify` returns WARN
for it unconditionally ahead of any `block_rules` promotion, and it is exempt
here. All three are needed: an unmaintained crate stays unmaintained, so a
newly published advisory would otherwise fail a push with no fix available.

**Noisy WARN-tier rule (e.g. ruff `S101` on test asserts)?** `aramid.toml`'s
`block_rules` only demotes/promotes the BLOCK/WARN boundary -- it has nothing
to say about a rule that was WARN-tier to begin with. `aramid override <id>
--reason "..."` suppresses one finding at a time (ledger-logged) but doesn't
scale to a rule that fires on every test file. For that, configure the tool
itself -- e.g. ruff's own `per-file-ignores`:

```toml
[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101"]
```

aramid's own `--extend-select S` flag still respects this; it only adds
rules to what ruff selects, it does not override the target repo's ignores.

**Demoting a BLOCK-tier rule?** Setting `block_rules.<tool>.block` in
`aramid.toml` can only ADD to what your own machine's config (packaged
defaults plus `~/.aramid/config.toml`) already established -- an incomplete
or empty list in a repo's `aramid.toml` no longer drops any OTHER rule, and
aramid prints a stderr notice naming exactly which rule ids it restored when
this happens. This is deliberate: a repo you clone (or a contributor's PR
inside one) cannot silently weaken your BLOCK-tier coverage for everyone who
uses it. To genuinely demote a rule, do it in `~/.aramid/config.toml` on
your own machine -- that layer is the actual floor, and repo config can no
longer remove anything from it.

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

**Fuzz driver timing out on, or executing, a function it should not
touch?** The drain-time fuzz consumer calls every changed top-level
function with generated arguments. A launcher, a `main`, or anything that
hands its arguments to `subprocess` will run whatever it is given -- and a
function that runs your whole suite times the driver out. Skip such names
in `aramid.toml`:

```toml
[fuzz]
skip_name_patterns = [
  "*deploy*", "*delete*", "*remove*", "*drop*", "*push*", "*send*",
  "*upload*", "*kill*", "*wipe*", "*publish*", "*destroy*", "*truncate*",
  "main", "_run",                            # this repo's additions
]
```

Patterns are `fnmatch` globs on the bare function name, case-insensitive.
The first twelve are aramid's packaged defaults; a `[fuzz]` list in
`aramid.toml` REPLACES that list rather than extending it, so keep the
ones you still want.

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

MCP-capable agents reach the same loop as tools, via the `.mcp.json` entry `aramid init` registers (`python -P -m aramid.mcp`): `aramid_check`, `aramid_status`, `aramid_ledger_filter`, `aramid_resolvers`, `aramid_override`, `aramid_mark_not_a_secret`, and `aramid_mark_rotated`.

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
