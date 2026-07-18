# Graphite architecture

This guide describes the current internal architecture and the constraints contributors must preserve. Graphite targets Python 3.11+, runs locally, and does not require a model provider. Its core graph facts and ordering are reproducible when the Graphite version, Tree-sitter/parser packages, Node/TypeScript toolchain, configuration, and resolver mode/outcome are fixed; optional tool availability or version differences can change extraction or resolution across hosts. Repository data, external processes, generated artifacts, and model input/output are trust boundaries rather than trusted implementation details.

## System context

Graphite is a local Python application. The public CLI is `graphite`, or equivalently `python -m graphite`, with command handling in `src/graphite/cli.py`. The optional `graphite-mcp` entry point in `src/graphite/mcp_server.py` exposes selected operations through MCP. Both are adapters over the same core modules; they are not alternative graph implementations.

The default build is local-first and zero-LLM. It scans a repository, extracts structural facts, constructs and analyzes a directed graph, validates the public bundle, and writes local artifacts. Model enrichment in `src/graphite/llm.py` is a post-analysis option. Its default mode is `none`, and core graph construction has no network requirement.

## Processing pipeline

```text
repository input
    -> collect and classify files
    -> parse and extract symbols/imports/calls
    -> resolve cross-file identities
    -> construct and analyze the directed graph
    -> optionally enrich a report through a configured model adapter
    -> validate the bundle and render artifacts
```

The implementation stages are:

1. **Collection — `ingest.py`, `config.py`, `git.py`.** Configuration establishes file-count and file-size limits and the cache and output locations. Ingestion normalizes project-relative paths, resolves candidates beneath the repository root, skips unsafe or ineligible files, and sorts accepted entries. Git-backed enumeration uses `GitRunner` with a trusted executable outside the repository, an argument vector, no shell, a filtered Git environment, a timeout, and a stdout cap. The filesystem fallback is also capped by configured file limits, but it does not provide every process control because no child process is involved.
2. **Extraction — `extract/ast.py`, `cache.py`, `ts_bridge.py`, `ts_resolver.mjs`.** Per-language Tree-sitter extractors handle JavaScript/TypeScript, Python, Go, and Rust, while unsupported or unavailable parsers degrade to a file-level record. Extraction emits symbols, imports, calls, and containment edges in deterministic order. The content-addressed cache avoids repeated AST work. TypeScript/JavaScript may additionally use the Node compiler bridge for compiler-backed module resolution; bridge failure falls back to heuristic resolution rather than making Node mandatory.
3. **Resolution — `resolve.py`.** `SourceIndex` builds the set of normalized, project-relative source identities, reads TypeScript aliases and workspace package entry points, prefers compiler-backed TypeScript results when available, and otherwise applies bounded heuristics. Global merge logic also resolves supported call identities and deterministically removes duplicates.
4. **Graph and analysis — `graph.py`, `cluster.py`, `analyze.py`, `query.py`.** `graph.py` constructs a NetworkX `DiGraph` after sorting nodes and edges and normalizing IDs. Analysis filters to project nodes where appropriate and returns bounded, ordered results. Community detection constructs a deterministic undirected view and uses the configured seed (42 by default). JSON conversion preserves the graph's deterministic insertion order.
5. **Optional enrichment — `llm.py`.** Enrichment runs after deterministic graph construction and analysis but before bundle validation and export. A configured adapter receives a size-capped graph summary, and its response is included as an annotation in the exported analysis; it does not change deterministic graph facts or gain authority to validate them. Provider reads have a hard 64 KiB response cap, redirects are disabled, and configured output tokens are normalized to 1–4096 with a default of 512. Errors are recorded in the analysis report and do not fail the deterministic build path.
6. **Validation and review — `validation.py`, `context.py`, `review.py`.** Bundle validation checks structural types, node and edge identity, referential integrity, metadata counts, and unsafe `source_file` paths. Review validates a supplied bundle before deriving graph impact evidence; its Git discovery also normalizes status records and rejects malformed or unsafe paths. By contrast, `cmd_query`, `cmd_impact`, `cmd_context`, and `GraphiteMCPServer._load` currently read JSON and call `graph_from_json` without `validate_graph_bundle` or a bounded-read helper. Those direct consumers therefore rely on callers to supply an already validated, reasonably sized artifact.
7. **Export — `export/json.py`, `export/md.py`, `export/html.py`, `io.py`.** Exporters consume the graph, clusters, analysis (including any enrichment result), and manifest. JSON serialization provides the data encoding; HTML separately JSON-encodes script data, escapes `<`, `>`, and `&`, HTML-escapes the title, and uses text DOM APIs for runtime labels. Text and JSON outputs use temporary files, `fsync`, and `os.replace` for atomic replacement of each file.
8. **Operations — `watch.py`, `daemon.py`, `daemon_health.py`, `bootstrap.py`, `init.py`, `windows_task.py`, `windows_startup.py`.** These modules watch for changes, maintain multi-project daemon status, evaluate health, generate integration instructions, and manage platform startup. They orchestrate the portable core rather than changing graph semantics.

