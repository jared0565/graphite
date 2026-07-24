# Graphite-first hardening of init/reinit — design (Spec A)

**Date:** 2026-07-24
**Branch:** `feat/graphite-first` (worktree; spec A of the two-spec graphite-first program, followed by the savings-display spec B)
**Status:** design — awaiting operator review
**Spec:** A — make graphite-first strictly observed whenever appropriate, and make re-init propagate that enforcement to consumer repos

## Context

Today "graphite-first" is soft. `graphite init` writes documentation only: the GRAPHITE.md
managed block (DOC_VERSION 4 "Required Workflow") plus per-platform pointer files. The only
active enforcement anywhere is a **user-global** Claude Code PreToolUse hook on Grep|Glob
(`~/.claude/hooks/graphite-reminder.py`, wired in `~/.claude/settings.json`) that emits a
non-blocking reminder when the cwd has `graph-out/graph.json`. It lives on this machine, not
in any repo; `init` knows nothing about it; every other agent (Codex, Gemini, Copilot,
Cursor, Windsurf) gets docs only.

Operator decisions (2026-07-24 brainstorm):

1. Two specs, A then B, one program; single consumer-repo rollout after B lands.
2. Enforcement strength: **deny-capable, remind default** — never block on guesswork.
3. Hook logic lives **in the package** (a CLI subcommand); wiring goes in the target repo's
   **committed `.claude/settings.json`**.
4. The global `graphite-reminder.py` wiring is **retired** after rollout.

## Non-goals

- No hook enforcement for non-Claude agents — Codex/Gemini/Copilot/Cursor/Windsurf have no
  hook system; they get the hardened instruction files only.
- No strict-deny for Glob. Glob patterns are filename lookups, which the graphite-first
  contract explicitly leaves to the filesystem tools. Glob gets remind-mode context only.
- No change to canonical `graph-out/` artifacts, no network, no inference. `agent-hook`
  joins the inference-free canonical command set (the CLI gate rejects `--llm` on it, exit 2).
- `init` never edits user-global `~/.claude/settings.json`. Retiring the global hook is a
  separate operator-approved rollout step.
- No hard blocking that strands the agent: every deny carries the exact replacement command.

## Change 1 — Template hardening (DOC_VERSION 4 → 5)

`GRAPHITE_DOC`'s "Required Workflow" becomes an explicit graphite-first contract:

- A decision table mapping question shape → command: who-calls / who-reads / where-defined /
  data-flow / blast-radius / structure-overview → `query` / `context` / `impact` / `search`
  **first**; literal text and filename lookups → grep/glob are legitimate.
- MUST language: cross-file exploration starts with graphite. Falling back to manual search
  is allowed only after graphite's answer proves insufficient, and the agent must say so.
- `SHARED_POINTER` (CLAUDE.md / AGENTS.md / GEMINI.md / ANTIGRAVITY.md /
  copilot-instructions.md / .windsurfrules) and `CURSOR_POINTER` get matching MUST language:
  graphite-first is required, GRAPHITE.md holds the contract.

`DOC_VERSION` bumps to 5. The existing managed-block refresh mechanism then propagates the
hardened text to every consumer repo on its next `init` — this is the docs half of
"enforced when we reinit", and it covers all agents including the hook-less ones.
`test_template_change_requires_doc_version_bump` keeps pinning the pairing.

## Change 2 — In-package hook logic: `graphite agent-hook`

New module `src/graphite/agent_hooks.py` + CLI subcommand
`python -m graphite agent-hook <event>`; reads the Claude Code hook JSON payload on stdin,
writes hook JSON on stdout. Spec A implements two events (spec B adds `stop`):

### `agent-hook session-start`

Injects, as `additionalContext`: the graphite-first contract summary and the repo's current
graph freshness (internal equivalent of `graphite check .`, capped at 2 s — over budget →
contract summary only). If the graph is stale, the context says to rebuild before relying
on it. Every session starts knowing the
rule and whether the graph is trustworthy.

### `agent-hook pre-tool-use [--mode remind|strict]`

Fires on Grep|Glob.

