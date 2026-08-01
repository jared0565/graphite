<!-- graphite:managed version=14 -->
# Graphite Development Context

Graphite is the shared local code graph for this project. Codex, Claude Code, Gemini CLI, Antigravity, Visual Studio, and other coding agents should use the same graph instead of rebuilding separate mental maps.

All commands below use `python -m graphite`, which works in every shell and for every agent as long as the Python environment has Graphite installed. A bare `graphite` command is equivalent where the console script is on PATH.

## Required Workflow

Graphite-first is required, not advisory. Before any cross-file exploration, consult the graph first. Manual search (grep, glob, directory walking) is the fallback, not the default: use it for literal text and filename lookups, or after a Graphite answer proved insufficient — and say so when you fall back.

| Question shape | Run first |
| --- | --- |
| Who calls / reads / imports this symbol? | `python -m graphite query "callers <symbol>"` |
| What does this symbol call? | `python -m graphite query "calls <symbol>"` |
| Where is this symbol defined? | `python -m graphite search "<symbol>"` |
| What breaks if this file changes? | `python -m graphite impact <file>` |
| What surrounds this file (callers, tests, neighbors)? | `python -m graphite context <file>` |
| How is the project structured? | `python -m graphite query "stats"` |
| Literal string or filename lookup | grep/glob — Graphite not required |
| Where is the shared agent channel? | `python -m graphite channel` |

Before non-trivial code changes:

1. Run `python -m graphite check .`
2. Run `python -m graphite context <target-file>` before editing important files.
3. Run `python -m graphite impact <target-file>` before changing shared logic, APIs, data flow, auth, persistence, deployment behavior, or other high-risk paths.
4. Use `python -m graphite search "<symbol, path, or concept>"` to locate nodes; use `python -m graphite query "stats"` when project structure is unclear.
5. Discover supported commands, query verbs, and limits with `python -m graphite capabilities --json` — do not guess query verbs. `query` takes structured verbs; `query --natural "<question>"` accepts only the fixed deterministic grammar listed by capabilities (no inference — unmatched questions fall back to ranked search).
6. Graph answers carry an `answer` block: `grade: decision_grade` means this answer's own relations/languages are healthy (an empty result is a trustworthy absence, subject to `caveats`); `advisory` means verify with grep and say so; `inconclusive` (also the legacy `"inconclusive": true`) means unknown, not safe. Check `known limits`/`caveats` before trusting empties.
7. `python -m graphite incidents list` shows recorded failures (build errors, malformed artifacts, inconclusive queries). Check it when a graph answer looks wrong; recurring incidents belong in a governed round.

After edits:

1. Run `python -m graphite build .` when `python -m graphite check .` reports the graph stale. Otherwise this repo's graph refreshes on its own while it is open in a coding agent.
2. Run relevant tests, typechecks, or validation commands.
3. Do not edit `graph-out/` manually.

Graph freshness is never a reason to avoid the graph. Always query it for
relationship questions: every answer carries its own `answer.grade`, so trust
that rather than guessing whether the graph is current. If an answer comes back
`inconclusive` or insufficient, fall back to search and say that you did.

## Repository Isolation

**Your repository is your world.** Do not read, write, or run commands in any other repository — including its `graph-out/`. This holds even when the other repo sits on the same machine, is a dependency of this one, or plainly contains the answer you need.

Cross-repo knowledge travels one way only: as a **recommendation**, through the shared interop channel, addressed to the agent that owns that repository. That agent decides and acts. A defect you find elsewhere is a request, never a patch — and never a read.

- Do not open another repo's source, tests, config, or `graph.json`. Each repo's graph describes that repo and belongs to its agent.
- Do not run any command with another repository as its working directory or root, including read-only ones such as `status`, `doctor`, or a test suite.
- Do report what you observed from your own side, and ask the owning agent to look. Say plainly which parts you could not verify.
- Do act on what another agent tells you about their repository, and attribute it to them.