## Module map

| Area | Primary modules | Responsibility | May depend on |
| --- | --- | --- | --- |
| Entry adapters | `cli.py`, `mcp.py`, `mcp_server.py`, `__main__.py` | Parse user/tool input, select operations, format responses | Core, validation, exporters, operations |
| Configuration and ingestion | `config.py`, `ingest.py`, `git.py` | Establish limits; discover, contain, classify, and hash repository files | Cache hashing and bounded process adapter |
| Extraction and resolution | `extract/ast.py`, `cache.py`, `resolve.py`, `ts_bridge.py`, `ts_resolver.mjs` | Parse language structures and resolve project identities | Ingestion contracts, configuration, optional Node/TypeScript process |
| Graph and analysis | `graph.py`, `cluster.py`, `analyze.py`, `query.py` | Build, cluster, analyze, and query the directed graph | NetworkX and deterministic extracted structures |
| Validation and evidence | `validation.py`, `context.py`, `review.py` | Validate bundles and derive review/context evidence | Graph structures; review may use the Git adapter |
| Export | `export/json.py`, `export/md.py`, `export/html.py`, `io.py` | Encode and atomically replace individual artifacts | Validated graph, analysis, clusters, manifest |
| Optional model enrichment | `llm.py` | Select an explicitly configured provider and append report enrichment from a bounded graph summary | Deterministic graph/analysis data, configuration, network only when enabled |
| Operations and integration | `watch.py`, `daemon.py`, `daemon_health.py`, `bootstrap.py`, `init.py`, `windows_task.py`, `windows_startup.py` | Freshness, daemon health, generated instructions, and OS startup | Public core operations and explicit platform/process boundaries |

The intended dependency direction is observable in current imports but is not enforced by a dedicated architecture linter. CLI and MCP adapt inputs; portable core modules do not import those UI entry points. Export consumes graph and analysis data; extraction does not depend on exporters. LLM enrichment consumes deterministic reports; graph and validation do not depend on a model provider. Windows startup and task management remain outside the portable core. External execution crosses explicit subprocess boundaries; Git uses the hardened `GitRunner`, while the TypeScript and Windows adapters have their own, not necessarily identical, controls. Contributors must preserve these directions and add tests if a boundary becomes mechanically important.

## Trust boundaries

### Doctor and deep-probe boundary

Fast checks are read-only with respect to the selected repository. They inspect the supported Python runtime, bounded Git enumeration, graph validity, daemon health, and static configuration or package availability. Missing MCP, TypeScript, daemon registration, or model configuration is an optional state; optional states do not block core graph operation. Only a `blocked` aggregate crosses the command's non-zero exit boundary.

The deep deterministic pipeline creates a synthetic repository in an external private temporary workspace and never writes to the selected root or loads or executes its code. On Windows, the normal lease parent and workspace directories are created with a protected, inheritable current-user DACL; the inheritable ACE applies to their children. This is a creation-time security-descriptor guarantee, not a claim that the DACL is re-read during each phase.

