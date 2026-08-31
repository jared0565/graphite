:name graphite
:description Local-first, zero-LLM knowledge graph extraction and querying with the graphite-code package.
:author user
:version 1.0.0

## /graphite [path]

Build a knowledge graph for the repository at `path` (defaults to the current directory). Runs entirely locally with zero LLM tokens.

Examples:
- `/graphite .`
- `/graphite tools/graphite`

## /graphite report [path]

Same as `/graphite [path]` — scans, builds the graph, clusters, and writes `graph-out/GRAPH_REPORT.md`, `graph.json`, and `graph.html`.

## /graphite check [path]

Check whether `graph-out/.graphite_manifest.json` still matches the working tree. Non-zero exit when files were added, removed, or changed, or when graphite's engine changed since the build (`engine_changed`); `--ignore-engine` reports source drift only.

```bash
graphite check .
graphite check . --json
```

## /graphite validate

Validate `graph-out/graph.json` integrity before relying on the graph in CI, pre-commit checks, or agent workflows.

```bash
graphite validate
graphite validate --json
```

Validation checks node uniqueness, edge endpoints, metadata counts, relative paths, and cluster membership. Successful builds also write `graph-out/.graphite_validation.json`.

## /graphite capabilities, /graphite search

Discover the contract before querying; locate nodes before naming them.

```bash
graphite capabilities --json        # verbs, aliases, limits, natural-language grammar
graphite search "TenantStore"       # ranked node search: symbol, path, or concept
graphite search "auth middleware" --json
```

## /graphite query "<query>"

Query the existing `graph-out/graph.json`. Verbs:
- `depends-on <node>` — direct dependencies of a node.
- `imported-by <node>` — direct consumers of a node.
- `callers <symbol>` — functions that call the symbol (call/reference in-edges).
- `calls <symbol>` — what the symbol calls (call/reference out-edges).
- `path <a> -> <b>` — shortest directed path over any edges.
- `reaches <a> -> <b>` — directed path over call/reference edges only.
- `community-of <node>` — cluster label for a node.
- `stats` — graph statistics: counts, density, nodes by kind, edges by relation, top-degree nodes.

Read the `answer.grade` on every result: `decision_grade` (act on it; an empty result is a trustworthy absence), `advisory` (usable, verify with a text search and say so), `inconclusive` (unknown, not "none"). When a node is not found, the error JSON includes a `candidates` list of close matches — retry with one of those ids. Natural-language questions go through a fixed grammar with `--natural`; `--show-plan` and `--plan-only` expose the canonical plan.

Examples:
- `/graphite query "callers save_tenant"`
- `/graphite query "path article-gen/route.ts -> db.ts"`

## /graphite context <file...>

Print compact agent-ready context for changed files or graph nodes before broad code reads or edits.

```bash
graphite context src/lib/db.ts
graphite context src/lib/db.ts --json
```

Use this during non-trivial development to identify direct dependencies, direct dependents, likely impacted files/tests, nearby community peers, and coupling risk signals.

## /graphite impact <file...>

Suggest reverse dependencies and likely tests for changed files using `graph-out/graph.json`.

```bash
graphite impact src/tenant-store.ts
graphite impact src/tenant-store.ts --json
```

## /graphite init [path]

Onboard a repository for coding agents: writes `GRAPHITE.md` and the selected platform instruction files (`AGENTS.md`, `CLAUDE.md`, `ANTIGRAVITY.md`, Copilot, Cursor, Windsurf), gitignore entries, an `.mcp.json` entry, hooks that keep the graph fresh — all launching graphite under `-P` — then builds and validates. Idempotent; rerun after upgrading graphite.

```bash
graphite init .
graphite init . --platform codex --platform claude
graphite init . --all
graphite init --list-platforms
```

## /graphite bootstrap [path]

Make a project Graphite-ready with the minimal set: updates `.gitignore`, creates or extends `AGENTS.md`, checks daemon visibility, builds the initial graph by default, and validates the graph.

```bash
graphite bootstrap .
graphite bootstrap . --no-build
```

## /graphite doctor [path]

Readiness and integration checks. Fast mode is read-only; `--deep` exercises the pipeline in a private temporary workspace and never writes to the repository.

```bash
graphite doctor .
graphite doctor . --deep
graphite doctor . --json
```

## TypeScript compiler-backed resolution

Graphite defaults to `GRAPHITE_TYPESCRIPT_RESOLVER=auto`. For TypeScript projects, it uses the local TypeScript compiler API when available to resolve aliases, barrels, re-exports, dynamic imports, and type-only import confidence labels.

When compiler resolution is available, Graphite also adds conservative file-level symbol edges:
- `references` for runtime symbol usage across files.
- `type_references` for type/interface usage across files.

These edges improve impact analysis without introducing model-specific behavior or sending source code to an LLM. It fails soft: if Node or TypeScript is unavailable, Graphite falls back to deterministic heuristic resolution.

```bash
graphite --typescript-resolver auto build .
graphite --typescript-resolver disabled build .
graphite --typescript-resolver-timeout 5 build .
graphite --no-typescript-symbol-references build .
```

Set `GRAPHITE_TYPESCRIPT_SYMBOL_REFERENCES=false` to disable compiler-backed symbol/type reference edges globally for a run.

