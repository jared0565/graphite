# Toggleable time/token savings display — design (Spec B)

**Date:** 2026-07-24
**Branch:** `feat/graphite-first` (worktree; spec B of the two-spec graphite-first program, builds on spec A's installer)
**Status:** design — awaiting operator review
**Spec:** B — show estimated time/token savings versus working without graphite every time a task completes, toggleable per repo

## Context

Operator request: "show the savings in time and token versus without graphite every time a
task is completed, make it toggleable (savings display on/off)". Operator decisions
(2026-07-24 brainstorm):

1. Display = **turn-end summary** (Claude Code Stop hook, wired by init) **+
   `graphite savings` CLI** for any agent on demand.
2. Estimator = **result-scaled heuristic**: counterfactual proportional to what each answer
   actually contained, every figure labeled *estimated*, formula printed with the report.
3. Toggle = **per-repo, default ON**, stored machine-local (viewing preference, not repo
   policy — not committed).

"Savings versus without graphite" is a counterfactual: the exploration that didn't happen
cannot be measured. This design keeps the claim honest by scaling the estimate from real
answer contents, flooring at zero, labeling everything as an estimate, and printing the
model's constants alongside the numbers.

## Non-goals

- No measurement claims — estimates only, always labeled.
- No data leaves the machine; the ledger is a local, gitignored, inference-free side file.
  Canonical Graph Isolation is unaffected; `graph-out/` is never touched.
- No turn-end display for non-Claude agents (no hook system); they use the CLI.
- No cross-repo aggregation and no published JSON schema for the savings report in v1
  (informational output; a schema can follow if agents start consuming it).
- No LLM anywhere: `savings` and `agent-hook stop` join the inference-free canonical set
  (CLI gate rejects `--llm`, exit 2).

## Change 1 — Usage ledger

New module `src/graphite/usage_ledger.py`. Each canonical read command — `query`
(structured and `--natural`), `search`, `context`, `impact` — appends one JSONL record on
success to `.graphite/local/usage.jsonl` in the target repo:

- `ts` (ISO timestamp), `cmd` (command class), `wall_ms` (measured),
- result metrics: node/file count in the answer, output bytes,
- counterfactual inputs: the answer's file paths with on-disk byte sizes (`os.stat` at
  answer time, per-file try/except, capped at 100 files per record).

Rules:

- **Never fatal:** every ledger write is wrapped; a ledger failure is invisible to the
  underlying command.
- **Bounded:** rotate at 5 MB to `usage.jsonl.1` (single generation, overwritten).
- **Machine-local:** `init` ensures `.graphite/local/` is gitignored (existing gitignore
  machinery; the router precedent already keeps `.graphite/` state machine-local).
- Corrupt lines are skipped by all readers, never fatal.

## Change 2 — Estimator (result-scaled heuristic)

New module `src/graphite/savings.py` owning the counterfactual model and its constants (one
documented dataclass; starting values below, tunable later, always printed by the report):
`GREP_TOKENS = 1000`, `GREP_SECONDS = 20`, `READ_SECONDS = 10`, `FILE_TOKEN_CAP = 2000`.

- Manual-equivalent cost of an answer covering K files:
  `grep_rounds = 1 + K // 10`;
  `manual_tokens = grep_rounds * GREP_TOKENS + Σ min(file_bytes / 4, FILE_TOKEN_CAP)`;
  `manual_seconds = grep_rounds * GREP_SECONDS + K * READ_SECONDS`.
- Graphite's actual cost: `output_bytes / 4` tokens plus measured wall seconds.
- `savings = max(0, manual − graphite)` — empty answers show zero, big answers show
  proportionally more.

Every displayed figure is labeled *estimated*; `graphite savings report` prints the formula
and the constants in a methodology footer. Honest and auditable, never oversold.

## Change 3 — Display surfaces

### Turn-end summary (Claude Code)

Spec A's settings installer additionally wires a Stop hook →
`python -m graphite agent-hook stop`. Behavior:

- Reads ledger entries since a per-session cursor (`.graphite/local/stop-cursor.json`,
  keyed by the `session_id` from the hook payload; stores byte offset + rotation
  generation + session running totals; pruned to the 20 most recent sessions).
- When the toggle is ON and ≥ 1 graphite call happened that turn, emits one compact
  user-visible line (hook `systemMessage`):
  `graphite: est. ~12.3k tokens / ~4 min saved this turn (session: ~48k / ~15 min)`.
- No graphite use that turn → completely silent. Toggle OFF → silent. Any error → silent,
  exit 0 (same fail-open invariant as every `agent-hook` event; the hook never blocks a
  turn).

### `graphite savings` CLI (any agent, on demand)

- `report` (default): today's and all-time totals, broken down by command class, with the
  methodology footer; `--json` for machine-readable output.
- `on` / `off` / `status`: per-repo toggle in `.graphite/local/settings.json`
  (`{"savings_display": bool}`), default ON when absent. The toggle silences the turn-end
  line only; `report` always works, and the ledger keeps recording either way so history
  survives toggling.

## Change 4 — Sequencing with spec A

Spec A builds the installer and the `agent-hook` command; spec B extends both (adds the
`stop` event and the Stop-hook wiring entry). Both land on `feat/graphite-first`, merge to
main together after review; then spec A's rollout step runs once — consumer repos get docs
v5 + all three hook wirings in a single re-init, followed by global-hook retirement.

## Error handling

- Ledger writes, cursor writes, and the stop hook are never fatal and never block.
- Missing `.graphite/local/` is created on demand; unwritable → silent skip.
- Rotation and corrupt-line tolerance as in Change 1.

## Testing

- Ledger: append shape, rotation at cap, corrupt-line tolerance, stat-failure tolerance,
  never-raises wrapper.
- Estimator goldens: zero-file answer → zero; K-file scaling; per-file token cap;
  floor-at-zero when graphite cost exceeds manual estimate.
- Toggle semantics: default ON, off/on/status transitions, report unaffected by toggle.
- Stop hook: first turn of a session, no-usage turn (silent), multi-call turn, cursor
  advance across turns, rotation-crossing cursor, toggle-off silence, malformed payload →
  exit 0.
- Installer: Stop-hook entry present after init; re-init refreshes all three graphite
  entries; foreign hooks preserved.
- CLI gate: `savings` rejects `--llm` (exit 2).
