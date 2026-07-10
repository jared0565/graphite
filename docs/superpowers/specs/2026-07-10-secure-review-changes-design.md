# Secure Change Review Design

## Objective

Harden Graphite's generated HTML report against repository-controlled script injection and add a deterministic, model-agnostic change-review command that turns repository changes into explicit review and acceptance criteria.

The release must remain local-first and zero-LLM by default. The new review workflow must not depend on a model provider, agent vendor, hosted service, or proprietary instruction format.

## Context and Findings

Graphite already provides graph freshness checks, per-file impact analysis, compact context, graph validation, and optional provider-agnostic LLM enrichment. Its baseline suite passes with 92 tests and 3 environment-dependent skips when pytest uses a writable temporary directory.

The audit identified these in-scope weaknesses:

1. The HTML exporter inserts graph-derived JSON directly into a `script` element. A repository-controlled value containing a closing script tag can escape that data context.
2. The viewer renders graph-derived labels and cluster names with `innerHTML`. A malicious source symbol or filename can therefore become executable markup when a user opens the local report.
3. The HTML exporter imports the atomic-write helper but writes directly with `open`, weakening the artifact-integrity guarantee documented by the project.
4. Graphite can report impact for named files, but it does not produce a repository-level review packet with explicit, verifiable acceptance criteria. Agents and humans must assemble this information manually and inconsistently.
5. The repository has pre-existing lint debt. Cleanup will be limited to files changed by this feature; unrelated findings will be documented to preserve a surgical diff.

The security skill has no framework-specific reference for this Python CLI architecture, so the hardening design uses standard context-safe encoding, DOM construction, least-surprise error handling, and existing project conventions.

## Chosen Approach

Implement a focused release candidate:

- secure the generated viewer at both server-side serialization and client-side rendering boundaries;
- restore atomic HTML publication;
- add `graphite review-changes` as a deterministic CLI workflow;
- provide Markdown for reviewers and JSON for CI or agent integration;
- add focused regression and integration tests;
- publish an audit report that distinguishes fixed findings from deferred recommendations.

This approach is preferred over a security-only patch because the review packet closes a practical workflow gap. It is preferred over generating broad agent methodology files because Graphite should supply evidence and acceptance criteria without imposing one vendor's orchestration model.

## HTML Hardening

### Embedded data

The exporter will serialize the graph bundle as JSON and neutralize characters that can terminate or alter an HTML script context. At minimum, literal `<`, `>`, and `&` characters will be encoded as Unicode escapes in the embedded payload. This preserves JSON semantics while preventing `</script>` from appearing in the HTML source.

The document title will be HTML-escaped before template substitution.

### DOM rendering

The selection panel will no longer interpolate graph values into `innerHTML`. Static layout will be created once, and dynamic repository-controlled values will be assigned with `textContent`. Line breaks and separators will use explicit DOM nodes or safe static markup.

### Artifact publication

The exporter will call Graphite's existing `atomic_write_text` helper. A failed or interrupted report write must not replace the last complete report with a partial file.

## `review-changes` Capability

### Command contract

The command will support:

```text
graphite review-changes [PATH] [FILES ...]
graphite review-changes . --json
```

When files are supplied, Graphite will review exactly those normalized project-relative paths. Otherwise, it will discover changed paths from Git using a bounded, non-shell subprocess call. Discovery will include staged, unstaged, untracked, and deleted paths. Rename records will resolve to their destination path where one exists.

The command will accept the existing graph path and impact-depth controls. It will not invoke an LLM.

### Processing flow

1. Resolve and validate the project root.
2. Collect explicit or Git-discovered changes.
3. Normalize paths, reject paths outside the project, deduplicate them, and order them deterministically.
4. Read graph freshness metadata without rebuilding the graph.
5. Load and validate the existing graph bundle.
6. Calculate impacted files and likely tests using the existing impact semantics.
7. Derive transparent risk signals from facts such as deleted files, missing graph matches, stale graph state, absent likely tests, broad impact, and changes to operational or dependency configuration.
8. Produce acceptance criteria tied directly to those signals.
9. Emit Markdown by default or structured JSON with a stable schema.

### Output schema

The machine-readable result will contain:

- schema version;
- project-relative root label, without leaking an absolute path;
- deterministic changed-file records and discovery source;
- graph freshness and validation state;
- impacted files and likely tests;
- risk level and individually identified risk signals;
- acceptance criteria with stable IDs, descriptions, and verification hints;
- warnings and blockers.

Risk is advisory, not a claim that a change is safe. A blocker means the evidence is insufficient for reliable acceptance, such as a missing or invalid graph. The command will return non-zero only for invalid input, operational failure, or blockers when the caller explicitly requests enforcement. Ordinary risk signals remain review information.

### Model-agnostic boundary

The review engine will be a pure Python module with typed data contracts and no dependency on `graphite.llm`. CLI formatting will be separate from evidence collection. Any LLM or coding agent can consume the Markdown or JSON, but no model is required and no source content is transmitted.

## Failure Handling

- Outside-root explicit paths are rejected rather than silently rewritten.
- A non-Git directory without explicit files returns a clear actionable error.
- An empty change set returns a valid low-risk packet stating that no changes were found.
- A missing, stale, or invalid graph is reported explicitly. The command will not hide the problem by rebuilding automatically.
- Git subprocess execution is bounded by a timeout, uses an argument list, disables stdin, and does not invoke a shell.
- Malformed Git output or undecodable paths produce a sanitized error without exposing secrets or absolute system paths.

## Testing Strategy

Tests will be written before implementation for each behavior:

1. hostile labels and closing script tags remain inert in generated HTML;
2. dynamic graph values are never assigned through `innerHTML`;
3. HTML export uses the atomic writer successfully;
4. explicit-file review output is deterministic and includes impact, likely tests, risks, and acceptance criteria;
5. Git discovery handles staged, unstaged, untracked, deleted, and renamed files;
6. outside-root paths and non-Git implicit discovery fail safely;
7. empty changes, stale graphs, invalid graphs, and missing graph nodes are represented accurately;
8. JSON and Markdown contain equivalent evidence;
9. existing tests remain green and touched files pass Ruff.

Tests will use temporary local repositories and will not access the network or an LLM.

## Documentation and Audit Deliverables

The README will document the command, its zero-LLM behavior, exit semantics, and example output. A security and reliability audit report will record:

- fixed findings with code references and verification;
- residual risks and deferred recommendations;
- the acceptance checklist for reviewers;
- the baseline and final verification results.

## Acceptance Criteria

The implementation is acceptable when:

1. All existing and new tests pass using a writable test temporary directory.
2. Touched source and test files pass Ruff.
3. Generated HTML contains no literal attacker-supplied closing script tag and uses no dynamic `innerHTML` assignment.
4. HTML publication uses the atomic-write helper.
5. `review-changes` produces byte-stable JSON for identical repository and graph state.
6. The review packet includes explicit evidence, risk signals, and verifiable acceptance criteria without invoking an LLM.
7. No new runtime dependency is added.
8. Documentation accurately describes limitations and does not claim that a review packet proves security.

## Deferred Recommendations

The following are intentionally outside this focused change unless implementation reveals direct coupling:

- a broader cleanup of all pre-existing Ruff findings;
- network egress policy controls for user-configured LLM endpoints;
- response-size limits for optional LLM HTTP adapters;
- cryptographic signing of generated artifacts;
- automatic execution of project test commands, which would expand Graphite's trust and command-execution boundary.