Canonical containment, pinned directory handles, reparse state, directory identity, and path bindings are validated before and after each synchronous phase. Subprocesses cross a separate hostile boundary with bounded stdin/stdout/stderr, one shared deadline, disabled shell execution, and native process containment: Windows Job Objects or POSIX process groups contain descendants. Cleanup coordination reserves part of the same deadline, revalidates the lease before deletion, and gives one bounded cleanup worker sole ownership. A cleanup timeout blocks the result and prevents another core probe in the same interpreter/process from racing the live lease until cleanup completes. The coordinator is process-local and does not claim cross-process exclusion.

These controls do not provide an OS sandbox. The local user and same-user process namespace remain a best effort boundary: another malicious process with the same authority may still interfere, so uncertain workspace bindings are leaked rather than deleted by pathname.

The MCP probe starts an isolated interpreter from a guarded distribution-record import manifest. It rejects current-directory, user-site, and attacker-controlled selected-root shadows and validates expected Graphite and MCP module origins before protocol startup. The exact origin-verified trusted Graphite source may be inside the selected repository, but it is accepted only when its expected lexical, canonical, filesystem-identity, and module-origin checks all match. MCP dependency roots, distribution metadata, package origins, and alternate Graphite origins that overlap the selected root remain rejected. The TypeScript probe is static no-exec detection: it reads package metadata through externally resolved Node but does not load, execute, or transpile project-controlled JavaScript, so detection stays optional/unverified.

The LLM path is an explicit subprocess and model trust boundary. `--include-llm` sends synthetic content only through an isolated bounded worker: one constant request, a hard 64 KiB HTTP response cap, redirects disabled, no retries, sanitized category-only failure, and no response body in the doctor report. Configured output-token bounds are normalized to 1–4096 with a default of 512, while the doctor probe forces 16. There is no repository data or model context carried between roots or tenants, preventing repository/model cross-contamination in this probe. Provider output cannot affect deterministic graph facts, validation, authorization, or core readiness.

The daemon-status path preserves bounded parse, schema, and output contracts. Input is size-limited before strict UTF-8 JSON parsing; health classification accepts only expected structures; doctor output exposes fixed booleans and counts rather than raw status, errors, process output, or paths.

## Consent-gated TypeScript activation boundary

`typescript_activation.py` owns evidence detection, prompt eligibility, lifecycle orchestration, revalidation, typed sanitized outcomes, and the process-local canonical-root lock. `dependency_install.py` owns manager adapters, exact argv, executable and validator provenance, registry/configuration isolation, source-policy checks, and post-install project-local detection. `cli.py` may call the activation entry point only from the single onboarding helper used by top-level `cmd_init` and `cmd_bootstrap`; build, report, check, doctor, daemon, watch, and MCP have no installation authority.

The lifecycle order is part of the public contract. Existing init/bootstrap writes complete first, activation runs second, and the requested build/validation stages then continue. `installed`, `already_available`, `not_applicable`, `declined`, and `guidance_only` preserve normal success semantics. `validation_failed`, `installation_failed`, and `verification_failed` are fatal activation outcomes: the overall command returns 1 while completed onboarding files and any reviewable package-manager changes remain preserved. There is no automatic rollback because Graphite cannot safely distinguish its writes from concurrent user or manager writes.

