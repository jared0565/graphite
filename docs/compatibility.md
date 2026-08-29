# Compatibility and support

From 1.0.0 graphite follows semantic versioning for the surfaces listed here.
A breaking change to any of them is a major release; additions are minors;
fixes are patches. `CHANGELOG.md` names every change to a stable surface.

## Stable surfaces

- **CLI.** Every subcommand and option in `docs/reference/cli.md`. Options and
  subcommands may be added in a minor; removing or renaming one, changing a
  default, or changing what an option means is a major.
- **JSON outputs that carry `schema_version`**: `query`, `search`,
  `capabilities`, `doctor --json`, `check --json`, `validate --json`,
  `daemon-health --json`, `review-changes`. Fields are added in minors. A
  field is removed, renamed or changes meaning only with a `schema_version`
  bump, which is a major. The published schemas live in `docs/schemas/`.
- **`graph-out/graph.json`** at `schema_version` 1: node and edge shape,
  `metadata.engine.fingerprint`, and the `resolution_health` block at
  schema 3. Node ids may change between releases when the engine changes —
  `RELEASING.md` measures id survival for every release and the CHANGELOG
  says when it is not 100 %.
- **Configuration.** Every environment variable and `Config` field in
  `docs/reference/configuration.md`, including defaults.
- **Exit codes** in `docs/reference/exit-codes.md`.
- **The launch and hook contract.** `graphite agent-hook` always exits 0;
  the `.githooks/` trampolines and every generated launcher run the
  interpreter as `python -P -m graphite …`; `init` rewrites only the
  graphite-managed sections of `GRAPHITE.md`, `AGENTS.md`, `CLAUDE.md`,
  `.mcp.json` and `.vscode/tasks.json` and preserves foreign content.
- **MCP tool names** served by `graphite-mcp`.
- **The agent channel protocol** as described by `graphite channel` and the
  channel's `PROTOCOL.md`.

## Not stable

Internal modules under `graphite.*` (import them at your own risk),
everything under `graph-out/` other than `graph.json`, the extraction cache
layout (`cache_version`), routing and overlay storage layouts,
`GRAPH_REPORT.md` prose, `graph.html`, and the wording of human-readable
(non-`--json`) output.

## Deprecation policy

A deprecated stable surface keeps working for at least **one minor release**
after the deprecation is announced in `CHANGELOG.md`, prints a warning that
names its replacement, and is removed only in the next major.

## Support matrix

Exactly the CI matrix in `.github/workflows/ci.yml`: **Windows, Linux
(Ubuntu) and macOS × CPython 3.11, 3.12, 3.13 and 3.14**. A platform is
supported when every one of its cells gates merges; nothing else counts as
support. Daemon supervision is installable on all three
(`daemon-install-windows`, `daemon-install-linux`, `daemon-install-macos`).

Repository size: `capabilities` declares `supported_repo_files` with the
measurement backing it in `docs/benchmarks.md`.

## Security fixes

Ship in the next patch release of the current 1.x minor; see `SECURITY.md`.