**If you need a fact from another repo, ask for it.** A claim clearly labelled unverified is safer than a verified one obtained out of bounds — the boundary is the control, and stepping over it to be thorough defeats it.

### The one exception: the shared agent channel

There is one shared **agent channel** on this machine: a directory named `.agent-channel/`, its own git repository, living outside every project and belonging to no repo and no agent. **Every agent may read it and write to it**, whichever repository it is responsible for, and nothing in it is any project's source or data.

Its absolute location is machine-local and deliberately kept out of this file: project files are committed and pushed, so a local directory layout does not belong in them. **Resolve it with `python -m graphite channel`** (`--json` for a machine-readable form, reporting whether it exists and is a git repo). The path goes to stdout and diagnostics to stderr, so `$(python -m graphite channel)` is safe to use directly.

This is the exception that makes isolation workable: isolation without a channel is a wall, not a boundary. Read the channel's `PROTOCOL.md` before writing there.

**Use the broker, not the filesystem.** Graphite exposes the channel as MCP tools, so you never need write access outside your own repository — and if you are sandboxed to your workspace, these are the only way in:

| tool | what it does |
| --- | --- |
| `graphite_channel_inbox` | messages addressed to you that you have not been handed yet — **call this at the start of a session** |
| `graphite_channel_post` | write a new round |
| `graphite_channel_status` | `acknowledged` / `blocked` (give a reason) / `done` / `withdrawn` |
| `graphite_channel_list` | every round with its author and current status |
| `graphite_channel_read` | one round by number |

Four things that will bite you if you assume otherwise:

- **You cannot post as another agent.** There is no author field; your identity comes from the repository the server runs in. `unregistered_project` means ask the operator to register you, not look for a way around it.
- **Rounds are immutable.** Create-only — no edit, no append, no delete. Correct one by posting another with `supersedes`.
- **Graphite assigns round numbers.** Do not pick one.
- **Delivery is recorded by the broker**, as `inbox` hands a message over. You cannot assert or decline it, and anything left `delivered` or `acknowledged` for more than 3 days is reported as stalled.

Every commit there carries your agent's `Co-Authored-By` trailer and states its reason; a `commit-msg` hook rejects commits that name no agent, because all agents commit under one identity and the trailer is what makes the history auditable. The broker satisfies that hook for you. An operator can audit the whole channel at any time with `python -m graphite channel report`, which grades every row by what it can actually vouch for.

### Agent boundary vs. tool boundary

These are different questions and one must not be used to argue about the other.

- **An agent** may act only within its own repository.
- **A tool doing what it was designed to do is not an agent crossing a boundary.** `graphite init` onboarding a repository, or a security gate running against one, is the operator's tooling operating on a repo. That is by design and is not governed by the agent rule.

The distinction belongs to the operator invoking the tool. It is not a licence to reclassify yourself as a tool in order to reach into another repository.

## Canonical Graph Isolation

`scan`, `build`, `report`, `check`, `validate`, `query`, `context`, `impact`,
`watch`, and `daemon` are inference-free canonical operations. They do not read
provider credentials, ignore ambient `GRAPHITE_LLM*` configuration, and reject
legacy non-`none` LLM flags. Model-generated annotations belong only in the
explicit, non-authoritative overlay boundary and must never replace or modify
canonical `graph-out` artifacts.

## Operating Rules

- Treat Graphite as a project map, not as proof of correctness.
- Always read the source files and tests that Graphite identifies before changing behavior.
- Graphite-first: prefer graph commands over manual cross-file search; fall back only when the graph answer is insufficient, and say so.
- If `python -m graphite check .` reports stale output, rebuild before relying on context or impact data.
- Canonical Graphite operations run locally and never use LLM or network inference.
- For TypeScript resolver issues, use `python -m graphite --typescript-resolver disabled build .` only as a fallback.
- Stay inside this repository: no reads, writes, or commands in any other repo or its graph. Findings about another repo go to its agent as a recommendation via the shared `.agent-channel/`.
<!-- graphite:managed-end -->