Automatic mutation is available only after bounded contained evidence, one unambiguous supported root lockfile, compatible `package.json#packageManager`, safe manifest/lock sources, absent manager override files, a supported external manager version, interactive default-No consent, and successful exact-package validation. The automatic adapters are npm 8–11, pnpm 11, and Bun 1. Yarn remains `guidance_only`. The validator must be an absolute regular file outside the selected root, runs through trusted external Node with only `typescript`, and is revalidated before use. The package manager is likewise external and revalidated immediately before launch. On POSIX, manager resolution permits only a bounded external symlink-launcher route while argv retains the lexical manager name required by Corepack-style dispatch. Immutable provenance covers every root-to-leaf directory/component binding used by the launcher and its symlink targets, including component identity, ownership, mode, and symlink target text, and the complete route is revalidated before version and install launches. A bounded prefix captured during the pinned-file read classifies the final target: exact `#!/usr/bin/env node`, `#!/usr/bin/node`, or `#!/bin/node` scripts execute only as canonical trusted-Node plus lexical-launcher argv, while recognized ELF/Mach-O managers such as Bun execute directly; ambiguous or unsupported interpreters fail closed to guidance. Both Node and the complete launcher route are revalidated immediately before version and install, and child `PATH` is never used to select Node. Root- or current-user-owned sticky directories such as the canonical temporary directory are allowed only when sticky semantics and trusted child ownership prevent cross-user replacement; group/world-writable non-sticky ancestors fail closed. Cycles, dangling or excessive routes, selected-root crossings, unsafe ownership, and component, chain, interpreter, or target replacement also fail closed. Directory-component symlinks such as `/tmp`-style canonical redirects use the same provenance policy rather than a pathname exception. Validator and control-file symlink rules remain strict, as does Windows executable resolution. A same-UID process may still race the final path-based OS launch after revalidation; that existing local-user trust limitation is not presented as cross-process isolation.

The automatic process boundary uses fixed argv with `shell=False`, closed stdin, lifecycle/build scripts disabled, a minimal allowlisted environment, isolated manager configuration/home, the canonical `https://registry.npmjs.org/`, stripped ambient registry tokens, bounded output, native descendant containment, and one shared deadline across validation, install, verification, and cleanup. Private registries and mirrors deliberately remain outside this unattended trust boundary and use the fixed manual workflow under operator policy.

Manifest and lockfile identities and hashes are captured before mutation and rechecked. TypeScript activation results expose only fixed outcomes, reason codes, manager names, and relative changed control-file paths. That activation result boundary includes neither credentials, raw process output, registry responses, absolute host paths, nor repository source; it is not a claim about every unrelated Graphite command or artifact. The lock excludes concurrent activation only within the current process; other editors and package managers remain possible failure domains.

On POSIX, descriptor-relative cleanup can safely empty an isolated directory but cannot atomically unlink its open root by descriptor. The cleanup worker may therefore intentionally retain an empty temporary root for operating-system temporary-directory reclamation rather than race a same-user pathname replacement. Operators may remove a confirmed stale empty root under their normal temporary-directory policy, but Graphite will not delete it by an unsafe inspect-then-remove sequence.

These controls provide prevention, containment, detection, and recoverable evidence; they do not provide an OS sandbox and do not make Graphite unhackable. The local user, same-user process namespace, manager implementation, canonical registry, trusted Node/validator, and operating system remain explicit trust boundaries.

**Repository input.**

Treat paths, bytes, symlinks, encodings, file counts, and file sizes as hostile. Current ingestion resolves roots and candidates, requires candidates to remain below the root, rejects unsafe Git paths, skips unreadable/binary/oversized files, applies configured count bounds, and records normalized project-relative paths. Extraction reads bytes and degrades or reports parse/read errors. New readers must use the same containment model; never trust a repository string as an absolute destination or executable.

**Process boundary.**

Git, Node/TypeScript, and Windows task/startup integrations execute outside Python. `GitRunner` is the strongest current adapter: fixed executable discovery outside the repository, argv execution with `shell=False`, disabled stdin/stderr, filtered `GIT_*` environment, timeout, stdout cap, and typed sanitized failures. The TypeScript bridge uses argv, `subprocess.run`, captured output, a configured timeout, and a 500-character diagnostic cap, but it currently inherits the environment and does not impose an explicit output-byte cap. Windows adapters are platform-specific and must be reviewed on their own controls. Do not generalize Git's isolation guarantees to every subprocess.

**Artifact and browser boundary.**

