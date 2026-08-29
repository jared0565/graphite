# Graphite user guide

Graphite builds a deterministic, local knowledge graph of a codebase — files,
symbols, imports, calls, containment, communities — with no model, no
network and no credentials, and answers structural questions from it: who
calls this, what imports that, what breaks if this file changes. Coding
agents and scripts read the same graph through the CLI, an MCP server, and
the instruction files `graphite init` writes into a repository.

This guide is task-oriented. The reference pages are exact and kept in step
with the code by tests: [CLI](reference/cli.md),
[configuration](reference/configuration.md),
[exit codes](reference/exit-codes.md), [compatibility](compatibility.md),
[benchmarks](benchmarks.md). Operational symptoms and their causes are in the
[knowledge base](knowledge-base.md).

## 1. Install

```bash
python -m pip install --user graphite-code
graphite --version
```

- The distribution is **`graphite-code`** (PyPI's `graphite` is an unrelated
  project). The import package is `graphite`; the console scripts are
  `graphite` and `graphite-mcp`.
- Python 3.11–3.14 on Windows, Linux and macOS. Every one of those twelve
  combinations gates the test suite in CI.
- `graphite --version` prints four lines: the version, the **engine
  fingerprint** (a digest of the extraction engine *as installed on this
  machine*, including the parser packages), the cache version and the engine
  schema. Quote all four when reporting a problem; the version alone does not
  identify what you are running.
- The MCP server is an extra: `python -m pip install --user "graphite-code[mcp]"`.
  Read "System readiness and optional integrations" in the README first — the
  package-validation policy applies before that install.

**Launching from a repository.** Use the console script, or
`python -P -m graphite …`. The `-P` is not optional: without it Python puts
the current directory first on `sys.path`, so a `graphite.py` or a
`graphite/` package planted at a repository root would be imported in place
of the installed one. Everything graphite generates — hooks, `.mcp.json`
entries, editor tasks, daemon launchers — already carries `-P`. The console
scripts are immune by construction.

## 2. Build the first graph

```bash
cd /path/to/repo
graphite build .          # graph-out/graph.json, graph.html, GRAPH_REPORT.md
graphite check .          # is graph-out current? names the reason when not
graphite validate         # bundle integrity before CI or agents rely on it
graphite report .         # rebuild + report in one step
```

`build` scans the repository (respecting containment: nothing above the root,
no symlink escapes, size and count limits from
[configuration](reference/configuration.md)), extracts with Tree-sitter for
TypeScript/JavaScript, Python, Go and Rust, resolves cross-file identities,
builds and analyses the graph, validates it, and writes `graph-out/`. The
output directory is gitignored by `init`/`bootstrap`. Only changed files are
re-parsed on later builds: the extraction cache under `.cache/graphite` is
content-addressed and partitioned by engine identity, so upgrading graphite
re-extracts once and never serves a graph built by an older engine.

`check` exits non-zero when the graph is stale and says why: source changes
(files added, changed, removed) or `engine_changed` (graphite itself was
upgraded). `--ignore-engine` reports source drift only.

## 3. Ask questions

```bash
graphite capabilities --json           # the contract: verbs, limits, grammar
graphite search "TenantStore"          # ranked node search: symbol, path, concept
graphite query "callers save_tenant"   # who calls it
graphite query "calls save_tenant"     # what it calls
graphite query "imported-by src/db.py"
graphite query "depends-on src/db.py"
graphite query "path src/api/route.ts -> src/db.ts"
graphite query "reaches handle_request -> write_audit"
graphite query "community-of src/db.py"
graphite query "stats"
graphite context src/db.py             # dependencies, dependents, tests, peers
graphite impact src/db.py              # what to re-test after a change
```

Three habits make the answers trustworthy:

1. **Discover, don't guess.** `capabilities --json` lists every verb with its
   aliases, target roles and limits, and the complete natural-language
   grammar (`query --natural "who calls X"`). Query plans are canonical and
   inference-free; `--show-plan` includes the plan, `--plan-only` validates
   one offline.
2. **Read the grade.** Every query result carries an `answer` block scoped
   to the relations and languages *that answer used*:
   - `decision_grade` — every cell it relied on is well resolved. Act on it;
     an empty result is a trustworthy absence.
   - `advisory` — usable, but some cell is degraded; verify with a text
     search and say so.
   - `inconclusive` — degraded *and* empty. Unknown, not "none".
   The aggregate `resolution_health.healthy` can be true while the language
   you asked about is degraded, so gate on `answer.grade`, never on the
   aggregate.
3. **Use the candidates.** A not-found error includes `candidates` — close
   matches by id, name or path suffix. Retry with one of them rather than
   inventing an identifier.

The [agent integration guide](agent-integration.md) walks the
discover → search → query → validate workflow and explains resolution
health and incidents in depth.

## 4. Onboard a repository for coding agents

```bash
graphite init /path/to/repo                      # interactive platform choice
graphite init . --platform codex --platform claude
graphite init . --all
graphite init --list-platforms
graphite bootstrap /path/to/repo                 # minimal: gitignore + AGENTS.md + build
```

`init` writes `GRAPHITE.md` (the graph-first workflow every agent follows),
the per-platform instruction files (`AGENTS.md`, `CLAUDE.md`,
`ANTIGRAVITY.md`, `.github/copilot-instructions.md`,
`.cursor/rules/graphite.mdc`, `.windsurfrules` as selected), gitignore
entries, an `.mcp.json` entry, editor tasks, git hook trampolines and agent
hooks that keep the graph fresh on commit and on agent turns — all launching
graphite under `-P` — then builds and validates the graph. It is idempotent:
rerun it after upgrading graphite to pick up newer templates. Files it
manages carry a version marker; edit them through `init`, not by hand, or
use `--adopt` to bring hand-written legacy instruction files under
management by appending rather than overwriting.

