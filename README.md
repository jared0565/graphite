# Graphite

Local-first, deterministic knowledge graph extraction for codebases. A safer, faster, cheaper replacement for `graphify`.

## Principles

- **Zero-LLM by default** — structural extraction only; no API keys, no tokens, no cost.
- **Local-first** — runs entirely on your machine unless optional LLM enrichment is explicitly enabled.
- **Model-agnostic enrichment** — optional summaries use native Ollama or any OpenAI-compatible HTTP endpoint.
- **Deterministic graph** — same commit produces the same structural graph.
- **Safe output** — no absolute paths or system metadata leak into artifacts.
- **Incremental** — content-addressed cache means only changed files are re-parsed.
- **Multi-language** — structural extraction for TypeScript/JavaScript, Python, Go, and Rust.
- **TypeScript-aware** — uses the local TypeScript compiler API when available, with heuristic fallback.

## Contributing and project internals

- [Contributor guide](CONTRIBUTING.md) — development setup, testing, security expectations, and pull-request conventions.
- [Architecture guide](ARCHITECTURE.md) — pipeline, module boundaries, artifacts, extension points, and failure behavior.
- [Release guide](RELEASING.md) — maintainer verification, packaging, tagging, publication, and recovery steps.

## Installation

```bash
pip install -e F:/Projects/graphite
```

No model SDK is required for optional LLM enrichment; Graphite uses standard-library HTTP adapters.

## System readiness and optional integrations

Use the doctor before enabling optional integrations or when diagnosing a host. The fast command performs read-only checks; deep mode exercises the deterministic pipeline and configured integration boundaries:

```bash
python -m graphite doctor .
python -m graphite doctor . --deep
python -m graphite doctor . --deep --include-llm
```

Each check is `ready` when usable, `optional` when an absent integration does not affect core operation, `degraded` when a non-core capability needs attention, or `blocked` when a core safety or execution requirement failed. The overall result is the most severe check. The exit code boundary is deliberately narrow: `blocked` exits 1, while `ready`, `optional`, and `degraded` exit 0. Use `--json` for the stable machine-readable report.

Fast checks do not write to the selected repository. Deep pipeline work writes only to an external private temporary workspace; the selected repository remains read-only. On Windows, the private parent and workspace directories are created with a protected, inheritable current-user DACL. That is a creation-time guarantee, not a claim that the DACL is re-read during every probe phase.

Separately, the no-follow lease validates canonical containment, pinned directory handles, reparse state, and directory identity/bindings before and after each phase. Child processes receive native Job Object containment on Windows or POSIX process group containment, bounded I/O, and one end-to-end deadline. Cleanup is reserved within that deadline. A cleanup timeout is reported as a blocked result, and the cleanup worker retains sole ownership of the live lease while overlapping core probes in the same interpreter/process remain blocked. The local OS user and same-user process namespace remain a best-effort trust boundary: these controls reduce pathname races and contain descendants but cannot fully isolate a malicious process running as the same user.

MCP is optional. Before optional activation installs, the mandatory repository package-validation policy requires a trusted local validator. Set `GRAPHITE_PACKAGE_VALIDATOR` to the absolute path of the trusted `validate-packages.cjs` maintained by your environment. If the variable is unset, relative, missing, or does not name an existing file, stop. Never execute a relative repository-local validator. Do not download a validator, search for an unknown replacement, or fall back to an unverified script.

Run the applicable fail-closed check and validation command. PowerShell:

```powershell
if (
  [string]::IsNullOrWhiteSpace($env:GRAPHITE_PACKAGE_VALIDATOR) -or
  -not ([System.IO.Path]::IsPathFullyQualified($env:GRAPHITE_PACKAGE_VALIDATOR)) -or
  -not (Test-Path -LiteralPath $env:GRAPHITE_PACKAGE_VALIDATOR -PathType Leaf)
) { throw "GRAPHITE_PACKAGE_VALIDATOR is unset, relative, or missing; stop." }
node $env:GRAPHITE_PACKAGE_VALIDATOR mcp
if ($LASTEXITCODE -ne 0) { throw "Package validation failed; stop." }
```

POSIX shell:

```sh
if [ -z "${GRAPHITE_PACKAGE_VALIDATOR:-}" ]; then
  printf '%s\n' 'GRAPHITE_PACKAGE_VALIDATOR is unset; stop.' >&2
  exit 1
fi
case "$GRAPHITE_PACKAGE_VALIDATOR" in
  /*) ;;
  *) printf '%s\n' 'GRAPHITE_PACKAGE_VALIDATOR must be an absolute POSIX path; stop.' >&2; exit 1 ;;
esac
if [ ! -f "$GRAPHITE_PACKAGE_VALIDATOR" ]; then
  printf '%s\n' 'GRAPHITE_PACKAGE_VALIDATOR is missing; stop.' >&2
  exit 1
fi
node "$GRAPHITE_PACKAGE_VALIDATOR" mcp || exit 1
```