Repository-controlled names and strings remain untrusted when rendered. The normal build validates the public graph and rejects absolute or parent-traversing node/edge `source_file` values before export. The direct query, impact, context, and MCP load paths do not currently validate or bound the JSON read, which is a known hardening gap; do not treat their present behavior as a trust guarantee. JSON must be serialized, HTML script data must receive script-context escaping, visible browser values must use HTML escaping or text DOM APIs, and public artifacts must not introduce absolute system paths. `io.py` atomically replaces each completed file. The report set is written as several files rather than one filesystem transaction, so readers must validate the bundle and consult manifest/freshness state instead of assuming a directory snapshot is complete.

**Model and network boundary.**

Model use is disabled by default and requires explicit configuration; no vendor is required by the core. Current adapters use request timeouts, cap prompt characters, apply the hard 64 KiB bound to successful and HTTP-error response reads, disable redirects, redact the configured API key from returned error strings, and truncate errors. Provider output is untrusted report text: it must not become graph facts, execute tools, approve validation, authorize release, or cross tenant/repository contexts. Redaction is presently targeted rather than a general secret scanner, so contributors must avoid adding secrets or raw source to prompts and errors.

### Adaptive router authority and audit boundary

`routing.registry.BUNDLED_PROFILES` is the immutable authority allowlist; the bounded cached Ollama inventory establishes availability only. Inventory presence does not authorize a model. Unknown identifiers, aliases, missing profiles, stale inventories, and exact-digest mismatches fail closed. Lifecycle eligibility enforces the 30-day minimum retirement runway: a dated retirement must be strictly more than 30 days after the evaluation date. Capability and context requirements, data policy, risk, default-only effort support, and the configured request/repository budget are ranking hard gates before deterministic role and provider-reported usage class ranking. A recommendation does not prove quota remains: actual repository and machine quota reservation happens atomically during approval consumption. Usage class is coarse provider metadata, not a USD price or measured saving.

The active provisional pool comprises `kimi-k2.7-code:cloud` for primary coding at high usage, `minimax-m2.7:cloud` for coding and agentic work at medium usage, `nemotron-3-super:cloud` for reasoning and review at medium usage, and `minimax-m3:cloud` for long-context and agentic work at high usage. All four accept only `default` effort. Provisional profiles are ineligible for high-risk tasks, which produce a manual frontier handoff rather than weakening a gate.

The execution authority binds one signed, short-lived, single-use approval to the exact model and inventory digest, effort, graph/context manifest, input and output limits, and quota reservation. Runtime independently revalidates the signed digest against bounded loopback inventory before approval consumption and the provider POST. The canonical loopback executor makes one request. It never automatically retries, falls back, switches models, pulls a model, reuses an approval, or follows redirects. Non-TTY input or output, JSON mode, CI, and `--yes` are incapable of granting execution authority.

Provider text crosses only the interactive display boundary. The CLI escapes terminal controls and delimiter impersonation, frames every line, and keeps the text ephemeral. Persistence contains the validated receipt, hashes, bindings, and bounded audit metadata, never the displayed text. A new attempt is durably recorded as `pending`; successful receipt finalization transactionally creates the execution, receipt, evidence, budget link, and `completed` transition.

If that final transaction fails after the single provider call, the service attempts to stage the validated receipt and marks the attempt `persistence_failed`. The attempt is reconcilable only while that staged state remains intact and available. `graphite route recoverable <root> --limit 50 --json` exposes only a bounded, deterministically ordered page of validated attempt IDs/status; `next_cursor` can be supplied with `--after` when `has_more` is true. `graphite route reconcile <root> --attempt-id <id> --json` transactionally performs the missing finalization with a sanitized receipt and without a provider call or approval issue, consumption, or reuse. Expected recovery validation/storage failures cross a dedicated allowlisted CLI boundary: JSON mode emits only a stable error code object and text mode emits a fixed path-free code. Operators should preserve or back up routing state before repair. If storage is unavailable for normal finalization and fallback staging, or staged state is deleted, corrupted, or lost with the disk, reconciliation is unavailable.

