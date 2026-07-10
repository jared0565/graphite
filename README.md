# Graphite

Local-first, deterministic knowledge graph extraction for codebases. A safer, faster, cheaper replacement for `graphify`.

## Principles

- **Zero-LLM by default** — structural extraction only; no API keys, no tokens, no cost.
- **Local-first** — runs entirely on your machine unless optional LLM enrichment is explicitly enabled.
- **Model-agnostic enrichment** — optional summaries use native Ollama or any OpenAI-compatible HTTP endpoint.
- **Deterministic graph** — same commit produces the same structural graph.
- **Safe output** — no absolute paths or system metadata leak into artifacts.
- **Incremental** — content-addressed cache means only changed files are re-parsed.
- **TypeScript-aware** — uses the local TypeScript compiler API when available, with heuristic fallback.

## Installation

```bash
pip install -e F:/Projects/graphite
```

No model SDK is required for optional LLM enrichment; Graphite uses standard-library HTTP adapters.

## Global F:/Projects usage

This repository lives at `F:\Projects\graphite` (its own git repo) and is pip-installed editable, so `python -m graphite` works from any project in any shell. The `graphite` / `graphite-mcp` console-script shims are equivalent where they are on PATH (on this machine: PowerShell/cmd via `C:\Users\fbmac\.local\bin\graphite.cmd`, but not Git Bash — prefer `python -m graphite` in scripts and agent instructions).

To onboard a new or existing project, run one command from anywhere:

```bash
python -m graphite init F:/Projects/MyApp        # agent instructions + gitignore + first build + validation
python -m graphite bootstrap F:/Projects/MyApp   # minimal variant: gitignore + AGENTS.md + build
```

The machine-wide daemon (`graphite daemon F:\Projects`) auto-discovers any project with standard markers (`.git`, `package.json`, `pyproject.toml`, `wrangler.toml`, `go.mod`, `Cargo.toml`) and keeps its graph fresh, so `init` is about wiring agent instructions, not registration.

## Usage

```bash
# Scan a repo (zero tokens)
graphite scan .

# Build the graph (zero tokens)
graphite build .

# Generate report and interactive viewer
graphite report .

# Query the graph
# Verbs: depends-on, imported-by, callers, calls, path <a> -> <b>,
#        reaches <a> -> <b> (call/reference edges only), community-of, stats
graphite query "depends-on src/lib/db.ts"
graphite query "callers calculateCommissionPence"

# Check whether graph-out is current
graphite check .

# Suggest files and tests affected by a change
graphite impact src/lib/db.ts

# Compact agent-ready context for a file or node
graphite context src/lib/db.ts

# Initialize shared Graphite instructions for AI coding platforms
graphite init F:/Projects/MyApp
graphite init . --platform codex --platform claude
graphite init . --all

# Make a project Graphite-ready
graphite bootstrap F:/Projects/MyApp

# Check daemon health
graphite daemon-health F:/Projects

# Audit whether Graphite can replace Graphify for a project
graphite audit-replacement F:/Projects/MyApp
```

## Graphify replacement audit

Use the replacement audit before removing legacy Graphify files or ignore entries:

```bash
graphite audit-replacement F:/Projects/MyApp
graphite audit-replacement . --json
graphite audit-replacement . --fail-on-blocker
```

The audit checks Graphite bootstrap state, graph freshness and validity, daemon visibility, daemon health, physical Graphify remnants, and Graphify text/config references. It reports recommendations but never deletes files automatically.

## Daemon health

Use daemon health for operational checks and automation:

```bash
graphite daemon-health F:/Projects
graphite daemon-health F:/Projects --json
graphite daemon-health F:/Projects --fail-on-error
```

Health checks include status age, daemon process presence, startup launcher installation, failing projects, pending initial builds, and projects that have not built successfully within the configured age window.


## AI platform initialization

Use `graphite init` when you enter a new project and want AI coding tools to share one Graphite workflow:

```bash
graphite init .
graphite init . --platform codex --platform claude
graphite init . --platform antigravity --platform visual-studio
graphite init . --all
graphite init --list-platforms
```

When no platform is supplied in an interactive terminal, Graphite presents the common platform list and lets you choose. In non-interactive mode, it defaults to Codex, Claude Code, Antigravity, and Visual Studio/GitHub Copilot. The command creates or updates `GRAPHITE.md` with the required workflow plus optional LLM-enrichment instructions, and updates the selected platform instruction files, including `AGENTS.md`, `CLAUDE.md`, `ANTIGRAVITY.md`, `.github/copilot-instructions.md`, `.cursor/rules/graphite.mdc`, and `.windsurfrules` as applicable. It also keeps default-deny `.gitignore` repositories from hiding those instruction files.

## Project bootstrap

Use bootstrap for new or existing projects that should join the Graphite workflow:

```bash
graphite bootstrap F:/Projects/MyApp
graphite bootstrap . --no-build
graphite bootstrap . --json
```

