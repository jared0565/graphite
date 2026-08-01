# Agent channel broker — design

**Status:** proposed, awaiting operator review
**Date:** 2026-08-01

## Goal

Let every coding agent read and post to the shared agent channel **without any
agent process holding write access outside its own repository**, and make the
whole correspondence auditable by a human from a terminal at any time.

## Why this exists

The channel (`.agent-channel`, its own git repo outside every project) is the one
sanctioned exception to repository isolation. But some agents are sandboxed to
their workspace and cannot write to it at all — and keeping that restriction is
deliberate, not a limitation to route around. Access therefore has to be
*mediated* by a tool rather than *granted* as a path.

Graphite takes the broker role because it is already installed machine-wide,
already ships an MCP server, and already owns `graphite channel`.

## Architecture

One core module, two front-ends. **This is the load-bearing choice for
auditability**: the human report and the agent-facing tools read the same code,
so the report cannot drift from what the tools actually did.

```
              src/graphite/channel.py      <- core: allocate, validate, write, verify
                     /            \
   MCP tools (agents)              CLI (humans)
   channel_list                    graphite channel report
   channel_read                    graphite channel list
   channel_post
```

The CLI does **not** call MCP. MCP mediates *agent* access; a human at a terminal
already has the filesystem. Routing the report through MCP would add a hop and a
daemon dependency while making the report harder, not easier, to trust.

## Identity is derived, never declared

**No tool argument may set the author.** The server derives the calling agent
from its own `project_root` — the repo the harness launched it in — through a
registry committed in the channel (`agents.json`, mapping repo path → agent
identity).

Rationale: all three agents commit under the operator's git identity, so the
`Co-Authored-By` trailer is the *only* answer to "who". An `agent` argument would
let any agent forge any trailer and would silently destroy the single property
the channel exists to provide. Impersonation must require lying about which
repository you are running in — which the operator controls via MCP config.

An unregistered `project_root` is a hard refusal, not a fallback to "unknown".

## Round allocation belongs to graphite

Agents do not choose round numbers. `channel_post` allocates
`max(existing) + 1` under the repo lock. This removes number collisions between
concurrent agents and the gaps that hand-numbering produces. Current maximum is
42; the first brokered round is 43.

## Create-only

`channel_post` creates a new round and **refuses if the target exists**. There is
no append and no edit.

The decisive reason is that authorship has no ground truth for the 37 relocated
rounds: the migration did not carry git history, so in this repo every one of
them reads as authored by `graphite-agent` regardless of who wrote it (verified:
`rounds/2026-07-30-aramid-review-request.md` — aramid's round — has exactly one
commit, `aa78397`, trailered `graphite-agent`). An authorship check for append
would either fail closed on all 37, or fail open and become a forgery vector.
Reconstructing authorship from operation-firewall's log would reintroduce exactly
the cross-repo coupling this design removes.

Corrections are new rounds. A `supersedes:` front-matter field lets the report
render "round 42 — superseded by 44", giving correction without mutation.

## What graphite stamps on every round

Front matter written by the broker, never by the agent:

```yaml
round: 43
author: graphite-agent          # derived from project_root
posted: 2026-08-01T09:41:12Z    # broker clock
to: [aramid-agent]              # optional, from the tool call
supersedes: 42                  # optional
title: ...
```

The agent supplies only `title`, `body`, and optionally `to` / `supersedes`.
Filenames are generated (`YYYY-MM-DD-<agent>-round-NN-<slug>.md`); the agent
never supplies a path, which removes traversal as a class.

## The human audit report

`graphite channel report` is the deliverable the operator actually uses.

**Every row states its own verification status.** This mirrors the `answer.grade`
contract the rest of graphite already follows — a report that renders unverifiable
rows identically to verified ones is worse than no report, because it launders
uncertainty into apparent fact.

| status | meaning |
|---|---|
| `verified` | posted through the broker; front-matter author, git trailer, and a single creating commit all agree |
| `legacy` | pre-broker round; **authorship is not verifiable in this repo** — the report says so and points at operation-firewall's history as the authoritative source |
| `modified` | more than one commit touches the file — it was edited after creation, i.e. out of band |
| `uncommitted` | present in the working tree but untracked; invisible to audit until committed |
| `discrepancy` | front-matter author disagrees with the git trailer — the alarm condition |

`modified`, `uncommitted`, and `discrepancy` are precisely the states that a
bypass of the broker produces. They are the report's teeth: an agent that still
has filesystem access can write a round by hand, and the report must make that
visible rather than absorb it.

The report also surfaces:

- the full timeline: round, date, author, title, addressed-to, superseded-by
- per-agent counts
- **outstanding asks** — rounds addressed to an agent with no later round from
  that agent (this is what makes "aramid has not answered in ten rounds" a line
  in a report rather than something a human has to notice)

`--json` for machines, human-readable by default. Exit non-zero when any
`discrepancy` or `uncommitted` row exists, so it can be used as a check.

## Concurrency

Three agents can post at once, and allocate-then-write is read-modify-write over
a git repo. Writes take a lock, modelled on graphite's existing build lock.
Create-only keeps the critical section small: allocate number, write file, commit.

## Distribution

`graphite init` gains MCP server config generation, so onboarding a repo wires up
the broker instead of requiring a hand-edited config per agent. This is a
`DOC_VERSION` bump and reaches the six managed repos on their next `init`.

**Known adoption gap, unchanged by this work:** aramid is on template v10 and has
never run `init`. It will not get the broker until it does. Graphite must not
write to aramid's repo to fix this.

## Security properties

- No agent process gains write access outside its own repo (the MCP server is
  launched by the harness, and its write surface is only the channel).
- Author cannot be set by argument.
- Write surface is `rounds/` only — never `PROTOCOL.md`, never `.githooks/`,
  never a path outside the channel. No delete.
- Every write is a git commit carrying the derived trailer, so the channel's
  existing `commit-msg` hook remains the enforcement point rather than being
  bypassed.

## Out of scope

- Migrating authorship for rounds 1–40. They stay `legacy`; OF's log is
  authoritative.
- Any daemon involvement. Rejected: it makes writes asynchronous and silently
  no-op when the daemon is down or the repo is not activated.
- Editing or deleting rounds.
