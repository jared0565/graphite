# graphite Agent Notes

## Automatic Graphite Consult

For any non-trivial code change, run Graphite before broad file reads or edits:

```bash
python -m graphite check .
python -m graphite context <likely-changed-file>
python -m graphite impact <likely-changed-file>
python -m graphite query "stats"
```

Use `python -m graphite context` first when the likely target file is known. Treat its output as a dependency map, not as proof of correctness: still read the relevant source files and tests before editing. If `python -m graphite check .` reports stale output, rebuild before relying on context or impact data.

If the graph is missing or stale, rebuild it from the repository root with:

```bash
python -m graphite -v build .
```

During active multi-file development, keep the graph current with:

```bash
python -m graphite watch . --impact
```

Graphite is a centrally installed Python package (importable from any repository), runs locally by default, and should not use LLM or network calls unless explicitly configured. `python -m graphite` works in every shell; a bare `graphite` command is equivalent where the console script is on PATH.

For TypeScript, Graphite uses the local TypeScript compiler resolver automatically when available. If a project has a broken TypeScript setup, fall back with `python -m graphite --typescript-resolver disabled build .`.

<!-- graphite:managed version=14 -->
## Shared Graphite Instructions

Graphite-first is required in this repo. Follow `GRAPHITE.md` before making non-trivial code changes: for cross-file questions (who-calls, where-defined, impact, data flow, structure) run the Graphite commands first; grep/glob are for literal text and filename lookups only. Fall back to manual search only after a Graphite answer proved insufficient, and say so. Use the existing `graph-out/graph.json` as the shared project graph, and do not edit `graph-out/` manually.

**Stay inside this repository.** Do not read, write, or run commands in any other repo, including its graph. Findings about another repo go to its agent as a recommendation through the shared `.agent-channel/` (see its `PROTOCOL.md`); that agent decides and acts. A tool doing its designed job is a separate question from an agent's boundary. See `GRAPHITE.md` section "Repository Isolation".
<!-- graphite:managed-end -->

<!-- aramid:begin -- managed by `aramid init`; hand-edits inside the fence are overwritten -->
## Aramid (security & quality gate)

This repo is gated by aramid. Read `ARAMID.md` before your first commit.

- Before committing: run `aramid check --staged`. Read findings with
  `aramid ledger filter --status open`.
- NEVER pass `--no-verify` (or `-n`) to `git commit`, or `--no-verify` to
  `git push` -- it disables secret scanning along with everything else.
  Armed repos reject the call outright.
- To suppress a WARN finding, use `aramid override <id> --reason "..."`
  (ledger-logged); never edit findings away by hand.
<!-- aramid:end -->