Bootstrap updates `.gitignore`, creates or extends `AGENTS.md` with the auto-consult workflow, checks daemon visibility, builds the initial graph by default, and validates `graph-out/graph.json`.

## Agent auto-consult workflow

For non-trivial code changes, agents should consult Graphite before broad file reads or edits:

```bash
graphite check .
graphite context src/lib/db.ts
graphite impact src/lib/db.ts
graphite query "stats"
```

Use `graphite context` first when you know the likely file. It returns matched nodes, direct dependencies, direct dependents, impacted files, likely tests, community peers, and coupling risk signals without dumping the full graph.

## Artifact validation

Every successful build validates the public `graph-out/graph.json` bundle before publishing reports and writes `graph-out/.graphite_validation.json`.

Use this in CI, pre-commit checks, or before relying on an existing graph:

```bash
graphite validate
graphite validate --json
```

Validation checks include:

- node IDs are present and unique
- edge sources and targets exist
- metadata counts match actual graph contents
- generated artifacts do not leak absolute filesystem paths
- cluster members refer to known nodes

Graphite writes artifacts atomically so interrupted builds do not leave partially written JSON, Markdown, or HTML files.

## TypeScript compiler-backed resolution

Graphite defaults to `GRAPHITE_TYPESCRIPT_RESOLVER=auto`. For TypeScript/JavaScript projects, it tries to use the project's installed `typescript` package to resolve imports and exports more accurately.

This improves:

- `tsconfig` path aliases
- `index.ts` barrels
- `export ... from` and `export * from` re-exports
- dynamic imports like `await import("./feature")`
- type-only import confidence labels
- file-level runtime symbol references
- file-level type references

If Node or TypeScript is unavailable, Graphite falls back to its deterministic heuristic resolver and keeps building the graph.

Controls:

```bash
graphite --typescript-resolver auto build .
graphite --typescript-resolver disabled build .
graphite --typescript-resolver-timeout 5 build .
graphite --no-typescript-symbol-references build .
```

Environment variables:

- `GRAPHITE_TYPESCRIPT_RESOLVER`: `auto`, `compiler`, `heuristic`, or `disabled`.
- `GRAPHITE_TYPESCRIPT_RESOLVER_TIMEOUT`: compiler resolver timeout in seconds.
- `GRAPHITE_TYPESCRIPT_SYMBOL_REFERENCES`: `true` or `false` for compiler-backed symbol/type reference edges.

## Background watcher

Use the watcher during active development when you want `graph-out` to stay current automatically:

```bash
graphite watch . --impact
```

Behavior:

- Builds once on startup unless `--no-initial-build` is set.
- Polls locally with no network calls by default.
- Debounces file changes before rebuilding, so save bursts do not cause repeated builds.
- Uses content hashes, not timestamps, to avoid unnecessary rebuilds.
- With `--impact`, prints impacted files and likely tests from the previous graph before rebuilding.
- Does not enable LLM enrichment unless `GRAPHITE_LLM` or `--llm` is explicitly set.

Useful controls:

```bash
graphite watch . --impact --interval 2 --debounce 1
graphite watch . --once --interval 0.1 --debounce 0
graphite watch . --no-initial-build
```

## Multi-project daemon

Use the daemon when you want Graphite to keep every discovered project under `F:\Projects` fresh without manually starting a watcher in each repo:

```bash
# One-shot health/build pass
graphite daemon F:\Projects --once

# Persistent local supervisor
graphite daemon F:\Projects

# Read latest health/status
graphite daemon-status F:\Projects
```

Daemon behavior:

- Discovers project roots by markers such as `.git`, `package.json`, `pyproject.toml`, `wrangler.toml`, `go.mod`, and `Cargo.toml`.
- Skips heavy/tool folders such as `node_modules`, `.git`, `graph-out`, `.cache`, `dist`, `build`, and `_tools`.
- Writes local operational state to `<base>/.graphite-daemon/status.json` and JSONL logs to `<base>/.graphite-daemon/graphite-daemon.log`.
- Limits work with `--max-projects`, `--max-depth`, `--max-files-per-project`, `--max-builds-per-cycle`, and `--build-timeout`.
- Runs child builds with isolated stdin and zero-LLM mode unless LLM flags/environment variables are explicitly enabled.

Useful controls:

```bash
graphite daemon F:\Projects --scan-interval 10 --discover-interval 60
graphite daemon F:\Projects --max-builds-per-cycle 1 --build-timeout 180
graphite daemon F:\Projects --no-initial-build
```

Windows startup integration:

```bash
# Install as a current-user logon task and start immediately
graphite daemon-install-windows F:\Projects --start-now

# Inspect the scheduled task
graphite daemon-task-status

# Remove the scheduled task
graphite daemon-uninstall-windows
```

The installed task is named `GraphiteDaemon-FProjects` by default and uses the same bounded zero-LLM daemon defaults.

If Task Scheduler creation is blocked by Windows policy, install the non-admin Startup-folder fallback:

```bash
graphite daemon-install-startup-windows F:\Projects
graphite daemon-startup-status F:\Projects
graphite daemon-uninstall-startup-windows F:\Projects
```