The schema-v3 migration deliberately maps schema-v1 `pending` and `persistence_failed` attempts that lack token and request-hash bindings to `legacy_unrecoverable` with `legacy_attempt_bindings_missing`. It separately maps schema-v2 nonterminal attempts that have those bindings but predate durable inventory-digest binding to `legacy_unrecoverable` with `legacy_attempt_digest_missing`. Both classes are quarantined and never replayed. Legacy `completed` rows remain intentionally preserved as read-only history. They are not reclassified as recoverable and are not rewritten to manufacture stronger evidence than the earlier schema recorded. New attempts and staged receipts persist the lowercase 64-hex digest from the signed approval manifest; finalization and reconciliation require an exact match.

## Artifacts and state

A build produces `graph.json`, `graph.html`, and `GRAPH_REPORT.md` in the configurable output directory (`graph-out` by default), plus internal `.graphite_manifest.json`, `.graphite_graph.json`, `.graphite_clusters.json`, `.graphite_analysis.json`, and `.graphite_validation.json` there. The manifest records scanned project-relative file paths and hashes; `graphite check` compares it with a new scan to report added, changed, and removed files. Extraction cache entries live under the configured cache directory (`.cache/graphite` by default) and are keyed by version and content-derived hashes. The daemon writes atomic status JSON under its state directory. Initialization/bootstrap may generate or update platform instruction files such as `GRAPHITE.md` and supported assistant integration files.

Current code sorts collection, extraction merge, graph construction, community members, and most query/review outputs; validates the public bundle before the normal report path publishes it; rejects unsafe public `source_file` paths on that path; and atomically replaces individual files where `io.py` is used. Cache writes are deterministic JSON but are not routed through the atomic helper. Multi-file report publication is not transactional. Direct query, impact, context, and MCP consumers neither validate the bundle nor bound their JSON read today.

Contributor invariants are stricter: artifact identities and repository paths stay normalized and project-relative; output ordering is stable; externally consumed structures are schema/invariant validated before use; incomplete or invalid bundles are rejected rather than silently accepted; and files are atomically replaced wherever interruption could expose partial content. If a new artifact set requires all-or-nothing consistency, add generation IDs or a commit/manifest protocol instead of assuming per-file atomic writes provide it.

## Failure behavior

- Escaping, absolute, symlink-resolved-outside-root, malformed Git, unreadable, binary, and oversized repository inputs are rejected or skipped according to the ingestion contract. Enumeration failures that prevent a trustworthy scan raise `IngestError`; individual unreadable files may be skipped or represented as extraction errors.
- `validate_graph_bundle` returns structured errors for malformed bundles, and the normal report path uses `assert_valid_graph_bundle` so an invalid public bundle fails before public exporters run. The standalone validate command exits non-zero on invalid JSON or failed validation. The query, impact, context, and MCP direct-load paths do not invoke validation or bound the read; callers are responsible for validating and constraining artifacts before those paths consume them until that gap is closed.
- Git failures use typed, sanitized exceptions with fixed messages. Other subprocess adapters currently return bounded diagnostic reasons or platform-specific errors; they do not all share Git's typed exception hierarchy, environment isolation, or output cap.
- Atomic replacement prevents a partially written individual artifact from being accepted at its final path. It does not make the entire report directory transactional; consumers must reject invalid bundles and use freshness/manifest evidence to detect stale or mixed state.
- Optional model enrichment catches provider/configuration/network failures, records a sanitized error result, and leaves deterministic graph construction, validation, and export operational. Model output never supplies validation or release authority.

## Extension points and invariants

**Languages.** Extend classification in `ingest.py`, add the Tree-sitter extractor and resolver behavior, and add fixtures/tests for symbols, imports, calls, malformed input, path containment, cross-file identity, and deterministic output. A language implementation must degrade safely when its parser or optional compiler is unavailable.

**Exporters.** Consume validated structures, encode for the exact destination context, write through atomic helpers, and exclude absolute paths, environment metadata, credentials, and other host-identifying state. Add hostile-string and interrupted-write tests.

**Query and analysis.** Preserve deterministic tie-breaking and ordering, bound result sizes and graph traversal, filter project nodes where the operation promises project-only evidence, and keep machine-readable JSON contracts stable.

