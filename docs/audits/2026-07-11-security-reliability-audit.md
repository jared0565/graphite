# Security and Reliability Audit — 2026-07-11

## Executive summary

This audit reviewed Graphite's generated HTML trust boundary, artifact publication, and repository-level change-review evidence. One high-severity security issue and one medium-severity reliability issue were confirmed and fixed; one informational capability gap with medium operational priority was also closed. The new review path is deterministic, local, zero-LLM, and independent of model, vendor, and agent. It validates evidence strings and paths, and caps custom graph input at 128 MiB, before producing impact, likely-test, risk, and acceptance-criteria output.

The changes materially reduce exposure; they do not make Graphite perfectly secure. Residual recommendations remain for optional LLM endpoint governance and response bounds, Git/packet/output resource limits, dependency/CI controls, existing lint debt, artifact authenticity when artifacts cross trust boundaries, injected atomic-write failure tests, and descriptor-based no-follow reads in hostile shared workspaces. The explicit release gates recorded below passed on 2026-07-11.

## Scope and threat model

### Scope

- HTML generation and publication: `src/graphite/export/html.py`, `src/graphite/io.py`, and focused tests.
- Deterministic change review: `src/graphite/review.py`, `src/graphite/cli.py`, and focused tests.
- Residual boundaries: optional LLM HTTP transport, review resource limits and output handling, dependency/CI posture, artifact distribution, and filesystem race resistance.

### Threat model

Repository content, graph artifacts, Git paths/status output, project names, custom graph locations, and malformed files are untrusted. An attacker may control a cloned repository or writable shared workspace and may attempt script injection, path escape, terminal/Markdown manipulation, memory exhaustion, information disclosure through errors, or a time-of-check/time-of-use (TOCTOU) filesystem race. Local users intentionally configuring an LLM endpoint establish a separate outbound network and credential trust boundary.

Out of scope were provider-side security, operating-system compromise, malicious Python interpreters or Git binaries, and the authenticity of artifacts distributed by an external release channel.

## Fixed findings

### High severity — immediate

#### 1. GRA-SEC-001 — Repository-controlled script/DOM execution in `graph.html`

**Status:** Fixed.

**Impact:** A malicious repository could place executable markup in graph data or the repository title and cause JavaScript execution in the local origin when a user opened the generated viewer, exposing data available to that origin and enabling actions with the viewer's browser privileges.