The fallback writes a hidden VBS launcher in the current user's Startup folder and an idempotent PowerShell launcher in `F:\Projects\.graphite-daemon`.

## Optional LLM enrichment

LLM enrichment is off by default. When enabled, Graphite sends bounded graph metadata and analysis summaries, not source code, to the configured provider. Use `--llm auto` when you want Graphite to decide whether the graph is complex/risky enough to justify the extra LLM call.

```bash
# Native Ollama, local only
GRAPHITE_LLM=local GRAPHITE_LLM_PROVIDER=ollama GRAPHITE_LLM_MODEL=qwen2.5-coder graphite report .

# OpenAI-compatible local server, such as LM Studio
GRAPHITE_LLM=local GRAPHITE_LLM_PROVIDER=lmstudio GRAPHITE_LLM_MODEL=local-model graphite report .


# OpenRouter model routing
GRAPHITE_LLM=cloud GRAPHITE_LLM_PROVIDER=openrouter GRAPHITE_LLM_MODEL=~openai/gpt-latest GRAPHITE_LLM_API_KEY=... graphite report .
GRAPHITE_LLM=cloud GRAPHITE_LLM_PROVIDER=openrouter GRAPHITE_LLM_MODEL=~anthropic/claude-sonnet-latest GRAPHITE_LLM_API_KEY=... graphite report .
# Generic OpenAI-compatible endpoint
GRAPHITE_LLM=cloud GRAPHITE_LLM_PROVIDER=openai-compatible GRAPHITE_LLM_BASE_URL=https://example.com/v1 GRAPHITE_LLM_MODEL=my-model GRAPHITE_LLM_API_KEY=... graphite report .
```

Equivalent CLI flags are available:

```bash
graphite --llm auto --llm-provider openrouter report .
graphite --llm local --llm-provider ollama --llm-model qwen2.5-coder report .
graphite --llm cloud --llm-provider openai-compatible --llm-base-url https://example.com/v1 --llm-model my-model report .
graphite --llm cloud --llm-provider openrouter report .
graphite --llm cloud --llm-provider openrouter --llm-model "~openai/gpt-latest" report .
```

Supported provider adapters:

- `ollama` — native `http://localhost:11434/api/chat`.
- `openai-compatible` — any `/v1/chat/completions` compatible endpoint.
- Aliases with sensible base URLs: `openai`, `openrouter`, `groq`, `lmstudio`, `vllm`.
- `openrouter` uses `https://openrouter.ai/api/v1` and defaults to `moonshotai/kimi-k2.7-code`; use `--llm-model` for any OpenRouter model slug, including latest aliases such as `~openai/gpt-latest`.

Relevant environment variables:

- `GRAPHITE_LLM`: `none`, `auto`, `local`, or `cloud`.
- `GRAPHITE_LLM_PROVIDER`: `ollama`, `openai-compatible`, `openai`, `openrouter`, `groq`, `lmstudio`, or `vllm`.
- `GRAPHITE_LLM_MODEL`: model name.
- `GRAPHITE_LLM_BASE_URL`: provider base URL.
- `GRAPHITE_LLM_API_KEY`: provider API key; do not commit this.
- `GRAPHITE_LLM_TIMEOUT`: request timeout seconds.
- `GRAPHITE_LLM_MAX_INPUT_CHARS`: prompt input budget.

Auto mode currently runs only when graph signals pass conservative thresholds, such as larger node/edge counts, many communities, god nodes, surprising connections, or highly linked files. It records the decision and reason in `graph-out/.graphite_manifest.json` and `GRAPH_REPORT.md`.

## Output

Artifacts are written to `graph-out/`:

- `graph.json` — bundled graph for external tools
- `GRAPH_REPORT.md` — human-readable audit
- `graph.html` — interactive viewer
- `.graphite_*.json` — intermediate pipeline artifacts

## Claude Code skill

A skill template lives at `F:\Projects\graphite\skill\SKILL.md`. To install:

```bash
mkdir -p ~/.claude/skills/graphite
cp /f/Projects/graphite/skill/SKILL.md ~/.claude/skills/graphite/SKILL.md
```

Then use `/graphite [path]` inside Claude Code. The skill defaults to zero-LLM mode.

### MCP server for Claude Code

Install the optional MCP dependency:

```bash
pip install -e "F:/Projects/graphite[mcp]"
```

Then configure Claude Code (Desktop) to use the local server. Add this to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "graphite": {
      "command": "python",
      "args": ["-m", "graphite.mcp"],
      "cwd": "F:/Projects/YourProject"
    }
  }
}
```

Once configured, Claude can call these tools automatically:

- `graphite_query` — e.g. `depends-on db.ts`, `imported-by db.ts`, `path article-gen/route.ts -> db.ts`, `stats`
- `graphite_community` — list the community around a node
- `graphite_summary` — stats, god nodes, entry points, surprising connections
- `graphite_refresh` — rebuild and reload the graph













