# Activation-scoped supervision

**Date:** 2026-07-28
**Status:** approved, pending implementation plan
**Supersedes:** discovery-based daemon supervision

## Problem

The daemon supervises every repository it can discover under a base folder. On this machine that is 32 projects, and it rebuilds them regardless of whether anyone is working on them. The operator's mandate:

> Graphite must only focus or work only on the repo that is being edited or loaded for editing. If the repo is not loaded for editing, just leave them alone. The proper behaviour now is to rebuild the graph of the repo when it is opened for editing.

The forecast behind it: at 100+ repositories, machine-wide supervision degrades machine resources and agent attention alike, and nobody works on ten repositories at once.

The daemon process itself is not the problem and may keep running machine-wide. What must change is **which repositories it touches**.

## Core idea

Replace the question the daemon asks each cycle.

| | today | after |
|---|---|---|
| supervised set | "what repos exist under the base folder?" | "what repos are open right now?" |
| discovery | filesystem walk, marker files, prune rules | read one directory of activation markers |
| idle repo | snapshotted every cycle, rebuilt on change | untouched |

Supervision follows **activation markers** rather than filesystem discovery.

## Consequences beyond the mandate

These fall out of the design rather than being separately engineered, and each should be verified during implementation rather than assumed.

- **#6 (nested repos unsupervised) becomes obsolete.** That warning exists because discovery deliberately prunes nested repositories. Activation does not care about nesting: open `BytesAI Learning\app` and it is supervised; close it and it is not. The warning answers a question this design deletes.
- **#18 (daemon never rebuilds on engine change) largely dissolves.** "Rebuild when opened" is precisely the missing trigger. Activation consults `check_graph_freshness`, which already reports `engine_changed`, so an engine upgrade propagates the next time each repo is opened — no restart, no 32-project rebuild storm.
- **#21's propagation problem shrinks.** A `cache_version` bump reaches a repo when someone opens it, which is also when it matters. #21's underlying cache-key defect is *not* fixed by this design and remains open on its own merits.
- **Repos outside the base folder are covered.** Supervision follows markers, not a base path, so `f:\Claude\Bytes Web\bytes-website` participates on equal terms.

## Components

### 1. Activation registry

One small file per active repository:

```
<state>/active/<sha256(normalized_repo_path)[:16]>.json
```

```json
{
  "root": "F:\\Projects\\aramid",
  "agent": "claude",
  "first_seen": "2026-07-28T05:10:00+00:00",
  "last_seen":  "2026-07-28T05:41:12+00:00"
}
```

**State directory is user-scoped**, not under the daemon base: `%LOCALAPPDATA%\graphite` on Windows, `~/.local/state/graphite` on POSIX, overridable via `GRAPHITE_STATE_DIR`. Tying it to `F:\Projects` would reintroduce the base-path coupling this design removes.

**Central rather than per-repo.** A marker inside each repo (`<repo>/.graphite/active`) would force the daemon to scan repositories to discover which are active — reintroducing the cost being removed. One directory read gives the whole active set.

Writes must be atomic (temp file + replace) and tolerate concurrent writers: several agents may open the same repo simultaneously.

### 2. Activation writers

Hung off the `PlatformSpec` registry that `graphite init` already maintains (`init.py:123-128`), which already covers codex, claude, gemini, antigravity, visual-studio/copilot, and cursor.

| platform | mechanism | status |
|---|---|---|
| Claude Code | `SessionStart` hook writes marker; `Stop` hook refreshes it | hook already installed (`agent_hooks.py:59`), needs the write |
| VS Code, Cursor, Antigravity | `.vscode/tasks.json` task with `"runOptions": {"runOn": "folderOpen"}` | new; all three are VS Code-derived |
| any agent | **universal backstop:** an interactive `graphite` CLI invocation inside a repo creates or refreshes that repo's marker | new; covers Codex and Gemini with no hook support required |

**The backstop must exclude daemon-spawned builds.** The daemon runs `-m graphite build` as a child process inside the repo it is supervising. If that invocation refreshed the marker, every activated repo would renew its own activation forever and never expire — supervision would ratchet up and never release, which is the failure this design exists to prevent. Daemon children must be marked as such (explicit flag or environment variable) and skip the write. This is the single most important detail to get right, and test 8 below exists for it.

The backstop is what makes this design robust to agents graphite cannot hook: if an agent uses graphite at all, graphite learns the repo is active. Coverage degrades gracefully rather than failing.

