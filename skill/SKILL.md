:name graphite
:description Local-first, zero-LLM knowledge graph extraction using the graphite package.
:author user
:version 0.1.0

## /graphite [path]

Build a knowledge graph for the repository at `path` (defaults to current directory). Runs entirely locally with zero LLM tokens by default.

Examples:
- `/graphite .`
- `/graphite tools/graphite`

## /graphite report [path]

Same as `/graphite [path]` — scans, builds graph, clusters, and writes `graph-out/GRAPH_REPORT.md`, `graph.json`, and `graph.html`.


## /graphite check [path]

Check whether `graph-out/.graphite_manifest.json` still matches the current working tree. Returns a non-zero exit code when files were added, removed, or changed.

```bash
graphite check .
graphite check . --json
```

## /graphite impact <file...>

Suggest reverse dependencies and likely tests for changed files using `graph-out/graph.json`.

```bash
graphite impact src/tenant-store.ts
graphite impact src/tenant-store.ts --json
```



## /graphite validate

Validate `graph-out/graph.json` integrity before relying on the graph in CI, pre-commit checks, or agent workflows.

```bash
graphite validate
graphite validate --json
```

Validation checks node uniqueness, edge endpoints, metadata counts, relative paths, and cluster membership. Successful builds also write `graph-out/.graphite_validation.json`.

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
- Uses zero-LLM mode by default unless the user explicitly enables LLM enrichment.

## /graphite daemon [base]

Keep all discovered projects under a base folder current with a bounded local supervisor. Default base is `F:\Projects` when it exists.

```bash
graphite daemon F:\Projects --once
graphite daemon F:\Projects
graphite daemon-status F:\Projects
```

Daemon behavior:
- Discovers projects by markers like `.git`, `package.json`, `pyproject.toml`, `wrangler.toml`, `go.mod`, and `Cargo.toml`.
- Skips heavy/tool folders like `node_modules`, `.git`, `graph-out`, `.cache`, `dist`, `build`, and `_tools`.
- Writes status to `<base>/.graphite-daemon/status.json` and logs to `<base>/.graphite-daemon/graphite-daemon.log`.
- Bounds work with `--max-projects`, `--max-depth`, `--max-files-per-project`, `--max-builds-per-cycle`, and `--build-timeout`.
- Uses zero-LLM mode by default unless the user explicitly enables LLM enrichment.
Windows startup integration:
```bash
graphite daemon-install-windows F:\Projects --start-now
graphite daemon-task-status
graphite daemon-uninstall-windows

# Non-admin fallback when Task Scheduler is blocked
graphite daemon-install-startup-windows F:\Projects
graphite daemon-startup-status F:\Projects
graphite daemon-uninstall-startup-windows F:\Projects
```
## /graphite audit-replacement [path]

Audit whether Graphite is ready to replace legacy Graphify usage in a project.

```bash
graphite audit-replacement .
graphite audit-replacement . --json
graphite audit-replacement . --fail-on-blocker
```

Use this before removing Graphify files, scripts, ignore entries, or docs. The audit reports Graphite readiness, graph freshness/validity, daemon visibility/health, physical Graphify remnants, and legacy text references. It does not delete anything.

## /graphite daemon-health [base]

Run operational health checks for the local multi-project daemon.

```bash
graphite daemon-health F:\Projects
graphite daemon-health F:\Projects --json
graphite daemon-health F:\Projects --fail-on-error
```

Use this to check status freshness, process presence, startup launcher installation, failing projects, pending initial builds, and projects not built recently.

## /graphite bootstrap [path]

Make a project Graphite-ready. Bootstrap updates `.gitignore`, creates or extends `AGENTS.md`, checks daemon visibility, builds the initial graph by default, and validates the graph.

```bash
graphite bootstrap .
graphite bootstrap F:\Projects\MyApp
graphite bootstrap . --no-build
```

## /graphite context <file...>

Print compact agent-ready context for changed files or graph nodes before broad code reads or edits.

```bash
graphite context src/lib/db.ts
graphite context src/lib/db.ts --json
```

Use this during non-trivial development to identify direct dependencies, direct dependents, likely impacted files/tests, nearby community peers, and coupling risk signals.

## /graphite query "<query>"

Query the existing `graph-out/graph.json`. Supported queries:
- `depends-on <node>` — direct dependencies of a node.
- `imported-by <node>` — direct consumers of a node.
- `callers <symbol>` — functions that call the symbol (call/reference in-edges).
- `calls <symbol>` — what the symbol calls (call/reference out-edges).
- `path <a> -> <b>` — shortest directed path over any edges.
- `reaches <a> -> <b>` — directed path over call/reference edges only.
- `community-of <node>` — cluster label for a node.
- `stats` — graph statistics: counts, density, nodes by kind, edges by relation, top-degree nodes.

When a node is not found, the error JSON includes a `candidates` list of close matches — retry with one of those ids.

Examples:
- `/graphite query "depends-on db.ts"`
- `/graphite query "path article-gen/route.ts -> db.ts"`

## Claude Code MCP tools

When the Graphite MCP server is configured, Claude can query the graph directly:
- `graphite_query` — run structured queries.
- `graphite_community` — describe a node's community/cluster.
- `graphite_summary` — high-level stats, god nodes, entry points.
- `graphite_refresh` — rebuild and reload `graph-out/graph.json`.

To configure, install the MCP dependency and add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "graphite": {
      "command": "python",
      "args": [
        "-m",
        "graphite.mcp"
      ],
      "env": {},
      "cwd": "F:\\Projects\\YourProject"
    }
  }
}
```

## Implementation

This skill calls the central `graphite` Python package installed from `F:\Projects\graphite` (its own git repo).

```bash
pip install -e F:\Projects\graphite
python -m graphite -v build .
```

When LLM enrichment is desired, keep the default zero-LLM workflow unless the user explicitly opts in. Graphite is model agnostic:

```bash
# Native Ollama, local only
GRAPHITE_LLM=local GRAPHITE_LLM_PROVIDER=ollama GRAPHITE_LLM_MODEL=qwen2.5-coder graphite report .

# Generic OpenAI-compatible endpoint
GRAPHITE_LLM=cloud GRAPHITE_LLM_PROVIDER=openai-compatible GRAPHITE_LLM_BASE_URL=https://example.com/v1 GRAPHITE_LLM_MODEL=my-model GRAPHITE_LLM_API_KEY=... graphite report .
```

Supported adapters are `ollama` and `openai-compatible`, with aliases for `openai`, `openrouter`, `groq`, `lmstudio`, and `vllm`. LLM enrichment sends bounded graph metadata and summaries, not source code by default.

## Safety

- No absolute paths or system metadata leak into artifacts.
- No external API calls in default mode.
- Generated output goes to `graph-out/` which is gitignored.
















