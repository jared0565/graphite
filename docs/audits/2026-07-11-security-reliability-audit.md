# Security and Reliability Audit — 2026-07-11

## Executive summary

This audit reviewed Graphite's generated HTML trust boundary, artifact publication, and repository-level change-review evidence. One high-severity script/DOM execution issue and two medium-severity reliability/operations gaps were confirmed and fixed. The new review path is deterministic, local, zero-LLM, and independent of model, vendor, and agent. It validates and bounds repository-controlled evidence before producing impact, likely-test, risk, and acceptance-criteria output.

The changes materially reduce exposure; they do not make Graphite perfectly secure. Residual recommendations remain for optional LLM endpoint governance and response bounds, dependency/CI controls, existing lint debt, artifact authenticity when artifacts cross trust boundaries, and descriptor-based no-follow reads in hostile shared workspaces. The final full-suite run is intentionally not claimed here: Task 6 verification is pending.

## Scope and threat model

### Scope

- HTML generation and publication: `src/graphite/export/html.py`, `src/graphite/io.py`, and focused tests.
- Deterministic change review: `src/graphite/review.py`, `src/graphite/cli.py`, and focused tests.
- Residual boundaries: optional LLM HTTP transport, dependency/CI posture, artifact distribution, and filesystem race resistance.

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