Only after the applicable validator command succeeds, enable the declared extra:

```bash
python -m pip install -e ".[mcp]"
```

The deep MCP probe launches an isolated interpreter from a guarded distribution-record import manifest. It rejects current working directory, user-site, and attacker-controlled selected-root shadows. The exact origin-verified trusted Graphite source may be inside the selected repository, but it is accepted only when its expected lexical, canonical, filesystem-identity, and module-origin checks all match; overlapping MCP dependency or distribution-metadata roots and alternate Graphite origins remain rejected.

TypeScript compiler resolution is also optional. Use the same configured validator and fail-closed fully-qualified-path and existence checks, changing only the validated package argument to `typescript`:

```powershell
node $env:GRAPHITE_PACKAGE_VALIDATOR typescript
if ($LASTEXITCODE -ne 0) { throw "Package validation failed; stop." }
```

```sh
node "$GRAPHITE_PACKAGE_VALIDATOR" typescript || exit 1
```

Use the environment variable commands above, or substitute the clearly marked placeholder below with the trusted absolute validator path for your environment:

```text
node "<absolute-path-to-validator>" typescript
```

The angle-bracket value is a placeholder, not a literal path or a repository-local validator. Then use the target project's existing package manager to add the verified `typescript` package locally; do not install it globally. Doctor statically detects project-local TypeScript from package metadata but intentionally never executes or transpiles untrusted project JavaScript. A detected compiler therefore remains optional/unverified rather than being treated as executed proof.

The validator target in that command is `validate-packages.cjs typescript`; preserve that package spelling exactly.

Local Ollama activation needs no API key: set `GRAPHITE_LLM=local`, `GRAPHITE_LLM_PROVIDER=ollama`, and an explicit `GRAPHITE_LLM_MODEL`; leave `GRAPHITE_LLM_API_KEY` unset. For a cloud provider, use a newly rotated, session-scoped `GRAPHITE_LLM_API_KEY` and explicitly set the provider, model, and HTTPS base URL. Never place the value in a command example, repository file, persistent parent-process configuration, shell history, or log. If a credential may have been exposed, revoke it in the provider dashboard, remove it from parent secret configuration, rotate it, and restart the parent and all affected processes so they cannot retain the old environment.

`--include-llm` is an explicit network action and uses synthetic content only. It sends one bounded constant probe with no repository data, follows no redirects or retries, and reports neither response text, raw error text, nor secrets. The normal enrichment setting `GRAPHITE_LLM_MAX_OUTPUT_TOKENS` defaults to 512 and is clamped to 1–4096. The doctor probe overrides it with a fixed 16-token cap. Keep the LLM probe disabled unless network access to the configured endpoint is approved.

## Global F:/Projects usage

This repository lives at `F:\Projects\graphite` (its own git repo) and is pip-installed editable, so `python -m graphite` works from any project in any shell. The `graphite` / `graphite-mcp` console-script shims are equivalent where they are on PATH (on this machine: PowerShell/cmd via `C:\Users\fbmac\.local\bin\graphite.cmd`, but not Git Bash — prefer `python -m graphite` in scripts and agent instructions).

To onboard a new or existing project, run one command from anywhere:

```bash
python -m graphite init F:/Projects/MyApp        # agent instructions + gitignore + first build + validation
python -m graphite bootstrap F:/Projects/MyApp   # minimal variant: gitignore + AGENTS.md + build
```

The machine-wide daemon (`graphite daemon F:\Projects`) auto-discovers any project with standard markers (`.git`, `package.json`, `pyproject.toml`, `wrangler.toml`, `go.mod`, `Cargo.toml`) and keeps its graph fresh, so `init` is about wiring agent instructions, not registration.

Set `GRAPHITE_PROJECTS_ROOT` to change the default base folder used by `daemon`, `daemon-status`, `daemon-health`, the Windows startup installers, and init/bootstrap daemon-visibility checks (falls back to `F:/Projects` when it exists, else the current directory).

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
# Responses include `match` metadata (exact-id | name | path-suffix | fuzzy,
# plus alternates when a token was ambiguous); not-found errors include a
# `candidates` list of close matches.
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

On Windows, process enumeration may require elevated CIM access. If the operating system denies that read-only observation, daemon health reports `daemon_process_check_unavailable` as a warning rather than claiming the daemon is stopped. Fresh status updates and selected-project health remain usable; run the same health command from an elevated shell when a definitive process-presence check is required.


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

## Consent-gated project-local TypeScript activation

