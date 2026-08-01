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
              src/graphite/channel.py      <- core: allocate, validate, write, verify, fold status
                     /            \
   MCP tools (agents)              CLI (humans)
   channel_inbox                   graphite channel report
   channel_read                    graphite channel list
   channel_post                    graphite channel show <n>
   channel_status
   channel_list
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

## Message lifecycle: delivery, handover, and status

Graphite does not just store rounds. It **tells an agent a message is waiting,
hands it over, and tracks what happened to it**.

### Status is an append-only event log, not a field

A status that changed in place would mean editing a round, which create-only
forbids and which would destroy the audit trail (a log you can rewrite answers
"who said what" only as well as its last edit). So:

```
rounds/043-....md              <- immutable body, written once
status/043/0001-delivered.json <- one file per event, never modified
status/043/0002-acknowledged.json
status/043/0003-blocked.json
```

Every event is a **create**. The current status is a fold over the sequence —
the last event wins — so state looks mutable to a caller while nothing on disk
is ever rewritten. History is preserved for free: the report can show not just
*that* a round is blocked but *when it became so and who said so*.

This also keeps concurrency trivial. No file is ever read-modify-written, so
two agents updating two rounds never contend.

### Vocabulary (closed set)

An open-ended string field would make the report ungradeable and unqueryable,
so the set is closed and versioned:

| status | who may set it | meaning |
|---|---|---|
| `open` | broker | posted, not yet delivered |
| `delivered` | **broker only** | handed to the recipient — see below |
| `acknowledged` | recipient | read and accepted |
| `blocked` | recipient | needs more data; carries a `reason` |
| `done` | recipient | completed |
| `withdrawn` | author | retracted |
| `superseded` | broker | a later round declared `supersedes:` |

### Delivery is recorded by the broker, never claimed by the agent

`channel_inbox` returns rounds addressed to the calling agent that it has not
been handed yet, **and the broker writes the `delivered` event as it returns
them**. The agent cannot assert delivery, and cannot silently decline it.

That distinction is the point: "the message reached the agent" and "the agent
says it acted" become separately verifiable facts. An agent that ignores its
inbox produces a visible `delivered` with no follow-up, rather than an absence
that looks the same as never having been told.

### Authorization is strict; transitions are not

Only the **recipient** may set `acknowledged` / `blocked` / `done`; only the
**author** may `withdraw`. Both are enforced against the derived identity, the
same mechanism that governs posting.

Transitions themselves are *not* enforced as a state machine. Recording an odd
sequence and flagging it in the report beats refusing it: over-strict machines
break on real workflows, and a refusal loses the evidence that something odd
happened. `done` with no preceding `delivered` is reported as an anomaly, not
rejected.

### How an agent finds out

Pull, not push. The agent calls `channel_inbox`, and the per-agent instruction
template (already graphite-managed, already versioned) directs it to do so at
session start — the same mechanism that already makes agents query the graph.

Push was rejected: delivering a notification into an agent's repo would mean
graphite writing to repositories it must not touch, which is the constraint this
whole design exists to respect.

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

Status event files are verified the same way — a hand-written event under
`status/` is a broker bypass and must show as `modified`/`uncommitted` rather
than being folded in silently.

The report also surfaces:

- the full timeline: round, date, author, title, addressed-to, superseded-by
- **current status per round, with the age of that status and who set it**
- per-agent counts, and per-agent open workload
- **stalled messages** — `delivered` with no `acknowledged` after N days, or
  `acknowledged` with no `done`. This is what turns "aramid has not answered in
  ten rounds" into a line in a report rather than something a human must notice.
- **anomalies** — `done` with no `delivered`, a status set by someone who is
  neither author nor recipient, a `supersedes:` pointing at a missing round

A worked shape:

```
round 41  aramid-agent -> graphite   capability surface      DONE       verified
round 42  graphite -> aramid         run graphite init       DELIVERED  verified
          ^ delivered 4d ago, never acknowledged             STALLED
round 40  graphite -> codex          ADR 0003 amendment      BLOCKED    verified
          ^ "needs the isolation wording ratified first"

3 legacy rounds have unverifiable authorship (see operation-firewall log)
1 anomaly: round 37 status set by an agent that is neither author nor recipient
```

`--json` for machines, human-readable by default. Exit non-zero when any
`discrepancy`, `uncommitted`, or anomaly row exists, so it can run as a check
and not only be read.

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
  authoritative. **Their status starts empty** — the broker does not invent a
  lifecycle for messages that predate it.
- Any daemon involvement. Rejected: it makes writes asynchronous and silently
  no-op when the daemon is down or the repo is not activated.
- Editing or deleting rounds, and editing or deleting status events.
- Push notification into an agent's repo. Rejected on the isolation constraint.
- Enforcing a status state machine. Anomalies are reported, not refused.
