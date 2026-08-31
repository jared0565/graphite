# Graphite

Local-first, deterministic knowledge graph extraction for codebases — zero-LLM, daemon-maintained, agent-agnostic.

**Status: 1.0.0, production/stable.** Published on PyPI as
[`graphite-code`](https://pypi.org/project/graphite-code/) with PEP 740
attestations; supported on Windows, Linux and macOS with Python 3.11–3.14
(every cell gates CI). What 1.x promises — CLI, JSON outputs, `graph.json`,
configuration, exit codes, the launch contract — is in
[docs/compatibility.md](docs/compatibility.md).

## Documentation

- [User guide](docs/user-guide.md) — install, build the first graph, ask questions, onboard a repository for agents, keep graphs fresh with the daemon, upgrade and verify a release.
- [Knowledge base](docs/knowledge-base.md) — symptoms, causes and remedies collected from operating graphite; read it before filing an issue.
- [Agent integration guide](docs/agent-integration.md) — how a coding agent or script drives the query interface and grades its answers.
- Reference, each page kept in lockstep with the code by a test: [CLI](docs/reference/cli.md), [configuration](docs/reference/configuration.md), [exit codes](docs/reference/exit-codes.md), [compatibility and support](docs/compatibility.md), [benchmarks](docs/benchmarks.md).
- [Security policy](SECURITY.md) — supported versions and the private reporting path.

## Principles

- **Inference-free canonical graph** — structural extraction never reads provider credentials or invokes a model.
- **Local-first** — canonical scan, build, report, check, query, context, impact, watch, and daemon operations stay local.
- **Isolated enrichment** — model output belongs only in explicit, non-authoritative overlays and never changes canonical artifacts.
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
python -m pip install --user graphite-code
graphite --version          # version, engine fingerprint, cache and schema versions
```

The distribution is `graphite-code` (PyPI's `graphite` is another project);
the import package is `graphite` and the console scripts are `graphite` and
`graphite-mcp`. Requires Python 3.11–3.14. No model SDK or provider
credential is required for canonical graph operation. Every release is built
by CI from its tag, published with PEP 740 attestations, and verifiable from
the index — see "Upgrading and verifying a release" in the
[user guide](docs/user-guide.md).

Installing for one interpreter is what makes `python -P -m graphite` work
from any repository on the machine, which is how every onboarded project
reaches it. Keep the `-P`: without it Python puts the current directory
first on `sys.path`, so a `graphite.py` or `graphite/__init__.py` planted at
a repository root would be imported instead of the installed package. The
console scripts are immune by construction. Contributors install the clone
editable into a virtual environment **outside** the checkout — see
[CONTRIBUTING.md](CONTRIBUTING.md).

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
python -m pip install --user "graphite-code[mcp]"
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

Canonical commands ignore ambient `GRAPHITE_LLM*` settings and never read `GRAPHITE_LLM_API_KEY`. Optional doctor probing remains a separate, explicit network action. For the explicit doctor probe, local Ollama needs no API key; a cloud probe requires a newly rotated, session-scoped value. Never place a credential in a repository file, persistent parent-process configuration, shell history, or log. If a credential may have been exposed, revoke it in the provider dashboard, remove it from parent secret configuration, rotate it, and restart the parent and all affected processes so they cannot retain the old environment.

`--include-llm` is an explicit network action and uses synthetic content only. It sends one bounded constant probe with no repository data, follows no redirects or retries, and reports neither response text, raw error text, nor secrets. The normal enrichment setting `GRAPHITE_LLM_MAX_OUTPUT_TOKENS` defaults to 512 and is clamped to 1–4096. The doctor probe overrides it with a fixed 16-token cap. Keep the LLM probe disabled unless network access to the configured endpoint is approved.

## Machine-wide usage

Installed for an interpreter, `python -P -m graphite` works from any project in any shell. The `graphite` / `graphite-mcp` console scripts are equivalent wherever they are on PATH — and shadow-proof by construction — but a scripts directory that PowerShell and cmd see is not always on Git Bash's PATH, so prefer `python -P -m graphite` in scripts, hooks and agent instructions. Everything `graphite init` generates (hooks, `.mcp.json`, editor tasks, daemon launchers) already carries the `-P`.

To onboard a new or existing project, run one command from anywhere:

```bash
python -m graphite init /path/to/MyApp        # agent instructions + gitignore + first build + validation
python -m graphite bootstrap /path/to/MyApp   # minimal variant: gitignore + AGENTS.md + build
```

The machine-wide daemon (`graphite daemon /path/to/projects`) auto-discovers any project with standard markers (`.git`, `package.json`, `pyproject.toml`, `wrangler.toml`, `go.mod`, `Cargo.toml`) and keeps its graph fresh, so `init` is about wiring agent instructions, not registration. To exclude a directory (and its whole subtree) from supervision — e.g. a third-party SDK checkout — drop a `.graphite-ignore` file in it; the daemon skips it at the next discovery cycle.

Set `GRAPHITE_PROJECTS_ROOT` to change the default base folder used by `daemon`, `daemon-status`, `daemon-health`, the platform daemon installers, and init/bootstrap daemon-visibility checks (defaults to the current directory when unset).

After upgrading graphite itself, restart the daemon — stop it, install, then start — because a long-running daemon keeps executing the code it loaded at start. A daemon started under the new code detects the engine change itself and rebuilds every supervised graph, clearing `engine_changed` staleness across all managed projects in one pass.

## Usage

Reference pages, each kept in lockstep with the code by a test:
[CLI](docs/reference/cli.md) (generated from the parser),
[configuration](docs/reference/configuration.md) (every `GRAPHITE_*` variable),
[exit codes](docs/reference/exit-codes.md),
[compatibility and support](docs/compatibility.md) (what 1.x promises), and
[benchmarks](docs/benchmarks.md) (what `supported_repo_files` rests on).

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
# Responses carry schema_version plus a uniform `resolution` list (how each
# input resolved: exact-id | name | path-suffix | fuzzy, with alternates when
# ambiguous); the per-verb `match` metadata remains. Not-found errors include
# a `candidates` list of close matches.
# Traversal is bounded with generous defaults (path/reaches max_depth 32;
# neighbor listings max_results 200) — results report truncated + limits, and
# a no_path with truncated:true means the bound was hit, not proven absence.
graphite query "depends-on src/lib/db.ts"
graphite query "callers calculateCommissionPence"

# Every query is executed through a canonical, inference-free plan (schema v1).
# --show-plan includes the plan in the result; --plan-only validates and prints
# the plan without loading the graph (offline syntax check for agents).
graphite query "reaches handler -> db.write" --show-plan
graphite query "callers acceptPairing" --plan-only

# Natural-language questions via a FIXED deterministic grammar (no LLM, no
# network): recognized questions translate to a plan and execute (the matched
# pattern and plan are included); impact/context/tests questions return the
# canonical command to run; anything else falls back to ranked search as
# clarification candidates. The full grammar is listed by capabilities.
graphite query --natural "who calls acceptPairing?"
graphite query --natural "what breaks if I change db.ts"
graphite query --natural "who calls acceptPairing" --plan-only

# Deterministic ranked node search (symbol, path, or concept) and
# machine-readable capability discovery for agents (verbs, target roles,
# limits, plan version, natural-language grammar)
graphite search "acceptPairing"
graphite capabilities --json

# Integration contract for agents: docs/agent-integration.md walks the
# discover -> search -> query -> validate workflow; docs/schemas/*.json
# publishes the plan/result/search/capabilities JSON schemas (kept in
# lockstep with live outputs by compatibility tests).

# Check whether graph-out is current (names the reason when stale:
# engine_changed vs source changes; --ignore-engine reports source drift only)
graphite check .
graphite check . --ignore-engine

# Suggest files and tests affected by a change
graphite impact src/lib/db.ts

# Compact agent-ready context for a file or node
graphite context src/lib/db.ts

# Initialize shared Graphite instructions for AI coding platforms
graphite init C:/Projects/MyApp
graphite init . --platform codex --platform claude
graphite init . --all

# Make a project Graphite-ready
graphite bootstrap C:/Projects/MyApp

# Check daemon health
graphite daemon-health C:/Projects

# Audit whether Graphite can replace legacy graph tooling in a project
graphite audit-replacement C:/Projects/MyApp
```

## Legacy replacement audit

Use the replacement audit before removing a legacy graph tool's files or ignore entries:

```bash
graphite audit-replacement C:/Projects/MyApp
graphite audit-replacement . --json
graphite audit-replacement . --fail-on-blocker
```

The audit checks Graphite bootstrap state, graph freshness and validity, daemon visibility, daemon health, physical legacy-tool remnants, and legacy text/config references. It reports recommendations but never deletes files automatically.

## Daemon health

Use daemon health for operational checks and automation:

```bash
graphite daemon-health C:/Projects
graphite daemon-health C:/Projects --json
graphite daemon-health C:/Projects --fail-on-error
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
graphite bootstrap C:/Projects/MyApp
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
- Polls locally and rebuilds canonical graphs without model inference.
- Debounces file changes before rebuilding, so save bursts do not cause repeated builds.
- Uses content hashes, not timestamps, to avoid unnecessary rebuilds.
- With `--impact`, prints impacted files and likely tests from the previous graph before rebuilding.
- Ignores ambient provider configuration. Legacy non-`none` `--llm` and provider flags are rejected.

Useful controls:

```bash
graphite watch . --impact --interval 2 --debounce 1
graphite watch . --once --interval 0.1 --debounce 0
graphite watch . --no-initial-build
```

## Multi-project daemon

Use the daemon when you want Graphite to keep every discovered project under `C:\Projects` fresh without manually starting a watcher in each repo:

```bash
# One-shot health/build pass
graphite daemon C:\Projects --once

# Persistent local supervisor
graphite daemon C:\Projects

# Read latest health/status
graphite daemon-status C:\Projects
```

Daemon behavior:

- Discovers project roots by markers such as `.git`, `package.json`, `pyproject.toml`, `wrangler.toml`, `go.mod`, and `Cargo.toml`.
- Skips heavy/tool folders such as `node_modules`, `.git`, `graph-out`, `.cache`, `dist`, `build`, and `_tools`.
- Writes local operational state to `<base>/.graphite-daemon/status.json` and JSONL logs to `<base>/.graphite-daemon/graphite-daemon.log`.
- Limits work with `--max-projects`, `--max-depth`, `--max-files-per-project`, `--max-builds-per-cycle`, and `--build-timeout`.
- Runs child builds with isolated stdin and zero-LLM mode unless LLM flags/environment variables are explicitly enabled.

Useful controls:

```bash
graphite daemon C:\Projects --scan-interval 10 --discover-interval 60
graphite daemon C:\Projects --max-builds-per-cycle 1 --build-timeout 180
graphite daemon C:\Projects --no-initial-build
```

Windows startup integration:

```bash
# Install as a current-user logon task and start immediately
graphite daemon-install-windows C:\Projects --start-now

# Inspect the scheduled task
graphite daemon-task-status

# Remove the scheduled task
graphite daemon-uninstall-windows
```

The installed task is named `GraphiteDaemon-FProjects` by default and uses the same bounded zero-LLM daemon defaults.

If Task Scheduler creation is blocked by Windows policy, install the non-admin Startup-folder fallback:

```bash
graphite daemon-install-startup-windows C:\Projects
graphite daemon-startup-status C:\Projects
graphite daemon-uninstall-startup-windows C:\Projects
```

The fallback writes a hidden VBS launcher in the current user's Startup folder and an idempotent PowerShell launcher in `C:\Projects\.graphite-daemon`.

Linux and macOS supervision, same daemon, same bounded defaults:

```bash
# Linux: a systemd USER unit (~/.config/systemd/user/graphite-daemon.service),
# reloaded, enabled and started; no privilege needed
graphite daemon-install-linux ~/Projects
graphite daemon-service-status
graphite daemon-uninstall-linux

# macOS: a launchd agent (~/Library/LaunchAgents/com.graphite.daemon.plist),
# bootstrapped into the user's GUI domain; logs under ~/Library/Logs/graphite
graphite daemon-install-macos ~/Projects
graphite daemon-service-status
graphite daemon-uninstall-macos
```

`daemon-service-status` answers for whichever supervisor the platform uses (on Windows it reports the scheduled task and the startup launcher), and `daemon-health` reports the same fact as `startup installed`. Every generated launcher runs the interpreter with `-P` -- the systemd unit and the plist are built from the same argument vector as the Windows task. A systemd user unit runs only while you have a login session unless you enable lingering (`loginctl enable-linger $USER`); that is a policy choice graphite does not make for you.

## Canonical graph and enrichment isolation

`scan`, `build`, `report`, `check`, `validate`, `query`, `context`, `impact`, `watch`, and `daemon` are canonical operations. They force an internal no-inference configuration, ignore ambient `GRAPHITE_LLM*` values, exclude provider data from graph artifacts, and reject legacy non-`none` `--llm` or provider flags. `--llm none` remains a temporary compatibility no-op.

Model enrichment uses the explicit `graphite overlay build` boundary. The command requires an existing fresh canonical graph plus exact current provider-lifecycle and model identity SHA-256 digests. OpenRouter additionally requires its routing-policy digest. Only lifecycle-governed Ollama and OpenRouter overlays are accepted; Ollama is restricted to loopback HTTP and OpenRouter to its canonical HTTPS API root.

Global provider options precede the subcommand. These examples deliberately omit credentials; provide an OpenRouter credential only through an approved session-scoped secret environment, never argv or a repository file:

```powershell
graphite --llm local --llm-provider ollama --llm-model qwen2.5-coder:7b overlay build . `
  --provider-identity-digest <64-lowercase-hex-lifecycle-digest> `
  --model-identity-digest <64-lowercase-hex-model-digest>

graphite --llm cloud --llm-provider openrouter --llm-model <exact-provider-model-id> overlay build . `
  --provider-identity-digest <64-lowercase-hex-lifecycle-digest> `
  --model-identity-digest <64-lowercase-hex-model-digest> `
  --routing-policy-digest <64-lowercase-hex-routing-policy-digest>
```

The overlay manifest binds the canonical bundle fingerprint, lifecycle/model/routing identities, input/output/time limits, creation time, outcome, and schema version. Successful payloads are content-addressed and the manifest is replaced last, so interruption cannot replace the last valid overlay with a partial result. A failed call writes only a separate allowlisted failure category; raw diagnostics, prompts, credentials, endpoints, and paths are excluded.

Overlay files are non-authoritative, independently stale, and stored only beneath `graph-out/overlays/<provider>/<identity-digest>/`. Identity-derived paths reject traversal, symlinks, reparse points, collisions, and output-root escape. Restrictive file permissions are applied. Changing the canonical graph or provider/model/routing identity makes the overlay stale without changing canonical freshness or exit status. `query`, `context`, `impact`, validation, routing, watch, and daemon do not read overlays. Deleting the overlay tree removes annotations without changing canonical artifacts.

## Adaptive development routing

Graphite's governed development router invokes only locally installed Claude Code
and Codex CLIs that are already authenticated through a Claude subscription or a
ChatGPT subscription. It does not accept or use Anthropic/OpenAI API keys. Ollama is
not a development-routing provider. Future Ollama/OpenAI-compatible enrichment is
restricted to the separate overlay boundary, and OpenRouter remains separate from
governed development routing.

Authenticated Claude Code and Codex subscription CLIs are the only governed
development execution providers.

Before routing, install the vendor CLIs through their official distribution paths,
authenticate them interactively, and verify the exact subscription identity:

```powershell
claude --version
claude auth status --json
codex --version
codex login status
```

Claude must report `claude.ai` first-party authentication; Codex must report
`Logged in using ChatGPT`. Graphite hashes the resolved executable and binds its
version, adapter protocol, requested model, effective model, effort, permission
mode, risk ceiling, verification time, and expiry into a capability snapshot.
The no-edit verifier must report input and output usage. Graphite validates both
against the exact approved reservation before saving the snapshot; missing,
invalid, or over-budget usage fails closed and creates no active authority.
Claude profile verification additionally requires one schema-constrained turn and
an exact terminal `structured_output` object; free-text output is never verification
authority. Ordinary task execution remains outside this verification-only schema.
Profile evidence is explicit and short-lived. A CLI update, executable replacement,
authentication change, effective-model mismatch, or expired snapshot fails closed.
Capability evidence helps establish eligibility; it is not authorization authority.

Provider lifecycle state is stored separately from canonical graph artifacts. The
states are `discovered`, `compatible`, `verification_required`, `active`,
`incompatible`, and `unavailable`. A changed executable hash or patch version gets
a bounded standard probe; a minor version or capability change gets an expanded
probe; a major version leaves the provider `incompatible` until a new compatibility
policy is separately approved. Passing a probe moves an identity only to
`verification_required`, never directly to `active`.

The daemon may observe and persist sanitized lifecycle transitions, but it cannot
activate a provider or add provider facts to the canonical graph. Immediately
before approval consumption, the lazy execution check re-observes the exact runtime
identity and is authoritative even when daemon state is stopped or stale. Failure
or corruption in one provider lifecycle boundary fails that provider closed without
blocking canonical scan, build, check, query, watch, daemon builds, or another
independent provider boundary.

Lifecycle operator commands open the existing lifecycle database read-only, enforce
pages of 1–100 records, and emit the same bounded public fields in compact JSON or
indented human-readable form. They never create missing state or expose executable
paths, endpoint query strings, credentials, prompts, or raw diagnostics:

```powershell
graphite lifecycle list . --limit 50 --json
graphite lifecycle status . --boundary-digest <64-lowercase-hex> --json
graphite lifecycle history . --boundary-digest <64-lowercase-hex> --limit 50
graphite lifecycle policy inspect . --boundary-digest <64-lowercase-hex> --json
```

`lifecycle policy prepare` creates a content-hashed policy candidate only for the
exact current incompatible identity. It does not persist, promote, or activate the
candidate; promotion requires a separate human-authorized operation. `graphite lifecycle verification prepare`
similarly creates the complete manifest for one exact
`verification_required` identity and stops before inference. The manifest fixes the
model, effort, token/time/cost bounds, fixture commit, graph and response-contract
hashes, one attempt, no fallback, no resume, and no substitution. Display and review
of either candidate grant no execution authority.

```powershell
graphite route recommend . --objective "Review listing search" --target src/search.py
graphite route run . --objective "Review listing search" --target src/search.py
graphite route review . --task-id task-identifier
graphite route accept . --task-id task-identifier
graphite route reject . --task-id task-identifier
graphite route cleanup . --task-id task-identifier
graphite route status . --json
graphite route policy . --json
```

`route recommend` is offline and read-only. It requires a fresh validated graph and
a current verified capability snapshot. `route run` creates a detached worktree at
the approved commit, prints the exact provider/model/effort/permission manifest, and
then asks for consent. Approval defaults to No. Non-TTY input/output, JSON mode, CI,
and `--yes` cannot grant consent. Approval is signed, short-lived, single-use,
snapshot-bound, prompt-hash-bound, commit-bound, and token-bound. It is consumed
immediately before exactly one provider process.

The provider may edit only the isolated worktree under the selected permission
mode. Graphite rejects symlinks/reparse points, nested repositories, submodule
changes, case collisions, out-of-scope files, excessive file/byte counts, identity
drift, and diff drift. It runs bounded, credential-free validation and records a
content hash—not diff contents. Provider output remains untrusted and is never
validation or merge authority.

High-risk work requires a second, separately approved, read-only review by the
other provider. The reviewer receives an ephemeral synthetic diff and cannot edit.
`route accept` rechecks the diff and validation evidence, then creates a detached,
cherry-pickable commit; it never merges the source branch. `route reject` records the
human verdict. `route cleanup` is a separate destructive authority step.

There is no automatic retry, arbitrary provider/model switch, session reuse,
acceptance, cleanup, cherry-pick, or merge. The sole automatic fallback is a bounded
one-step advance to the other provider when both exact candidates were selected and
approved in the same immutable route pool and the first returns the allowlisted
`capacity_unavailable` category before producing output or side effects. Every other
failure remains failed and requires a new approval flow. Legacy Ollama executions
are retained as read-only history and cannot be replayed as Claude or Codex attempts.

Telemetry is append-only and restricted to provider/profile identity, category and
risk, latency, reported token usage, diff size, validation outcome, defect classes,
rework count, human verdict, and provenance. Source, prompts, responses, diff
contents, paths, secrets, and raw diagnostics have no telemetry field. Subscription
cost is `unknown`, never zero. Learning can create a signed candidate and comparison
evidence, but cannot change the provider allowlist, permission ceiling, risk
ceilings, or autonomy. Promotion and rollback both require interactive human
approval and never delete evidence.

### Schema-v4 to schema-v5 migration and rollback

Stop all Graphite routing writers before upgrade or rollback. On the first v5 open,
Graphite creates `backups/events-schema-v4.sqlite3` and
`backups/events-schema-v4.sha256.json`, verifies the backup is schema v4 and passes
SQLite integrity and foreign-key checks, then performs the v5 lifecycle-binding
migration. Historical v4 rows remain readable but do not acquire invented lifecycle
authority. After migration, run `graphite route status . --json`, SQLite
`PRAGMA integrity_check`, and `PRAGMA foreign_key_check`, then preserve both backup
files.

Rollback is a database restore, not an in-place downgrade:

1. Stop every process that can write `.graphite/routing/events.sqlite3`.
2. Verify the backup SHA-256 against `backups/events-schema-v4.sha256.json` and run
   SQLite `PRAGMA integrity_check` and `PRAGMA foreign_key_check` against the backup.
3. Preserve the current v5 database for incident analysis, then atomically restore
   the verified v4 backup as `events.sqlite3`.
4. Restore the matching v4 application build and confirm the schema version and
   historical row counts with its read-only status path before allowing writers.

If the v5 database is partially migrated, the backup marker is absent/mismatched,
or integrity fails, keep routing stopped. Restore the verified backup or deploy a
tested forward fix; do not hand-edit schema metadata or delete evidence.

Incident response follows the same containment rule: stop routing, preserve the
database and worktree evidence, revoke an affected subscription session when
credential exposure is suspected, and resume only after explicit review.

Provider environment variables are reserved for explicit doctor probes and the
overlay boundary. Canonical commands do not read them:

- `GRAPHITE_LLM`: `none`, `auto`, `local`, or `cloud`.
- `GRAPHITE_LLM_PROVIDER`: `ollama`, `openai-compatible`, `openai`, `openrouter`, `groq`, `lmstudio`, or `vllm`.
- `GRAPHITE_LLM_MODEL`: model name.
- `GRAPHITE_LLM_BASE_URL`: provider base URL.
- `GRAPHITE_LLM_API_KEY`: provider API key; do not commit this.
- `GRAPHITE_LLM_TIMEOUT`: request timeout seconds.
- `GRAPHITE_LLM_MAX_INPUT_CHARS`: prompt input budget.
- `GRAPHITE_LLM_MAX_OUTPUT_TOKENS`: overlay output-token budget, clamped to 1–4096.

These settings never appear in canonical manifests or reports.

## Output

Artifacts are written to `graph-out/`:

- `graph.json` — bundled graph for external tools
- `GRAPH_REPORT.md` — human-readable audit
- `graph.html` — interactive viewer
- `.graphite_*.json` — intermediate pipeline artifacts

## Claude Code skill

A skill template lives at `skill/SKILL.md`. To install, from a clone of this
repository:

```bash
mkdir -p ~/.claude/skills/graphite
cp skill/SKILL.md ~/.claude/skills/graphite/SKILL.md
```

Then use `/graphite [path]` inside Claude Code. The skill defaults to zero-LLM mode.

### MCP server for Claude Code

Complete the mandatory package-validation policy and MCP activation steps in [System readiness and optional integrations](#system-readiness-and-optional-integrations). Do not bypass or reorder the validator and install steps.

`graphite init` writes a project-local `.mcp.json` entry for Claude Code and
Codex (and rewrites its own entry on every run, preserving foreign ones). To
configure a client by hand, launch the server with `-P` — or use the
`graphite-mcp` console script, which needs no flag:

```json
{
  "mcpServers": {
    "graphite": {
      "command": "python",
      "args": ["-P", "-m", "graphite.mcp"],
      "cwd": "C:/Projects/YourProject"
    }
  }
}
```

Once configured, Claude can call these tools automatically:

- `graphite_query` — e.g. `depends-on db.ts`, `imported-by db.ts`, `path article-gen/route.ts -> db.ts`, `stats`
- `graphite_community` — list the community around a node
- `graphite_summary` — stats, god nodes, entry points, surprising connections
- `graphite_refresh` — rebuild and reload the graph