After `graphite init` or `graphite bootstrap` writes its normal onboarding files, Graphite checks whether the selected root has `.ts`/`.tsx` source or `tsconfig.json` evidence but insufficient project-local TypeScript support. Core graphing does not require the compiler: when activation is unavailable, declined, or ineligible, Tree-sitter extraction and heuristic resolution remain available. Graphite adds only the exact project-local `typescript` development dependency; it does not infer `@types/*` or install frameworks or adjacent tooling. Graphite does not install global TypeScript.

Automatic activation requires a contained regular `package.json`, exactly one supported root lockfile, matching `package.json#packageManager` metadata when present, safe control-file dependency sources, no manager-specific configuration that could redirect the operation, a supported external manager executable/version, and project-local TypeScript not already being resolvable. The automatic matrix is npm 8–11 with `package-lock.json`, pnpm 11 with `pnpm-lock.yaml`, and Bun 1 with exactly one of `bun.lock` or `bun.lockb`. Yarn is `guidance_only` because Graphite cannot currently prove a version-independent unattended registry, credential, and lifecycle-script boundary. Missing, nested-only, malformed, conflicting, ambiguous, or unsafe evidence also returns `guidance_only`; Graphite never guesses npm or a workspace package.

Eligibility reads and snapshots of `package.json` and the selected lockfile happen before the prompt. In an interactive terminal Graphite prompts exactly once after those checks and before validator, network, or any manifest, lockfile, or dependency-store mutation:

```text
Project-local TypeScript is missing. Install it with <manager> as a development dependency? [y/N]
```

The prompt defaults to No. Only an explicit `y` or `yes`, case-insensitively, grants consent. Empty input, EOF, malformed input, and every other response mean `declined`. There is no remembered consent between repositories or invocations. JSON, CI, redirected stdin, redirected stdout, and `--yes` are non-interactive activation modes: they never prompt, validate, or install and instead return a non-mutating result such as `guidance_only`, `already_available`, or `not_applicable`.

Consent does not bypass validation. `GRAPHITE_PACKAGE_VALIDATOR` must identify an absolute, existing regular file outside the selected root. Graphite invokes that validator through a trusted external Node executable with the exact argument `typescript`; unset, relative, missing, repository-contained, changed, rejected, or non-file validators fail closed before installation. The automatic path permits only `https://registry.npmjs.org/`, removes ambient registry tokens and repository-controlled overrides, uses fixed argv with lifecycle scripts disabled, closes child stdin, and bounds output, descendants, and the shared deadline. Private registries and enterprise mirrors use the manual workflow under the operator's existing package-management policy.

Onboarding files are written before activation and remain preserved. Activation then runs before the normal optional build and validation stages. `installed`, `already_available`, `not_applicable`, `declined`, and `guidance_only` do not make otherwise-successful onboarding fail. Explicitly approved `validation_failed`, `installation_failed`, and `verification_failed` outcomes preserve the completed onboarding files but make `init` or `bootstrap` return exit code 1. Package-manager changes remain visible for review; Graphite performs no automatic rollback that could overwrite concurrent edits.

When automatic activation is unavailable, follow this fixed manual workflow in order:

1. Set `GRAPHITE_PACKAGE_VALIDATOR` to your environment's trusted absolute validator path outside the project.
2. Fail closed if it is unset, relative, missing, or not a regular file.
3. Run the validator for the exact package name `typescript`, for example `node "$GRAPHITE_PACKAGE_VALIDATOR" typescript`, and stop on failure.
4. Only after successful validation, use the project's existing package manager to add `typescript` as a local development dependency with lifecycle scripts disabled according to local registry and credential policy.
5. Rerun `graphite doctor` or onboarding to confirm project-local detection.

Normal `build`, `report`, `check`, `doctor`, `daemon`, `watch`, MCP, agent, and other non-onboarding paths have no TypeScript installation authority. These controls reduce and contain risk; they are not a claim that Graphite or the local host is unhackable.

## Agent auto-consult workflow

For non-trivial code changes, agents should consult Graphite before broad file reads or edits:

```bash
graphite check .
graphite context src/lib/db.ts
graphite impact src/lib/db.ts
graphite query "stats"
```

Use `graphite context` first when you know the likely file. It returns matched nodes, direct dependencies, direct dependents, impacted files, likely tests, community peers, and coupling risk signals without dumping the full graph.

## Deterministic change review

Use `review-changes` to turn a change set into a deterministic review packet before accepting or merging it:

```bash
# Discover all current Git changes and render a Markdown packet
graphite review-changes .

# Emit stable, machine-readable evidence
graphite review-changes . --json

# Opt in to a non-zero exit only when the packet contains a blocker
graphite review-changes . --json --fail-on-blocker

# Review an explicitly selected scope instead of Git discovery
graphite review-changes . src/lib/db.py tests/test_db.py --json

# Use a graph contained within the project root
graphite review-changes . src/lib/db.py --graph-json artifacts/graph.json --json
```