- **Remind (default):** non-blocking `additionalContext` reminder — the current global
  hook's behavior, now versioned in the package and hardened to match the new template
  language.
- **Strict:** may return `permissionDecision: deny`, but only on a **high-confidence,
  graph-backed classifier**. Deny requires ALL of:
  1. the tool is Grep (never Glob);
  2. the repo's graph exists and loads within budget (32 MiB byte cap on `graph.json`;
     over-budget → remind — the hook must stay sub-second typical, and fail-open beats
     slow);
  3. an identifier-like token extracted from the Grep pattern (alphanumeric/underscore runs,
     ≥ 3 chars, regex metacharacters stripped; at most 5 tokens checked) resolves to a graph
     node by the exact-match tiers of the existing search machinery;
  4. the Grep is cross-file (no `path` argument targeting a single file).

  The deny reason names the matched node and hands back the replacement commands
  (`query "callers <node>"` / `context <file>` / `search "<token>"`). Anything not provably
  answerable by the graph proceeds untouched — strict means "strict whenever appropriate",
  not "strict by guesswork".

**Fail-open invariant:** malformed stdin, missing graph, oversized graph, any exception →
exit 0, no denial (at most no output). A hook bug can never break a tool call or a session.

Mode is an explicit argument **in the committed wiring**, so strictness is visible repo
policy in git, not hidden machine state.

## Change 3 — Settings installer in init

`init` gains an idempotent step that merges graphite's hook entries into the target repo's
`.claude/settings.json`:

- PreToolUse, matcher `Grep|Glob` → `python -m graphite agent-hook pre-tool-use --mode <m>`
- SessionStart → `python -m graphite agent-hook session-start`

Rules:

- **Non-destructive merge.** Existing keys, other hooks, and unrelated settings are
  preserved. Graphite-owned entries are recognized by the `python -m graphite agent-hook`
  command prefix and replaced wholesale on re-init — the hook-wiring analog of the
  DOC_VERSION refresh, and the second half of "enforced when we reinit".
- **Malformed JSON → no-op** plus a reported action (`"malformed settings"`); init never
  destroys a settings file it cannot parse.
- Mode selection: `init --strict` writes strict; `init --remind` writes remind; **no flag
  preserves the repo's existing mode** (routine re-init never downgrades a strict repo),
  defaulting to remind for first-time wiring. `init --no-agent-hooks` skips the step.
- The gitignore allowlist mechanism (`ensure_gitignore_allowlist`) learns
  `.claude/settings.json` so default-deny repos commit the wiring.
- `InitResult` gains an `agent_hooks` report field (path, changed, action, mode).

## Change 4 — Rollout (operator-gated, after spec B lands)

1. Re-init the consumer repos (demo-store2, pawscout-worker, Medication Reminder, aramid):
   managed docs refresh to v5, hook wiring installed. One touch per repo.
2. Retire the global hook: remove the Grep|Glob → `graphite-reminder.py` wiring from
   `~/.claude/settings.json` (operator approves the edit; the script file may stay on disk).
   After per-repo wiring exists, the global hook is pure duplication — it only ever fired in
   repos with a built graph.

## Error handling

- Fail-open is the invariant for everything `agent-hook` touches (see Change 2).
- The installer's failure modes are "did nothing + reported why", never partial writes
  (`atomic_write_text`, same as the doc writers).
- Consumer repos without `.claude/` get the directory created; repos with foreign hooks keep
  them untouched.

## Testing

- Template↔DOC_VERSION pairing test extends to v5.
- Classifier goldens: symbol-in-graph + cross-file → deny payload with replacement commands;
  literal string / filename pattern / single-file path / missing graph / oversized graph /
  Glob → no deny (remind or silence per mode); malformed stdin → exit 0 silent.
- Settings merge: fresh repo, idempotent re-run, foreign-hooks preservation, graphite-entry
  replacement on re-init, mode preservation without flags, malformed-JSON refusal.
- Init e2e: fresh repo → docs v5 + wiring present + allowlist entries; v4 repo re-init →
  docs refresh to v5 + wiring added.
- CLI gate: `agent-hook` rejects `--llm` (exit 2), like every canonical command.
