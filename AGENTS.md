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