## /graphite watch [path]

Keep `graph-out` current during active development with a lightweight local polling watcher.

```bash
graphite watch . --impact
```

Watcher behavior:
- Builds once on startup unless `--no-initial-build` is set.
- Debounces save bursts before rebuilding.
- Uses file content hashes to avoid unnecessary rebuilds.
- With `--impact`, prints impacted files and likely tests before rebuilding.
- Zero-LLM; legacy provider flags are rejected.

## /graphite daemon [base]

Keep all discovered projects under a base folder current with a bounded local supervisor. The default base is `GRAPHITE_PROJECTS_ROOT` when set, else the current directory.

```bash
graphite daemon ~/Projects --once
graphite daemon ~/Projects
graphite daemon-status ~/Projects
```

Daemon behavior:
- Discovers projects by markers like `.git`, `package.json`, `pyproject.toml`, `wrangler.toml`, `go.mod`, and `Cargo.toml`; a `.graphite-ignore` file excludes a subtree.
- Skips heavy/tool folders like `node_modules`, `.git`, `graph-out`, `.cache`, `dist`, `build`, and `_tools`.
- Writes status to `<base>/.graphite-daemon/status.json` and logs to `<base>/.graphite-daemon/graphite-daemon.log`.
- Bounds work with `--max-projects`, `--max-depth`, `--max-files-per-project`, `--max-builds-per-cycle`, and `--build-timeout`.
- Zero-LLM; child builds run with a provider-scrubbed environment.

Supervised startup, one command per platform (every launcher runs the interpreter with `-P`):

```bash
# Windows: current-user scheduled task, or the Startup-folder fallback when Task Scheduler is blocked
graphite daemon-install-windows C:\Projects --start-now
graphite daemon-task-status
graphite daemon-uninstall-windows
graphite daemon-install-startup-windows C:\Projects
graphite daemon-startup-status C:\Projects
graphite daemon-uninstall-startup-windows C:\Projects

# Linux: systemd user unit          # macOS: launchd agent
graphite daemon-install-linux ~/Projects
graphite daemon-install-macos ~/Projects
graphite daemon-service-status
graphite daemon-uninstall-linux
graphite daemon-uninstall-macos
```

After upgrading graphite, restart the daemon (stop, install, start): a running daemon keeps the code it loaded at start.

## /graphite daemon-health [base]

Run operational health checks for the local multi-project daemon.

```bash
graphite daemon-health ~/Projects
graphite daemon-health ~/Projects --json
graphite daemon-health ~/Projects --fail-on-error
```

Use this to check status freshness, process presence, startup launcher installation, failing projects, pending initial builds, and projects not built recently.

## /graphite audit-replacement [path]

Audit whether Graphite is ready to replace legacy graph tooling in a project.

```bash
graphite audit-replacement .
graphite audit-replacement . --json
graphite audit-replacement . --fail-on-blocker
```

Use this before removing a legacy graph tool's files, scripts, ignore entries, or docs. The audit reports Graphite readiness, graph freshness/validity, daemon visibility/health, physical legacy remnants, and legacy text references. It does not delete anything.

## Claude Code MCP tools

When the Graphite MCP server is configured, Claude can query the graph directly:
- `graphite_query` — run structured queries.
- `graphite_community` — describe a node's community/cluster.
- `graphite_summary` — high-level stats, god nodes, entry points.
- `graphite_refresh` — rebuild and reload `graph-out/graph.json`.

`graphite init` writes the project-local `.mcp.json` entry. To configure a client by hand, install the extra (`python -m pip install --user "graphite-code[mcp]"`, after the package-validation policy in the README) and launch with `-P`, or use the `graphite-mcp` console script:

```json
{
  "mcpServers": {
    "graphite": {
      "command": "python",
      "args": ["-P", "-m", "graphite.mcp"],
      "env": {},
      "cwd": "/path/to/YourProject"
    }
  }
}
```

## Implementation

This skill calls the `graphite-code` package (import name `graphite`), installed for the interpreter the shell resolves as `python`:

```bash
python -m pip install --user graphite-code
graphite --version
python -P -m graphite -v build .
```

Prefer the `graphite` console script or `python -P -m graphite`; the `-P` stops a `graphite.py` at a repository root from shadowing the installed package.

Model enrichment is a separate, explicit overlay and is off unless the user opts in. Graphite is model agnostic:

```bash
# Native Ollama, local only
GRAPHITE_LLM=local GRAPHITE_LLM_PROVIDER=ollama GRAPHITE_LLM_MODEL=qwen2.5-coder graphite report .

# Generic OpenAI-compatible endpoint
GRAPHITE_LLM=cloud GRAPHITE_LLM_PROVIDER=openai-compatible GRAPHITE_LLM_BASE_URL=https://example.com/v1 GRAPHITE_LLM_MODEL=my-model GRAPHITE_LLM_API_KEY=... graphite report .
```

Supported adapters are `ollama` and `openai-compatible`, with aliases for `openai`, `openrouter`, `groq`, `lmstudio`, and `vllm`. LLM enrichment sends bounded graph metadata and summaries, not source code by default, and never changes the canonical graph.

## Safety

- No absolute paths or system metadata leak into artifacts.
- No external API calls in default mode.
- Generated output goes to `graph-out/`, which is gitignored.