**Evidence and mitigation:** The viewer embeds graph data in a script context at [`src/graphite/export/html.py:46-48`](../../src/graphite/export/html.py#L46). Script-safe JSON serialization now encodes `&`, `<`, and `>` at [`src/graphite/export/html.py:208-214`](../../src/graphite/export/html.py#L208), the document title is HTML-escaped at [`src/graphite/export/html.py:231-237`](../../src/graphite/export/html.py#L231), and dynamic selection content is assembled with `textContent`, `createTextNode`, and `replaceChildren` at [`src/graphite/export/html.py:183-198`](../../src/graphite/export/html.py#L183). No dynamic `innerHTML` sink remains in the viewer.

**Tests:** Injection breakout, title escaping, text-only DOM rendering, template-token preservation, and JSON round-trip behavior are covered at [`tests/test_html_security.py:21-32`](../../tests/test_html_security.py#L21) and [`tests/test_html_security.py:54-72`](../../tests/test_html_security.py#L54).

### Medium severity — high priority

#### 2. GRA-REL-001 — Non-atomic HTML publication

**Status:** Fixed.

Interrupted writes could previously leave a truncated or partially replaced viewer. The shared helper writes a same-directory temporary file, flushes and `fsync`s it, atomically replaces the destination, and removes the temporary file on failure at [`src/graphite/io.py:11-26`](../../src/graphite/io.py#L11). HTML publication uses that helper at [`src/graphite/export/html.py:238-244`](../../src/graphite/export/html.py#L238). Successful replacement and the absence of leftover temporary files after success are tested at [`tests/test_reliability.py:76-82`](../../tests/test_reliability.py#L76), and the HTML exporter delegation is tested at [`tests/test_html_security.py:35-51`](../../tests/test_html_security.py#L35). Injected write, flush, `fsync`, and replace failures are not covered by those tests.

### Informational severity — medium operational priority

#### 3. GRA-OPS-001 — Missing repository-level review evidence

**Status:** Fixed.

This was an operational capability gap, not a security defect: the repository lacked one command that joined the actual change scope to graph freshness, validation, impact, likely tests, risk signals, and acceptance criteria. Git and explicit discovery are implemented at [`src/graphite/review.py:31-176`](../../src/graphite/review.py#L31); deterministic packet construction and advisory risk are implemented at [`src/graphite/review.py:230-334`](../../src/graphite/review.py#L230); and Markdown rendering validates evidence fields before output at [`src/graphite/review.py:337-397`](../../src/graphite/review.py#L337). The CLI contains custom graph paths within the project, caps custom graph input at 128 MiB, and emits JSON or Markdown at [`src/graphite/cli.py:582-659`](../../src/graphite/cli.py#L582).

Determinism and the absence of LLM/model/timestamp fields are asserted at [`tests/test_review.py:792-813`](../../tests/test_review.py#L792). The implementation neither selects nor calls an agent or model; it is an evidence contract usable by any reviewer or automation.

## Quality hardening completed

- **Git boundary:** Git discovery requires the requested path to be the worktree top-level, uses `shell=False`, NUL-delimited porcelain records, a timeout, strict status grammar, safe UTF-8 relative paths, and sanitized errors ([`src/graphite/review.py:54-176`](../../src/graphite/review.py#L54)). Tests cover staged, unstaged, untracked, deleted, and renamed changes plus top-root, protocol, encoding, and error cases ([`tests/test_review.py:191-361`](../../tests/test_review.py#L191)).
- **Packet boundary:** Discovery mode, statuses, project labels, paths, graph-derived paths, control/format Unicode, source-file fields, and formatter shapes are validated before output ([`src/graphite/review.py:400-550`](../../src/graphite/review.py#L400), [`src/graphite/review.py:619-734`](../../src/graphite/review.py#L619)).
- **Graph boundary:** Custom graph input must resolve within the project root and is capped at 128 MiB; malformed and recursive JSON failures are sanitized, graph bundles are structurally validated, and the custom graph's sibling manifest drives freshness ([`src/graphite/cli.py:63-64`](../../src/graphite/cli.py#L63), [`src/graphite/cli.py:582-624`](../../src/graphite/cli.py#L582), [`src/graphite/review.py:427-498`](../../src/graphite/review.py#L427)). Containment, size, parsing, and custom-freshness behavior are tested at [`tests/test_review.py:864-975`](../../tests/test_review.py#L864) and [`tests/test_review.py:1018-1074`](../../tests/test_review.py#L1018).
- **Exit semantics:** For a successfully constructed packet, risk does not affect exit status; `--fail-on-blocker` makes evidence blockers return `1` ([`src/graphite/cli.py:655-659`](../../src/graphite/cli.py#L655)). Invalid inputs and operational errors return `1` independently; a high-risk packet without blockers remains successful ([`tests/test_review.py:978-986`](../../tests/test_review.py#L978)).

## Residual recommendations

### 1. GRA-SEC-R01 — Optional LLM endpoint egress and credential policy

**Severity / priority:** Medium / P1 before managed or multi-tenant deployment.

The generic OpenAI-compatible adapter intentionally accepts a user-configured base URL and sends the configured bearer credential to its `/chat/completions` endpoint ([`src/graphite/llm.py:43-77`](../../src/graphite/llm.py#L43)). This is an intended endpoint feature, not an exploitable vulnerability by itself. In centrally managed environments, add an explicit egress/endpoint policy: HTTPS enforcement for remote hosts, an allowlist or administrator approval, loopback/private-address rules appropriate to deployment, redirect policy, scoped credentials, and auditable provider selection. When optional enrichment is enabled, the prompt builder does not intentionally include source-file contents, but it transmits graph metadata, filenames, identifiers, labels, and analysis summaries to the configured provider ([`src/graphite/llm.py:275-313`](../../src/graphite/llm.py#L275)).

### 2. GRA-REL-R01 — Bound optional LLM HTTP response bodies

**Severity / priority:** Medium / P1.

Successful LLM responses are currently read without a byte limit at [`src/graphite/llm.py:316-323`](../../src/graphite/llm.py#L316). Add a configurable maximum response size, stream at most `limit + 1` bytes, reject oversized and non-object JSON responses, and apply equivalent bounds to HTTP error bodies. This limits memory consumption from a faulty or untrusted configured endpoint.

### 3. GRA-REL-R02 — Cap Git discovery, evidence cardinality, and rendered output

**Severity / priority:** Medium / P1.

Git status stdout is captured in memory before parsing ([`src/graphite/review.py:83-116`](../../src/graphite/review.py#L83)), and all parsed changes are retained ([`src/graphite/review.py:116-152`](../../src/graphite/review.py#L116)). Packet impact lists and JSON/Markdown output also have no aggregate item or byte cap ([`src/graphite/review.py:321-397`](../../src/graphite/review.py#L321), [`src/graphite/cli.py:655-658`](../../src/graphite/cli.py#L655)). Add maximum Git stdout bytes, change count, graph-derived impact/test entries, per-field length, and total serialized output bytes; reject or explicitly truncate with machine-readable notices. This reduces memory-exhaustion risk from very large or adversarial repositories. The existing 128 MiB custom graph cap does not cover these paths.

### 4. GRA-SUP-R01 — Add dependency vulnerability/provenance scanning and CI

**Severity / priority:** Medium / P1 before release automation.

Runtime and development dependencies are declared at [`pyproject.toml:7-23`](../../pyproject.toml#L7), but this audit found no repository `.github/workflows` directory and no checked-in dependency vulnerability or provenance policy. Add CI that installs from a reproducible lock/constraints strategy, runs tests and Ruff, scans known vulnerabilities, produces an SBOM, and records build provenance. Pin CI actions and protect update review; do not automatically apply unreviewed dependency fixes.

### 5. GRA-QUAL-R01 — Retire repository-wide pre-existing Ruff debt

**Severity / priority:** Low / P2.

The pre-change baseline contained 13 Ruff findings. Files touched by the security/review implementation were clean in the focused lint check. A current repository-wide recheck reports 11 pre-existing findings outside those touched implementation files: unused imports at `src/graphite/cache.py:6`, `src/graphite/cluster.py:4`, `src/graphite/daemon.py:15`, `src/graphite/extract/ast.py:12`, `src/graphite/graph.py:5`, `src/graphite/mcp_server.py:5`, `src/graphite/replacement_audit.py:4`, `src/graphite/windows_task.py:5`, `src/graphite/windows_task.py:9`, and `tests/test_replacement_audit.py:9`, plus an unused local at `src/graphite/mcp_server.py:15`. Clear these in a separate behavior-preserving cleanup and make Ruff a CI gate. The removed `start_daemon_task` item is not an unresolved finding.

### 6. GRA-SUP-R02 — Sign or checksum artifacts only when they cross trust boundaries

**Severity / priority:** Low / P2, conditional.

Local artifacts are atomically published but are not authenticated ([`src/graphite/cli.py:188-194`](../../src/graphite/cli.py#L188), [`src/graphite/io.py:11-32`](../../src/graphite/io.py#L11)). That is appropriate for local working output. If graphs, reports, packages, or installers are distributed across a release, tenant, host, or administrative trust boundary, publish checksums and preferably signed provenance, and verify them before consumption.

### 7. GRA-SEC-R02 — Descriptor-based no-follow verification for hostile shared workspaces

**Severity / priority:** Low / P3.

Custom graph containment resolves the path before a later ordinary open ([`src/graphite/cli.py:582-602`](../../src/graphite/cli.py#L582)). In a hostile workspace writable by another principal, an attacker could attempt to swap a path component between validation and open. For that deployment model, open through a root directory descriptor with platform-appropriate no-follow semantics, validate the opened descriptor, and read from the same descriptor. Existing containment is adequate for the normal single-user repository model; this recommendation addresses residual TOCTOU risk.

### 8. GRA-REL-R03 — Inject atomic-publication failures in tests

**Severity / priority:** Low / P2.

The atomic helper contains cleanup logic for write, flush, `fsync`, and replace failures ([`src/graphite/io.py:11-26`](../../src/graphite/io.py#L11)), but current tests assert only successful replacement and no leftover temporary file after success ([`tests/test_reliability.py:76-82`](../../tests/test_reliability.py#L76)). Add injected-failure tests for each stage and assert that the prior destination remains intact where the platform contract permits, temporary files are removed, and the original exception propagates.

## Model-agnostic assurance

`review-changes` calls only local path, Git, graph-validation, graph-query, and formatting logic ([`src/graphite/cli.py:627-659`](../../src/graphite/cli.py#L627)). It has no provider configuration, prompt, model, timestamp, agent SDK, or network transport in its contract. Stable sorting is applied to changes and evidence ([`src/graphite/review.py:247-249`](../../src/graphite/review.py#L247), [`src/graphite/review.py:466-483`](../../src/graphite/review.py#L466)), and JSON output is key-sorted ([`src/graphite/cli.py:655-656`](../../src/graphite/cli.py#L655)). The packet is therefore model/vendor/agent-agnostic and repeatable for the same repository, graph, arguments, and Git state.

## Verification record

- **Pre-feature baseline:** on Windows with Python 3.14.5, `python -m pytest -q` produced **92 passed, 3 skipped**.
- **Focused HTML result at feature commit `6835227`:** on Windows with Python 3.14.5, `python -m pytest -q tests/test_html_security.py` produced **4 passed**.
- **Focused review result at feature commit `6835227`:** on Windows with Python 3.14.5, `python -m pytest -q tests/test_review.py` produced **114 passed**.
- **Ruff provenance:** on Windows with Python 3.14.5, `python -m ruff check . --output-format concise` reported **13 findings** before the feature changes and **11 pre-existing findings** after them, outside the touched implementation files.
- Documentation checks used CLI help, real-line-reference/token inspection, Markdown link inspection, and `git diff --check`.

These historical observations are retained for provenance but are not used as final release evidence; the fresh post-integration results follow.

### Final release verification — 2026-07-11

Fresh release verification was run from tested pre-recording commit `a5bbed9` on Windows with Python 3.14.5. Relevant commands used `$env:PYTHONPATH = "src"` and `$env:GRAPHITE_LLM = "none"`; pytest used fresh writable base directories under `F:\tmp`.

- `python -m pytest -q tests/test_html_security.py tests/test_review.py --basetemp F:\tmp\graphite-focused-final` exited `0`: **118 passed in 8.60s**.
- `python -m pytest -q --basetemp F:\tmp\graphite-full-final` exited `0`: **210 passed, 3 skipped in 26.19s**.
- `python -m ruff check src/graphite/export/html.py src/graphite/review.py src/graphite/cli.py tests/test_html_security.py tests/test_review.py` exited `0`: **All checks passed**.
- `git diff --check 3718a7b..HEAD` exited `0` with no output.
- `python -m graphite build .` exited `0` and wrote `graph-out/GRAPH_REPORT.md`, `graph-out/graph.json`, and `graph-out/graph.html`.
- `python -m graphite validate` exited `0`: **graph valid (1764 nodes / 3217 edges, 0 warnings)**.
- `python -m graphite review-changes . --json --fail-on-blocker` exited `0`. The saved output parsed as valid JSON, contained **0 blockers**, selected `CONFIRM_CLEAN`, did not contain the absolute worktree root, and had none of the forbidden `llm`, `model`, `timestamp`, `created_at`, `generated_at`, or `datetime` keys.
- `git status --short` exited `0` with no output; ignored `graph-out/` artifacts did not dirty the worktree.

All release-gate commands above passed. The residual recommendations remain open hardening work and retain their stated deployment conditions and priorities.

## Acceptance checklist and recommendation

- [x] Repository-controlled graph JSON cannot terminate the data script through literal HTML delimiters.
- [x] Repository title and dynamic DOM text are rendered in context-appropriate safe forms.
- [x] HTML publication uses the shared atomic writer and has focused regression coverage.
- [x] Change discovery covers Git and explicit scopes with containment, protocol, and error hardening.
- [x] Review packets validate graph evidence and expose impact, likely tests, risk, blockers, warnings, and acceptance criteria deterministically.
- [x] For a successfully constructed packet, high risk remains advisory; evidence-blocker failure is explicit opt-in behavior. Invalid inputs and operational errors fail independently.
- [x] Custom graph reads are project-contained, capped at 128 MiB, sanitized, and checked against a sibling manifest.
- [x] Local review output is documented as sensitive repository metadata that callers must protect.
- [x] Focused tests passed with a fresh writable base: `python -m pytest -q tests/test_html_security.py tests/test_review.py --basetemp F:\tmp\graphite-focused-final`.
- [x] The full suite passed with a fresh writable base: `python -m pytest -q --basetemp F:\tmp\graphite-full-final`.
- [x] Ruff passed on the release-gate implementation and test files: `python -m ruff check src/graphite/export/html.py src/graphite/review.py src/graphite/cli.py tests/test_html_security.py tests/test_review.py`.
- [x] The graph built from this worktree with LLM use disabled: `$env:PYTHONPATH = "src"; $env:GRAPHITE_LLM = "none"; python -m graphite build .`.
- [x] The generated graph validated: `$env:PYTHONPATH = "src"; $env:GRAPHITE_LLM = "none"; python -m graphite validate`.
- [x] The live blocking review produced the inspected clean packet: `$env:PYTHONPATH = "src"; $env:GRAPHITE_LLM = "none"; python -m graphite review-changes . --json --fail-on-blocker`.
- [x] The pre-recording patch and worktree were clean: `git diff --check 3718a7b..HEAD` and `git status --short`.
- [ ] Triage residual P1 recommendations before managed or multi-tenant deployment.

**Recommendation:** Accept the fixed findings for integration based on the passed release gates above. This acceptance does not claim perfect security. Track the residual items as hardening work, with LLM egress governance, LLM and review resource bounds, and CI/dependency assurance prioritized before broader managed deployment.