Two things worth knowing:

- Exclude a directory and its subtree from supervision (a vendored SDK, a
  nested checkout you do not want built) with an empty `.graphite-ignore`
  file inside it.
- `graphite doctor .` reports the repository's readiness; `--deep` exercises
  the pipeline in a private temporary workspace without writing to the
  repository. Foreign hooks (ones graphite did not write) are reported,
  never rewritten.

## 5. Keep graphs fresh

During active work in one repository:

```bash
graphite watch . --impact          # polls, debounces, rebuilds on content change
```

Across every repository under a projects root, run the daemon. It discovers
project roots by markers (`.git`, `package.json`, `pyproject.toml`,
`wrangler.toml`, `go.mod`, `Cargo.toml`), skips `node_modules`, `graph-out`,
`.cache`, `dist`, `build` and the like, and rebuilds only what changed:

```bash
graphite daemon ~/Projects --once      # one health/build pass
graphite daemon ~/Projects             # persistent, bounded, zero-LLM
graphite daemon-status ~/Projects      # latest status.json
graphite daemon-health ~/Projects      # freshness, process, launcher, failing projects
```

State lives under `<base>/.graphite-daemon/`: `status.json` (atomic) and a
JSONL `graphite-daemon.log` whose events (`project_activated`,
`build_started`, `build_succeeded`, `build_failed`, `build_skipped_locked`)
are the ground truth when `daemon-status` looks fine but a graph is stale.
Set `GRAPHITE_PROJECTS_ROOT` once to make `~/Projects` the default base for
all daemon commands.

Install it as a supervised service so it starts with your session — same
daemon, same bounded defaults, one command per platform:

```bash
# Windows: a current-user scheduled task (or the Startup-folder fallback when
# Task Scheduler is blocked by policy)
graphite daemon-install-windows C:\Projects --start-now
graphite daemon-install-startup-windows C:\Projects

# Linux: a systemd USER unit, reloaded, enabled and started; no privilege
graphite daemon-install-linux ~/Projects

# macOS: a launchd agent in ~/Library/LaunchAgents, logs under ~/Library/Logs/graphite
graphite daemon-install-macos ~/Projects

graphite daemon-service-status          # whichever supervisor this platform uses
graphite daemon-uninstall-linux         # likewise -windows / -macos
```

A systemd user unit runs only while you have a login session unless you
enable lingering (`loginctl enable-linger $USER`); graphite does not make
that policy choice for you. `daemon-health` reports `startup installed` for
the platform's supervisor.

## 6. Limits and scale

`graphite capabilities --json` declares the limits a build honours:
`max_graph_bytes` (128 MiB, the binding one) and `supported_repo_files`
(**7 000** files), with the basis printed beside it. The number rests on the
densest real repository measured — Django 5.2, 2 930 sources, builds in
about a minute to a 53 MB graph — and on a synthetic corpus that reaches the
cap near 18 000 files; code density, not file count, decides where a given
repository lands. [benchmarks.md](benchmarks.md) has the table and the
command that produced it (`benchmarks/build_benchmark.py`), and CI runs the
synthetic benchmark on every push. Above the declared size, split the build
(`.graphite-ignore` on subtrees) or raise the byte cap in configuration and
measure.

Dense import cycles are bounded, not fatal: the cycle report enumerates up to
length 8 under a 10 000-cycle budget and `analysis.cycle_search` says whether
that enumeration was exact.

## 7. Upgrade and verify a release

```bash
python -m pip install --user --upgrade graphite-code
graphite --version                     # new version, new engine fingerprint
```

Then **restart the daemon — stop, install, start** — on every machine that
runs one. A long-running daemon keeps executing the code it loaded at start.
A daemon started under the new code detects the engine change itself and
rebuilds each supervised graph once; `check` reports `engine_changed` until
that happens. Re-run `graphite init` in repositories whose instruction files
should pick up newer templates.

Every release is built by CI from its tag, its digests are pinned in the
publishing workflow before the workflow can run, and the files on PyPI carry
PEP 740 attestations naming the workflow that built them. To verify a release
you are about to trust, from a clone of the repository:

```bash
python scripts/verify_published_release.py 1.0.0 --wheel-sha256 <digest from the GitHub Release>
```

It verifies the release from the index — among its six arms, the served
wheel's digest against the one you pass and the presence of provenance —
and prints `OVERALL: PASS` only when every arm passes. Without the script:
`pip download graphite-code==<version> --no-deps`, compare `sha256sum` with
the digest on the GitHub Release, and open
`https://pypi.org/integrity/graphite-code/<version>/<filename>/provenance`,
which returns the attestation bundle (HTTP 200) for an attested file.

## 8. Configuration, exit codes, support

- Every `GRAPHITE_*` variable, with its default, is in
  [reference/configuration.md](reference/configuration.md). Canonical
  commands ignore `GRAPHITE_LLM*` entirely; model enrichment is a separate,
  explicit overlay (`graphite overlay build`) and never changes the graph.
- Exit codes per command are in [reference/exit-codes.md](reference/exit-codes.md).
- What 1.x promises, and the one-minor-release deprecation window, is in
  [compatibility.md](compatibility.md).
- Bugs and feature requests: the issue templates on GitHub. Security
  reports: the private path in [SECURITY.md](../SECURITY.md), never a public
  issue. Include `graphite --version` output and the platform.
