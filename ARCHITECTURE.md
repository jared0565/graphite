# Graphite architecture

This guide describes the current internal architecture and the constraints contributors must preserve. Graphite targets Python 3.11+, runs locally, produces deterministic code-graph artifacts, and does not require a model provider. Repository data, external processes, generated artifacts, and model input/output are trust boundaries rather than trusted implementation details.

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
    -> validate and render deterministic artifacts
    -> optionally enrich a report through a configured model adapter
```

The implementation stages are:

1. **Collection — `ingest.py`, `config.py`, `git.py`.** Configuration establishes file-count and file-size limits and the cache and output locations. Ingestion normalizes project-relative paths, resolves candidates beneath the repository root, skips unsafe or ineligible files, and sorts accepted entries. Git-backed enumeration uses `GitRunner` with a trusted executable outside the repository, an argument vector, no shell, a filtered Git environment, a timeout, and a stdout cap. The filesystem fallback is also capped by configured file limits, but it does not provide every process control because no child process is involved.
2. **Extraction — `extract/ast.py`, `cache.py`, `ts_bridge.py`, `ts_resolver.mjs`.** Per-language Tree-sitter extractors handle JavaScript/TypeScript, Python, Go, and Rust, while unsupported or unavailable parsers degrade to a file-level record. Extraction emits symbols, imports, calls, and containment edges in deterministic order. The content-addressed cache avoids repeated AST work. TypeScript/JavaScript may additionally use the Node compiler bridge for compiler-backed module resolution; bridge failure falls back to heuristic resolution rather than making Node mandatory.
3. **Resolution — `resolve.py`.** `SourceIndex` builds the set of normalized, project-relative source identities, reads TypeScript aliases and workspace package entry points, prefers compiler-backed TypeScript results when available, and otherwise applies bounded heuristics. Global merge logic also resolves supported call identities and deterministically removes duplicates.
4. **Graph and analysis — `graph.py`, `cluster.py`, `analyze.py`, `query.py`.** `graph.py` constructs a NetworkX `DiGraph` after sorting nodes and edges and normalizing IDs. Analysis filters to project nodes where appropriate and returns bounded, ordered results. Community detection constructs a deterministic undirected view and uses the configured seed (42 by default). JSON conversion preserves the graph's deterministic insertion order.
5. **Validation and review — `validation.py`, `context.py`, `review.py`.** Bundle validation checks structural types, node and edge identity, referential integrity, metadata counts, and unsafe `source_file` paths. `context.py` assembles bounded, sorted graph context, but `cmd_context` currently loads graph JSON directly without calling bundle validation. Review validates a supplied bundle before deriving graph impact evidence; its Git discovery also normalizes status records and rejects malformed or unsafe paths.
6. **Export — `export/json.py`, `export/md.py`, `export/html.py`, `io.py`.** Exporters consume the graph, clusters, analysis, and manifest. JSON serialization provides the data encoding; HTML separately JSON-encodes script data, escapes `<`, `>`, and `&`, HTML-escapes the title, and uses text DOM APIs for runtime labels. Text and JSON outputs use temporary files, `fsync`, and `os.replace` for atomic replacement of each file.
7. **Operations — `watch.py`, `daemon.py`, `daemon_health.py`, `bootstrap.py`, `init.py`, `windows_task.py`, `windows_startup.py`.** These modules watch for changes, maintain multi-project daemon status, evaluate health, generate integration instructions, and manage platform startup. They orchestrate the portable core rather than changing graph semantics.
8. **Optional enrichment — `llm.py`.** Enrichment runs only after deterministic graph construction and analysis. A configured adapter receives a size-capped graph summary, not an authority to mutate or validate the graph. Errors are recorded in the analysis report and do not fail the deterministic build path.

## Module map

| Area | Primary modules | Responsibility | May depend on |
| --- | --- | --- | --- |
| Entry adapters | `cli.py`, `mcp.py`, `mcp_server.py`, `__main__.py` | Parse user/tool input, select operations, format responses | Core, validation, exporters, operations |
| Configuration and ingestion | `config.py`, `ingest.py`, `git.py` | Establish limits; discover, contain, classify, and hash repository files | Cache hashing and bounded process adapter |
| Extraction and resolution | `extract/ast.py`, `cache.py`, `resolve.py`, `ts_bridge.py`, `ts_resolver.mjs` | Parse language structures and resolve project identities | Ingestion contracts, configuration, optional Node/TypeScript process |
| Graph and analysis | `graph.py`, `cluster.py`, `analyze.py`, `query.py` | Build, cluster, analyze, and query the directed graph | NetworkX and deterministic extracted structures |
| Validation and evidence | `validation.py`, `context.py`, `review.py` | Validate bundles and derive review/context evidence | Graph structures; review may use the Git adapter |
| Export | `export/json.py`, `export/md.py`, `export/html.py`, `io.py` | Encode and atomically replace individual artifacts | Validated graph, analysis, clusters, manifest |
| Optional model enrichment | `llm.py` | Select an explicitly configured provider and append a bounded report summary | Deterministic graph/analysis data, configuration, network only when enabled |
| Operations and integration | `watch.py`, `daemon.py`, `daemon_health.py`, `bootstrap.py`, `init.py`, `windows_task.py`, `windows_startup.py` | Freshness, daemon health, generated instructions, and OS startup | Public core operations and explicit platform/process boundaries |

The intended dependency direction is observable in current imports but is not enforced by a dedicated architecture linter. CLI and MCP adapt inputs; portable core modules do not import those UI entry points. Export consumes graph and analysis data; extraction does not depend on exporters. LLM enrichment consumes deterministic reports; graph and validation do not depend on a model provider. Windows startup and task management remain outside the portable core. External execution crosses explicit subprocess boundaries; Git uses the hardened `GitRunner`, while the TypeScript and Windows adapters have their own, not necessarily identical, controls. Contributors must preserve these directions and add tests if a boundary becomes mechanically important.

## Trust boundaries

**Repository input.** Treat paths, bytes, symlinks, encodings, file counts, and file sizes as hostile. Current ingestion resolves roots and candidates, requires candidates to remain below the root, rejects unsafe Git paths, skips unreadable/binary/oversized files, applies configured count bounds, and records normalized project-relative paths. Extraction reads bytes and degrades or reports parse/read errors. New readers must use the same containment model; never trust a repository string as an absolute destination or executable.

**Process boundary.** Git, Node/TypeScript, and Windows task/startup integrations execute outside Python. `GitRunner` is the strongest current adapter: fixed executable discovery outside the repository, argv execution with `shell=False`, disabled stdin/stderr, filtered `GIT_*` environment, timeout, stdout cap, and typed sanitized failures. The TypeScript bridge uses argv, `subprocess.run`, captured output, a configured timeout, and a 500-character diagnostic cap, but it currently inherits the environment and does not impose an explicit output-byte cap. Windows adapters are platform-specific and must be reviewed on their own controls. Do not generalize Git's isolation guarantees to every subprocess.

**Artifact and browser boundary.** Repository-controlled names and strings remain untrusted when rendered. Public graph validation rejects absolute or parent-traversing node/edge `source_file` values. JSON must be serialized, HTML script data must receive script-context escaping, visible browser values must use HTML escaping or text DOM APIs, and public artifacts must not introduce absolute system paths. `io.py` atomically replaces each completed file. The report set is written as several files rather than one filesystem transaction, so readers must validate the bundle and consult manifest/freshness state instead of assuming a directory snapshot is complete.

**Model and network boundary.** Model use is disabled by default and requires explicit configuration; no vendor is required by the core. Current adapters use request timeouts, cap prompt characters, redact the configured API key from returned error strings, and truncate errors. Provider output is untrusted report text: it must not become graph facts, execute tools, approve validation, authorize release, or cross tenant/repository contexts. Redaction is presently targeted rather than a general secret scanner, so contributors must avoid adding secrets or raw source to prompts and errors.

## Artifacts and state

A build produces `graph.json`, `graph.html`, and `GRAPH_REPORT.md` in the configurable output directory (`graph-out` by default), plus internal `.graphite_manifest.json`, `.graphite_graph.json`, `.graphite_clusters.json`, `.graphite_analysis.json`, and `.graphite_validation.json` there. The manifest records scanned project-relative file paths and hashes; `graphite check` compares it with a new scan to report added, changed, and removed files. Extraction cache entries live under the configured cache directory (`.cache/graphite` by default) and are keyed by version and content-derived hashes. The daemon writes atomic status JSON under its state directory. Initialization/bootstrap may generate or update platform instruction files such as `GRAPHITE.md` and supported assistant integration files.

Current code sorts collection, extraction merge, graph construction, community members, and most query/review outputs; validates the public bundle before the normal report path publishes it; rejects unsafe public `source_file` paths; and atomically replaces individual files where `io.py` is used. Cache writes are deterministic JSON but are not routed through the atomic helper. Multi-file report publication is not transactional.

Contributor invariants are stricter: artifact identities and repository paths stay normalized and project-relative; output ordering is stable; externally consumed structures are schema/invariant validated before use; incomplete or invalid bundles are rejected rather than silently accepted; and files are atomically replaced wherever interruption could expose partial content. If a new artifact set requires all-or-nothing consistency, add generation IDs or a commit/manifest protocol instead of assuming per-file atomic writes provide it.

## Failure behavior

- Escaping, absolute, symlink-resolved-outside-root, malformed Git, unreadable, binary, and oversized repository inputs are rejected or skipped according to the ingestion contract. Enumeration failures that prevent a trustworthy scan raise `IngestError`; individual unreadable files may be skipped or represented as extraction errors.
- `validate_graph_bundle` returns structured errors for malformed bundles, and the normal report path uses `assert_valid_graph_bundle` so an invalid public bundle fails before public exporters run. The standalone validate command exits non-zero on invalid JSON or failed validation. Callers that load graph JSON directly are still responsible for validating it first.
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
- Identical repository input and deterministic-core configuration produce the same graph facts and ordering; timestamps, random host state, and concurrency completion order do not affect them. Optional probabilistic model annotations are outside that guarantee and must never alter graph facts.
- Repository identities and public source paths are normalized and project-relative.
- Artifacts and logs contain no secrets, absolute system paths, or unnecessary environment metadata.
- Untrusted structures are validated before consumption; model text is never validation evidence.
- Public schemas and CLI behavior remain compatible unless an explicit, tested migration is provided.