`.vscode/tasks.json` must be **merged, never overwritten** — the precedent is `graphite init --adopt` (#13), which appends rather than destroying hand-written content.

### 3. Daemon

`run_daemon`'s supervised set becomes the live-marker set:

- Each discovery cycle reads `<state>/active/` and supervises markers whose `last_seen` is within the TTL. Expired markers are **deleted from disk**, not merely ignored, so the registry cannot grow without bound.
- A repo that loses its marker is removed from `states`, freeing its snapshot.
- A repo with no marker is **never snapshotted and never built**.
- Per-cycle build budgets (`max_builds_per_cycle`, `build_timeout`) are unchanged; with far fewer supervised repos they bind less often.

### 4. Rebuild on open

The hook writes the marker and returns immediately. The **daemon** notices the newly-active repo on its next cycle and builds it if stale.

Builds take seconds to minutes; nothing blocking belongs in a session-start path. The existing `_FRESHNESS_BUDGET_SECONDS` thread-with-timeout in `agent_hooks.py` exists for exactly this reason and should be preserved.

Staleness for this purpose is whatever `check_graph_freshness` reports: missing graph, file drift, **or** `engine_changed`.

### 5. Doctrine

**Governing principle: freshness is never a precondition for querying the graph.**

An agent must never decide *in advance* that the graph is not worth consulting. It queries, reads the grade the answer carries, and falls back only after an insufficient answer — saying so when it does. This is already what `PRE_TOOL_REMINDER` instructs (`agent_hooks.py:82`) and what the answer contract exists to support: `answer.grade` reports `decision_grade` / `advisory` / `inconclusive` per question, so the graph tells the agent when not to trust it. Any doctrine sentence that lets an agent skip on freshness grounds duplicates that judgement badly and unconditionally.

**The current text violates this.** `init.py:57`, shipped verbatim into every consumer (e.g. `aramid/GRAPHITE.md:34`):

> Run `python -m graphite build .` (skip if a Graphite daemon/watcher keeps this repo fresh; verify with `python -m graphite check .`)

It places "skip" beside "a daemon keeps this repo fresh." Scoped to the build step, it reads as a general licence, and it contradicts `PRE_TOOL_REMINDER` directly. Operator-reported: agents have declined to use the graph, citing that graphite is supervising the repo.

**This design must not replace one excuse with a better one.** An earlier draft of this spec proposed "a just-opened repo may still be building" — which is a *more* defensible reason to skip, and would have made the problem worse. Rejected.

Replacement wording, which states the refresh mechanism without offering an out:

> This repo's graph refreshes when you open it in a coding agent. Always query the graph for relationship questions — it grades its own answers, so trust `answer.grade` rather than guessing whether the graph is current. If an answer comes back `inconclusive` or insufficient, then fall back to search and say that you did. Run `python -m graphite build .` yourself only when `graphite check .` reports the graph stale.

Two rules follow for all shipped doctrine:
- The words "skip" and "daemon"/"supervising"/"fresh" never appear in the same instruction.
- Rebuilding is conditioned on an *observation* (`check` says stale), never on an assumption about who is maintaining the graph.

`DOC_VERSION` 9 → 10; re-init the five managed consumers.

This is a live defect in shipped consumer docs, independent of activation scoping, and is tracked separately so it can be fixed without waiting for this design.

## Error handling

- **Unwritable state dir** — activation degrades to a no-op; the agent still gets its session context. Never fail an agent session because supervision could not be registered.
- **Corrupt or partial marker** — treated as absent and removed. A malformed marker must not take down a daemon cycle; this is the seam #3 addressed for build failures, and the same discipline applies.
- **Marker for a path that no longer exists** — dropped on read.
- **Clock skew / future `last_seen`** — clamp rather than trusting, so a bad timestamp cannot pin a repo active forever.

## Testing

Behavioural, driving the real loop rather than asserting on mocks:

1. Marker round-trip: write, read, refresh, TTL expiry, atomic concurrent writes.
2. **Daemon supervises only marked repos** — given two repos and one marker, assert the unmarked repo is never built *and never snapshotted*. The negative half is the point: it is what the mandate actually asks for.
3. Losing a marker removes the repo from `states`.
4. Activation on an engine-stale graph triggers a rebuild (the #18 case).
5. SessionStart hook writes a marker for its repo.
6. `.vscode/tasks.json` merge preserves pre-existing tasks.
7. Corrupt marker does not abort the cycle.
8. **A daemon-spawned build does not refresh the marker it is building under.** Drive the real loop: activate a repo, let the daemon build it, advance past the TTL with no further agent activity, and assert the repo falls out of supervision. Without this, activation is permanent and the mandate is silently unmet — and because the ratchet only shows up over an hour of wall-clock, it would not surface in ordinary use.

Each test must be watched failing first. Per the standing rule, asserting that an artifact *exists* is weaker than asserting the behaviour it implies — for the daemon tests, assert on what was **not** touched.

## Migration

1. Ship activation writers and daemon changes together; a daemon reading markers with nothing writing them supervises nothing.
2. `graphite init` gains the activation integration; re-init the five managed consumers to install it.
3. Update doctrine and `DOC_VERSION`, verify with a marker survey.
4. Restart the daemon once so it runs the marker-reading code. This is the last full-supervision restart.

## Risks

- **Silent under-supervision.** The failure mode inverts: instead of building too much, graphite may build nothing because activation never fired. `daemon-health` must make the active set visible, and a repo whose graph is stale while its marker is live is the signal worth surfacing.
- **VS Code fork behaviour is assumed, not verified.** Cursor and Antigravity are believed to honour `runOn: folderOpen`; confirm per platform before claiming support in docs.
- **`Stop` fires per turn in Claude Code**, which makes it a good heartbeat but means session *end* is never explicitly signalled. TTL is therefore the only expiry path, and its length is the difference between a repo lingering supervised and being dropped mid-work.

## Decisions taken

- TTL: **60 minutes** without a heartbeat.
- State dir: **`%LOCALAPPDATA%\graphite`**, `GRAPHITE_STATE_DIR` override.
- `daemon-health`'s nested-unsupervised-repo warning is **removed, not ported**. Failing-project and incident reporting stay, scoped to active repos.
- The daemon keeps running machine-wide. Only its supervised set changes.

## Out of scope

- #21's cache-key defect (engine identity absent from the extraction cache key). Independent, still open.
- #19 class-field arrow binding.
- The unbound long tail from #20 (Promise executor params, node builtins via member calls, `alert`/`confirm`/`prompt`/`requestAnimationFrame`).