Interrupted writes could previously leave a truncated or partially replaced viewer. The shared helper writes a same-directory temporary file, flushes and `fsync`s it, atomically replaces the destination, and removes the temporary file on failure at [`src/graphite/io.py:11-26`](../../src/graphite/io.py#L11). HTML publication uses that helper at [`src/graphite/export/html.py:238-244`](../../src/graphite/export/html.py#L238). Replacement and cleanup are tested at [`tests/test_reliability.py:76-82`](../../tests/test_reliability.py#L76), and the HTML exporter delegation is tested at [`tests/test_html_security.py:35-51`](../../tests/test_html_security.py#L35).

#### 3. GRA-OPS-001 — Missing repository-level review evidence

**Status:** Fixed.

The repository previously lacked one bounded command that joined the actual change scope to graph freshness, validation, impact, likely tests, risk signals, and acceptance criteria. Git and explicit discovery are implemented at [`src/graphite/review.py:31-176`](../../src/graphite/review.py#L31); deterministic packet construction and advisory risk are implemented at [`src/graphite/review.py:230-334`](../../src/graphite/review.py#L230); and bounded Markdown rendering is implemented at [`src/graphite/review.py:337-397`](../../src/graphite/review.py#L337). The CLI loads contained, size-bounded graph evidence and emits JSON or Markdown at [`src/graphite/cli.py:582-659`](../../src/graphite/cli.py#L582).

Determinism and the absence of LLM/model/timestamp fields are asserted at [`tests/test_review.py:792-813`](../../tests/test_review.py#L792). The implementation neither selects nor calls an agent or model; it is an evidence contract usable by any reviewer or automation.

## Quality hardening completed

- **Git boundary:** Git discovery requires the requested path to be the worktree top-level, uses `shell=False`, NUL-delimited porcelain records, a timeout, strict status grammar, safe UTF-8 relative paths, and sanitized errors ([`src/graphite/review.py:54-176`](../../src/graphite/review.py#L54)). Tests cover staged, unstaged, untracked, deleted, and renamed changes plus top-root, protocol, encoding, and error cases ([`tests/test_review.py:191-361`](../../tests/test_review.py#L191)).
- **Packet boundary:** Discovery mode, statuses, project labels, paths, graph-derived paths, control/format Unicode, source-file fields, and formatter shapes are validated before output ([`src/graphite/review.py:400-550`](../../src/graphite/review.py#L400), [`src/graphite/review.py:619-734`](../../src/graphite/review.py#L619)).
- **Graph boundary:** Graphs must resolve within the project root, reads are capped at 128 MiB, malformed and recursive JSON failures are sanitized, graph bundles are structurally validated, and a custom graph's sibling manifest drives freshness ([`src/graphite/cli.py:63-64`](../../src/graphite/cli.py#L63), [`src/graphite/cli.py:582-624`](../../src/graphite/cli.py#L582), [`src/graphite/review.py:427-498`](../../src/graphite/review.py#L427)). Containment, size, parsing, and custom-freshness behavior are tested at [`tests/test_review.py:864-975`](../../tests/test_review.py#L864) and [`tests/test_review.py:1018-1074`](../../tests/test_review.py#L1018).
- **Exit semantics:** For a successfully constructed packet, risk does not affect exit status; `--fail-on-blocker` makes evidence blockers return `1` ([`src/graphite/cli.py:655-659`](../../src/graphite/cli.py#L655)). Invalid inputs and operational errors return `1` independently; a high-risk packet without blockers remains successful ([`tests/test_review.py:978-986`](../../tests/test_review.py#L978)).

## Residual recommendations

### 1. GRA-SEC-R01 — Optional LLM endpoint egress and credential policy

**Severity / priority:** Medium / P1 before managed or multi-tenant deployment.

The generic OpenAI-compatible adapter intentionally accepts a user-configured base URL and sends the configured bearer credential to its `/chat/completions` endpoint ([`src/graphite/llm.py:43-77`](../../src/graphite/llm.py#L43)). This is an intended endpoint feature, not an exploitable vulnerability by itself. In centrally managed environments, add an explicit egress/endpoint policy: HTTPS enforcement for remote hosts, an allowlist or administrator approval, loopback/private-address rules appropriate to deployment, redirect policy, scoped credentials, and auditable provider selection. Keep the current source-code exclusion guarantee in the prompt builder ([`src/graphite/llm.py:275-313`](../../src/graphite/llm.py#L275)).

### 2. GRA-REL-R01 — Bound optional LLM HTTP response bodies

**Severity / priority:** Medium / P1.

Successful LLM responses are currently read without a byte limit at [`src/graphite/llm.py:316-323`](../../src/graphite/llm.py#L316). Add a configurable maximum response size, stream at most `limit + 1` bytes, reject oversized and non-object JSON responses, and apply equivalent bounds to HTTP error bodies. This limits memory consumption from a faulty or untrusted configured endpoint.

### 3. GRA-SUP-R01 — Add dependency vulnerability/provenance scanning and CI

**Severity / priority:** Medium / P1 before release automation.

Runtime and development dependencies are declared at [`pyproject.toml:7-23`](../../pyproject.toml#L7), but this audit found no repository `.github/workflows` directory and no checked-in dependency vulnerability or provenance policy. Add CI that installs from a reproducible lock/constraints strategy, runs tests and Ruff, scans known vulnerabilities, produces an SBOM, and records build provenance. Pin CI actions and protect update review; do not automatically apply unreviewed dependency fixes.

### 4. GRA-QUAL-R01 — Retire repository-wide pre-existing Ruff debt

**Severity / priority:** Low / P2.

The pre-change baseline contained 13 Ruff findings. Files touched by the security/review implementation were clean in the focused lint check. A current repository-wide recheck reports 11 pre-existing findings outside those touched implementation files: unused imports at `src/graphite/cache.py:6`, `src/graphite/cluster.py:4`, `src/graphite/daemon.py:15`, `src/graphite/extract/ast.py:12`, `src/graphite/graph.py:5`, `src/graphite/mcp_server.py:5`, `src/graphite/replacement_audit.py:4`, `src/graphite/windows_task.py:5`, `src/graphite/windows_task.py:9`, and `tests/test_replacement_audit.py:9`, plus an unused local at `src/graphite/mcp_server.py:15`. Clear these in a separate behavior-preserving cleanup and make Ruff a CI gate. The removed `start_daemon_task` item is not an unresolved finding.

### 5. GRA-SUP-R02 — Sign or checksum artifacts only when they cross trust boundaries

**Severity / priority:** Low / P2, conditional.

Local artifacts are atomically published but are not authenticated ([`src/graphite/cli.py:188-194`](../../src/graphite/cli.py#L188), [`src/graphite/io.py:11-32`](../../src/graphite/io.py#L11)). That is appropriate for local working output. If graphs, reports, packages, or installers are distributed across a release, tenant, host, or administrative trust boundary, publish checksums and preferably signed provenance, and verify them before consumption.

### 6. GRA-SEC-R02 — Descriptor-based no-follow verification for hostile shared workspaces

**Severity / priority:** Low / P3.

Custom graph containment resolves the path before a later ordinary open ([`src/graphite/cli.py:582-602`](../../src/graphite/cli.py#L582)). In a hostile workspace writable by another principal, an attacker could attempt to swap a path component between validation and open. For that deployment model, open through a root directory descriptor with platform-appropriate no-follow semantics, validate the opened descriptor, and read from the same descriptor. Existing containment is adequate for the normal single-user repository model; this recommendation addresses residual TOCTOU risk.

## Model-agnostic assurance

`review-changes` calls only local path, Git, graph-validation, graph-query, and formatting logic ([`src/graphite/cli.py:627-659`](../../src/graphite/cli.py#L627)). It has no provider configuration, prompt, model, timestamp, agent SDK, or network transport in its contract. Stable sorting is applied to changes and evidence ([`src/graphite/review.py:247-249`](../../src/graphite/review.py#L247), [`src/graphite/review.py:466-483`](../../src/graphite/review.py#L466)), and JSON output is key-sorted ([`src/graphite/cli.py:655-656`](../../src/graphite/cli.py#L655)). The packet is therefore model/vendor/agent-agnostic and repeatable for the same repository, graph, arguments, and Git state.

## Verification record

- Pre-change full-suite baseline: **92 passed, 3 skipped**.
- Most recent independently observed focused HTML security run: **4 passed**.
- Most recent independently observed focused review run: **114 passed**.
- Current repository-wide Ruff recheck: **11 pre-existing findings** outside the touched implementation files; pre-change baseline was **13**.
- Task 5 documentation verification includes CLI help, real-line-reference/token checks, Markdown link inspection, and `git diff --check`.
- A post-integration full-suite result is **not** claimed in this report. **Task 6 is pending.**

## Acceptance checklist and recommendation

- [x] Repository-controlled graph JSON cannot terminate the data script through literal HTML delimiters.
- [x] Repository title and dynamic DOM text are rendered in context-appropriate safe forms.
- [x] HTML publication uses the shared atomic writer and has focused regression coverage.
- [x] Change discovery covers Git and explicit scopes with containment, protocol, and error hardening.
- [x] Review packets validate graph evidence and expose impact, likely tests, risk, blockers, warnings, and acceptance criteria deterministically.
- [x] High risk remains advisory; blocker failure is explicit opt-in behavior.
- [x] Custom graph reads are project-contained, 128 MiB bounded, sanitized, and checked against a sibling manifest.
- [ ] Complete Task 6 full-suite and integration verification before release.
- [ ] Triage residual P1 recommendations before managed or multi-tenant deployment.

**Recommendation:** Accept the fixed findings for integration after Task 6 completes successfully. Track the residual items as hardening work, with LLM egress governance, response-size bounds, and CI/dependency assurance prioritized before broader managed deployment.