**Model adapters.** Implement the provider protocol outside graph construction. Require explicit configuration, deadlines, bounded input/output, sanitized failures, and deterministic fakes in tests. Treat responses as untrusted annotations and never allow a model provider to change graph facts or validation results.

**Process adapters.** Use argv rather than shell interpolation, define time and output/resource bounds, return typed or safely structured failures, sanitize diagnostics, and isolate platform-specific behavior from the portable core. Environment inheritance must be a deliberate, documented choice.

Across all extensions, preserve these invariants:

- The core works with zero LLM or network access.
- Identical repository input and deterministic-core configuration produce the same graph facts and ordering only under a fixed Graphite version, Tree-sitter/parser package set, Node/TypeScript toolchain, and resolver mode/outcome. Parser availability changes whether structural or generic extraction runs, while `typescript_resolver=auto` may select compiler-backed resolution on one host and heuristics on another. Use heuristic or disabled TypeScript resolution when the compiler toolchain and cross-host resolver outcome cannot be controlled. Timestamps, random host state, and concurrency completion order must not otherwise affect core facts. Optional probabilistic model annotations are outside this guarantee and must never alter graph facts.
- Repository identities and public source paths are normalized and project-relative.
- Artifacts and logs contain no secrets, absolute system paths, or unnecessary environment metadata.
- Contributor target invariant (not universal current behavior): validate and size-bound untrusted structures before consumption; model text is never validation evidence.
- Public schemas and CLI behavior remain compatible unless an explicit, tested migration is provided.
# Adaptive routing trust boundary

The development router is an approval-gated change broker for two authenticated
subscription CLIs: Claude Code and Codex. Ollama is excluded from governed
development execution; OpenRouter remains reserved for in-application inference.
Trust zones are the source repository and validated graph, detached task worktrees,
the provider CLI process and its existing credential home, repository-local audit
storage, and machine-local signing state. Graphite neither reads API keys nor copies
subscription credentials into prompts, telemetry, child arguments, or storage.

The authority sequence is capability verification -> deterministic recommendation
-> detached worktree -> canonical prompt and manifest -> default-No single-use
approval -> CLI identity recheck -> one bounded process -> diff inspection ->
credential-free validation -> human accept/reject -> optional explicit cleanup.
Each transition binds the repository commit, capability snapshot, requested and
effective model, effort, executable hash/version, adapter protocol, permission
mode, prompt hash, token reservation, and timeout. No later stage can widen an
earlier permission. Worktree, approval, attempt, validation, and review identities
are immutable database evidence.

Capability verification reports actual input and output usage. The profile boundary
validates both values against the approved limits before constructing and saving
active authority. Invalid, missing, or over-budget usage cannot produce a persisted
snapshot; acceptance tooling uses the ordered verify-and-save operation rather than
an independently ordered persistence step.

Provider output and edits are untrusted. The diff boundary rejects filesystem
indirection, repository nesting, submodule changes, case collisions, scope or size
violations, and source/diff drift. High-risk tasks require a separately approved
read-only review by the other provider. Acceptance produces only a detached commit;
it never merges. Retry, fallback, provider switching, session reuse, cleanup, and
merge are never automatic.

Telemetry has a closed typed schema and excludes source, prompt/response text, diff
contents, paths, secrets, and raw diagnostics. Subscription cost remains `unknown`.
Recency weighting and Wilson confidence penalties can propose a signed policy
candidate. Candidate creation grants no authority. Interactive promotion cannot
alter provider allowlists, permission ceilings, risk ceilings, or autonomy; rollback
appends an activation event and retains all prior evidence.

Schema v4 is a forward cutover. Before changing a v3 database, Graphite creates a
private v3 backup and SHA-256 marker, validates schema and integrity, and quarantines
live legacy-provider attempts. Rollback requires stopped writers, verified backup
restore, and the matching old code; v4 is not edited into v3. A partial schema,
missing marker, lock, or failed integrity check leaves routing stopped for verified
restore or a tested forward fix.