With no selected files, Git discovery covers staged, unstaged, untracked, deleted, and renamed paths. With selected files, the packet uses exactly that explicit scope. The command checks graph freshness and validates the packet graph, derives reverse-dependency impact and likely tests, reports risk signals transparently, and emits concrete acceptance criteria. A custom graph uses the `.graphite_manifest.json` beside that graph for freshness checks.

Review freshness and repository ingestion share one hardened Git boundary: Graphite selects an absolute external Git executable, removes inherited `GIT_*` redirection, disables optional locks and repository-configured fsmonitor, and fails closed on Git or protocol errors. Git repositories must be processed from their top-level root; unsupported nested roots are rejected rather than scanned with a filesystem fallback.

`review-changes` is zero-LLM, local, deterministic, and model-, vendor-, and agent-agnostic. The command itself makes no network requests and transmits nothing. Its local output intentionally contains repository, project, path, graph, and dependency metadata, so callers must protect logs, pipes, and uploaded output. For a successfully constructed packet, risk does not affect exit status; `--fail-on-blocker` makes evidence blockers return `1`. Invalid inputs and operational errors return `1` independently.

For containment and resource safety, a custom `--graph-json` must resolve inside the reviewed project root and may be at most 128 MiB. Git stdout is capped at 16 MiB and Git status/file record counts are capped at 100,000. Evidence strings and paths are validated before they enter the packet, and low-level parser, filesystem, and Git errors are not copied into review output. Resolved output and cache directories are excluded from ingestion, including custom locations, so a build does not ingest its own artifacts or immediately make its graph stale. Packet, impact, and rendered-output cardinality remain residual limits; see the audit.

The workflow is informed by the pinned [Karpathy-inspired Think Before Coding, Simplicity, Surgical Changes, and Goal-Driven Execution principles](https://github.com/multica-ai/andrej-karpathy-skills/blob/2c606141936f1eeef17fa3043a72095b4765b9c2/README.md) and the [Superpowers spec-to-plan, TDD, and review philosophy](https://github.com/obra/superpowers/blob/d884ae04edebef577e82ff7c4e143debd0bbec99/README.md). Graphite implements those ideas as local evidence and acceptance packets; it does not impose or require any agent vendor.

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

LLM enrichment is off by default. When enabled, its prompt does not intentionally include source-file contents, but it transmits graph metadata, filenames, identifiers, labels, and analysis summaries to the configured provider. Use `--llm auto` when you want Graphite to decide whether the graph is complex/risky enough to justify the extra LLM call.

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

## Adaptive development routing

Graphite can recommend an Ollama Cloud model for a development task and, only after
separate interactive consent, make one bounded request through the local Ollama
loopback API. This development router is distinct from optional report enrichment:
OpenRouter is reserved for production in-application inference. Claude Code and
Codex are manual handoff channels only; Graphite never launches either CLI.

```powershell
graphite route policy . --refresh-models --json
graphite route recommend . --objective "Review listing search" --target src/search.py
graphite route run . --objective "Review listing search" --target src/search.py
graphite route status . --json
```

`route recommend` is offline and read-only. It requires a fresh validated graph and
a previously refreshed model snapshot. `route run` prints the selected model,
effort, quota estimate, and outbound manifest before asking for consent. Approval
defaults to No. JSON, CI, redirected input/output, and `--yes` cannot execute a
model. Source context leaves the machine only after the manifest is displayed and
the user explicitly answers yes.

Every approval is signed, short-lived, single-use, model/digest-bound, effort-bound,
context-bound, and quota-bound. Model output is untrusted, has no tool authority,
and cannot mutate code. High-risk work retains a permanent approval gate. Shadow
evaluation is disabled by default, separately consented, independently budgeted,
and unavailable for high-risk or sensitive categories.

Detailed receipts and evidence stay in repository-local `.graphite/routing` storage;
they contain hashes and metadata, not prompts or model responses. Machine-wide
sanitized aggregate learning is opt-in and contains only allowlisted enums, coarse
buckets, and version identifiers. The default retention window is 90 days. Use
policy rollback to restore a prior recommendation policy; removing local evidence
is an explicit operator action, never an automatic side effect.

Incident response: stop routing, preserve the append-only evidence, revoke exposed
credentials if any external system was involved, review the execution correlation,
close the incident explicitly, and start a new evidence window. A provider outage
or blocked recommendation does not weaken approval or security gates.

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

Complete the mandatory package-validation policy and MCP activation steps in [System readiness and optional integrations](#system-readiness-and-optional-integrations). Do not bypass or reorder the validator and install steps.

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
