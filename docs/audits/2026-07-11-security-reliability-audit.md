# Security and Reliability Audit — 2026-07-11

## Executive summary

This audit reviewed Graphite's generated HTML trust boundary, shared Git execution and ingestion boundary, artifact publication, and repository-level change-review evidence. Two high-severity security issues and one medium-severity reliability issue were confirmed and fixed; one informational capability gap with medium operational priority was also closed. Review and ingestion now share a deterministic Git runner that rejects repository-contained executables, scrubs inherited `GIT_*` redirection, bounds process output and cleanup, and fails closed for Git repositories. The review packet remains local, zero-LLM, and independent of model, vendor, and agent.

The changes materially reduce exposure; they do not make Graphite perfectly secure. Residual recommendations remain for optional LLM endpoint governance and response bounds, packet/impact/rendered-output limits, dependency/CI controls, existing lint debt, artifact authenticity when artifacts cross trust boundaries, injected atomic-write failure tests, descriptor-based no-follow reads, and external Git executable integrity in hostile shared workspaces. Fresh release verification for architectural code head `bd1e13d` passed on 2026-07-12.

## Scope and threat model

### Scope

- HTML generation and publication: `src/graphite/export/html.py`, `src/graphite/io.py`, and focused tests.
- Shared Git execution, repository ingestion, and deterministic change review: `src/graphite/git.py`, `src/graphite/ingest.py`, `src/graphite/review.py`, `src/graphite/cli.py`, and focused tests.
- Residual boundaries: optional LLM HTTP transport, review resource limits and output handling, dependency/CI posture, artifact distribution, and filesystem race resistance.

### Threat model

Repository content, repository-local executable names, graph artifacts, Git paths/status output, project names, custom graph locations, malformed files, and inherited `GIT_*` process variables are untrusted. An attacker may control a cloned repository or writable shared workspace and may attempt script injection, executable substitution, Git worktree/index/config redirection, evidence falsification, path escape, terminal/Markdown manipulation, memory exhaustion, information disclosure through errors, or a time-of-check/time-of-use (TOCTOU) filesystem race. Local users intentionally configuring an LLM endpoint establish a separate outbound network and credential trust boundary.

Out of scope were provider-side security, operating-system compromise, malicious Python interpreters or Git binaries, and the authenticity of artifacts distributed by an external release channel.

## Fixed findings

### High severity — immediate

#### 1. GRA-SEC-001 — Repository-controlled script/DOM execution in `graph.html`

**Status:** Fixed.

**Impact:** A malicious repository could place executable markup in graph data or the repository title and cause JavaScript execution in the local origin when a user opened the generated viewer, exposing data available to that origin and enabling actions with the viewer's browser privileges.

**Evidence and mitigation:** The viewer embeds graph data in a script context at [`src/graphite/export/html.py:46-48`](../../src/graphite/export/html.py#L46). Script-safe JSON serialization now encodes `&`, `<`, and `>` at [`src/graphite/export/html.py:208-214`](../../src/graphite/export/html.py#L208), the document title is HTML-escaped at [`src/graphite/export/html.py:231-237`](../../src/graphite/export/html.py#L231), and dynamic selection content is assembled with `textContent`, `createTextNode`, and `replaceChildren` at [`src/graphite/export/html.py:183-198`](../../src/graphite/export/html.py#L183). No dynamic `innerHTML` sink remains in the viewer.

**Tests:** Injection breakout, title escaping, text-only DOM rendering, template-token preservation, and JSON round-trip behavior are covered at [`tests/test_html_security.py:21-32`](../../tests/test_html_security.py#L21) and [`tests/test_html_security.py:54-72`](../../tests/test_html_security.py#L54).

#### 2. GRA-SEC-002 — Repository-controlled Git execution and evidence redirection

**Status:** Fixed.

**Impact:** In an untrusted repository or inherited hostile process environment, an unqualified Git launch could execute a repository-controlled `git` program, while inherited `GIT_DIR`, `GIT_WORK_TREE`, index/object/config, executable-path, or related `GIT_*` variables could redirect discovery or falsify the reported change scope. The former ingestion fallback could also continue after Git enumeration failed and read files that Git ignore rules would have excluded. These paths could run attacker-controlled code with the reviewer's privileges, ingest unintended sensitive files, or cause downstream review and release decisions to rely on attacker-selected evidence.

**Evidence and mitigation:** The shared runner resolves an absolute Git executable only from absolute `PATH` directories, rejects candidates contained by the project root, and launches the resolved external path with `shell=False` ([`src/graphite/git.py:45-84`](../../src/graphite/git.py#L45), [`src/graphite/git.py:177-208`](../../src/graphite/git.py#L177)). Every invocation removes inherited variables whose names begin with `GIT_` case-insensitively, sets `GIT_OPTIONAL_LOCKS=0`, disables terminal prompts, adds `--no-optional-locks`, and forces `core.fsmonitor=false` ([`src/graphite/git.py:68-84`](../../src/graphite/git.py#L68), [`src/graphite/git.py:211-219`](../../src/graphite/git.py#L211)). Review discovery imports and uses that runner at [`src/graphite/review.py:12-18`](../../src/graphite/review.py#L12) and [`src/graphite/review.py:63-123`](../../src/graphite/review.py#L63); repository ingestion uses the same boundary at [`src/graphite/ingest.py:10-18`](../../src/graphite/ingest.py#L10) and [`src/graphite/ingest.py:160-208`](../../src/graphite/ingest.py#L160).

Freshness has no bare-Git bypass: `_check_status` re-enumerates through `collect_files`, including custom-graph sibling-manifest checks ([`src/graphite/cli.py:215-250`](../../src/graphite/cli.py#L215), [`src/graphite/cli.py:613-624`](../../src/graphite/cli.py#L613)). Explicit-file review freshness is covered by a shared-boundary integration test at [`tests/test_review.py:980-1025`](../../tests/test_review.py#L980).

**Tests:** Shared runner tests cover bounded streaming, overflow termination, timeouts, bounded cleanup when descendants hold pipes, external executable selection, project-contained rejection, POSIX execute permission, environment isolation, optional-lock/fsmonitor controls, and sanitized errors ([`tests/test_git_security.py:148-231`](../../tests/test_git_security.py#L148), [`tests/test_git_security.py:234-407`](../../tests/test_git_security.py#L234)). Review and ingestion integration contracts are covered at [`tests/test_review.py:204-452`](../../tests/test_review.py#L204) and [`tests/test_hardening.py:57-188`](../../tests/test_hardening.py#L57).

### Medium severity — high priority

#### 3. GRA-REL-001 — Non-atomic HTML publication

**Status:** Fixed.

Interrupted writes could previously leave a truncated or partially replaced viewer. The shared helper writes a same-directory temporary file, flushes and `fsync`s it, atomically replaces the destination, and removes the temporary file on failure at [`src/graphite/io.py:11-26`](../../src/graphite/io.py#L11). HTML publication uses that helper at [`src/graphite/export/html.py:238-244`](../../src/graphite/export/html.py#L238). Successful replacement and the absence of leftover temporary files after success are tested at [`tests/test_reliability.py:76-82`](../../tests/test_reliability.py#L76), and the HTML exporter delegation is tested at [`tests/test_html_security.py:35-51`](../../tests/test_html_security.py#L35). Injected write, flush, `fsync`, and replace failures are not covered by those tests.

### Informational severity — medium operational priority

#### 4. GRA-OPS-001 — Missing repository-level review evidence

**Status:** Fixed.

This was an operational capability gap, not a security defect: the repository lacked one command that joined the actual change scope to graph freshness, validation, impact, likely tests, risk signals, and acceptance criteria. Git and explicit discovery are implemented at [`src/graphite/review.py:40-190`](../../src/graphite/review.py#L40); deterministic packet construction and advisory risk are implemented at [`src/graphite/review.py:244-348`](../../src/graphite/review.py#L244); and Markdown rendering validates evidence fields before output at [`src/graphite/review.py:351-411`](../../src/graphite/review.py#L351). The CLI contains custom graph paths within the project, caps custom graph input at 128 MiB, and emits JSON or Markdown at [`src/graphite/cli.py:582-659`](../../src/graphite/cli.py#L582).

Determinism and the absence of LLM/model/timestamp fields are asserted at [`tests/test_review.py:956-977`](../../tests/test_review.py#L956). The implementation neither selects nor calls an agent or model; it is an evidence contract usable by any reviewer or automation.

## Quality hardening completed

- **Shared Git process boundary:** Review and ingestion use `GitRunner`, which caps stdout at 16 MiB, streams with `Popen`, applies per-command timeouts, kills on overflow/timeout, and bounds reader/process cleanup to 0.1 seconds ([`src/graphite/git.py:12-14`](../../src/graphite/git.py#L12), [`src/graphite/git.py:45-164`](../../src/graphite/git.py#L45)). Git review status and ingestion file records are each capped at 100,000 ([`src/graphite/review.py:26-27`](../../src/graphite/review.py#L26), [`src/graphite/review.py:126-166`](../../src/graphite/review.py#L126), [`src/graphite/ingest.py:15-16`](../../src/graphite/ingest.py#L15), [`src/graphite/ingest.py:190-208`](../../src/graphite/ingest.py#L190)). Tests cover overflow, timeout, and bounded cleanup at [`tests/test_git_security.py:148-231`](../../tests/test_git_security.py#L148), plus review record limits at [`tests/test_review.py:448-452`](../../tests/test_review.py#L448).
- **Fail-closed ingestion:** A Git repository aborts on unavailable/failed Git, timeout, oversized or malformed output, invalid UTF-8, or unsafe records rather than falling back to a filesystem walk; nested paths inside another Git repository are explicitly unsupported ([`src/graphite/ingest.py:160-208`](../../src/graphite/ingest.py#L160)). Non-Git directories alone use the walk fallback. Integration tests verify fail-closed errors and nested-root rejection at [`tests/test_hardening.py:99-188`](../../tests/test_hardening.py#L99).
- **Ingestion path boundary:** Git records must pass both POSIX and native path checks, resolve strictly inside the repository, and survive a final containment check before reading or hashing ([`src/graphite/ingest.py:135-157`](../../src/graphite/ingest.py#L135), [`src/graphite/ingest.py:237-278`](../../src/graphite/ingest.py#L237)). External symlink targets are rejected, while safe internal symlinks keep their listed alias identity instead of collapsing to the target path. Tests cover invalid UTF-8, Windows-native escapes, POSIX literal backslashes, external symlink rejection, the final boundary, and internal identity at [`tests/test_hardening.py:302-477`](../../tests/test_hardening.py#L302).
- **Dynamic exclusions and eligible-file caps:** Resolved output and cache directories inside the root are excluded by component prefix for Git and walk enumeration; custom artifacts therefore do not self-ingest or make a just-built custom graph immediately stale ([`src/graphite/ingest.py:95-132`](../../src/graphite/ingest.py#L95), [`src/graphite/ingest.py:211-254`](../../src/graphite/ingest.py#L211)). `cfg.max_files` is applied after eligibility filtering for Git enumeration and during eligible walk iteration, so skipped artifacts do not consume the useful-file budget ([`src/graphite/ingest.py:190-208`](../../src/graphite/ingest.py#L190), [`src/graphite/ingest.py:237-254`](../../src/graphite/ingest.py#L237)). Tests cover eligible-file limits and dynamic custom locations at [`tests/test_hardening.py:191-299`](../../tests/test_hardening.py#L191) and [`tests/test_hardening.py:480-595`](../../tests/test_hardening.py#L480).
- **Packet boundary:** Discovery mode, statuses, project labels, paths, graph-derived paths, control/format Unicode, source-file fields, and formatter shapes are validated before output ([`src/graphite/review.py:414-564`](../../src/graphite/review.py#L414), [`src/graphite/review.py:633-748`](../../src/graphite/review.py#L633)).
- **Graph boundary:** Custom graph input must resolve within the project root and is capped at 128 MiB; malformed and recursive JSON failures are sanitized, graph bundles are structurally validated, and the custom graph's sibling manifest drives freshness ([`src/graphite/cli.py:63-64`](../../src/graphite/cli.py#L63), [`src/graphite/cli.py:582-624`](../../src/graphite/cli.py#L582), [`src/graphite/review.py:441-512`](../../src/graphite/review.py#L441)). Containment, size, parsing, and custom-freshness behavior are tested at [`tests/test_review.py:1077-1187`](../../tests/test_review.py#L1077) and [`tests/test_review.py:1230-1286`](../../tests/test_review.py#L1230).
- **Exit semantics:** For a successfully constructed packet, risk does not affect exit status; `--fail-on-blocker` makes evidence blockers return `1` ([`src/graphite/cli.py:655-659`](../../src/graphite/cli.py#L655)). Invalid inputs and operational errors return `1` independently; a high-risk packet without blockers remains successful ([`tests/test_review.py:1190-1198`](../../tests/test_review.py#L1190)).

## Residual recommendations

### 1. GRA-SEC-R01 — Optional LLM endpoint egress and credential policy

**Severity / priority:** Medium / P1 before managed or multi-tenant deployment.

The generic OpenAI-compatible adapter intentionally accepts a user-configured base URL and sends the configured bearer credential to its `/chat/completions` endpoint ([`src/graphite/llm.py:43-77`](../../src/graphite/llm.py#L43)). This is an intended endpoint feature, not an exploitable vulnerability by itself. In centrally managed environments, add an explicit egress/endpoint policy: HTTPS enforcement for remote hosts, an allowlist or administrator approval, loopback/private-address rules appropriate to deployment, redirect policy, scoped credentials, and auditable provider selection. When optional enrichment is enabled, the prompt builder does not intentionally include source-file contents, but it transmits graph metadata, filenames, identifiers, labels, and analysis summaries to the configured provider ([`src/graphite/llm.py:275-313`](../../src/graphite/llm.py#L275)).

### 2. GRA-REL-R01 — Bound optional LLM HTTP response bodies

**Severity / priority:** Medium / P1.

Successful LLM responses are currently read without a byte limit at [`src/graphite/llm.py:316-323`](../../src/graphite/llm.py#L316). Add a configurable maximum response size, stream at most `limit + 1` bytes, reject oversized and non-object JSON responses, and apply equivalent bounds to HTTP error bodies. This limits memory consumption from a faulty or untrusted configured endpoint.

### 3. GRA-REL-R02 — Cap packet, impact, and rendered-output size

**Severity / priority:** Medium / P1.

Git stdout and Git record counts are now bounded, but explicit change scopes, graph-derived matched/impact/test lists, packet fields, and JSON/Markdown serialization have no aggregate cardinality, per-field length, or total output-byte cap ([`src/graphite/review.py:244-348`](../../src/graphite/review.py#L244), [`src/graphite/review.py:351-411`](../../src/graphite/review.py#L351), [`src/graphite/review.py:466-497`](../../src/graphite/review.py#L466), [`src/graphite/cli.py:655-658`](../../src/graphite/cli.py#L655)). Add limits for explicit changes, graph-derived evidence entries, individual strings, and total serialized output; reject or explicitly truncate with machine-readable notices. This reduces residual memory and log-amplification risk from very large or adversarial inputs. The 16 MiB Git stdout, 100,000-record, and 128 MiB graph caps do not bound these later structures.

### 4. GRA-SUP-R01 — Add dependency vulnerability/provenance scanning and CI

**Severity / priority:** Medium / P1 before release automation.

Runtime and development dependencies are declared at [`pyproject.toml:7-23`](../../pyproject.toml#L7), but this audit found no repository `.github/workflows` directory and no checked-in dependency vulnerability or provenance policy. Add CI that installs from a reproducible lock/constraints strategy, runs tests and Ruff, scans known vulnerabilities, produces an SBOM, and records build provenance. Pin CI actions and protect update review; do not automatically apply unreviewed dependency fixes.

### 5. GRA-QUAL-R01 — Retire repository-wide pre-existing Ruff debt

**Severity / priority:** Low / P2.

The pre-change baseline contained 13 Ruff findings. Files touched by the original security/review implementation were clean in its focused lint check. A historical repository-wide recheck before the shared Git architecture reported 11 pre-existing findings outside those then-touched files: unused imports at `src/graphite/cache.py:6`, `src/graphite/cluster.py:4`, `src/graphite/daemon.py:15`, `src/graphite/extract/ast.py:12`, `src/graphite/graph.py:5`, `src/graphite/mcp_server.py:5`, `src/graphite/replacement_audit.py:4`, `src/graphite/windows_task.py:5`, `src/graphite/windows_task.py:9`, and `tests/test_replacement_audit.py:9`, plus an unused local at `src/graphite/mcp_server.py:15`. Re-establish the current baseline during pending verification, clear remaining debt in a separate behavior-preserving cleanup, and make Ruff a CI gate. The removed `start_daemon_task` item is not an unresolved finding.

### 6. GRA-SUP-R02 — Sign or checksum artifacts only when they cross trust boundaries

**Severity / priority:** Low / P2, conditional.

Local artifacts are atomically published but are not authenticated ([`src/graphite/cli.py:188-194`](../../src/graphite/cli.py#L188), [`src/graphite/io.py:11-32`](../../src/graphite/io.py#L11)). That is appropriate for local working output. If graphs, reports, packages, or installers are distributed across a release, tenant, host, or administrative trust boundary, publish checksums and preferably signed provenance, and verify them before consumption.

### 7. GRA-SEC-R02 — Descriptor-based no-follow verification for hostile shared workspaces

**Severity / priority:** Low / P3.

Custom graph containment resolves the path before a later ordinary open ([`src/graphite/cli.py:582-602`](../../src/graphite/cli.py#L582)). In a hostile workspace writable by another principal, an attacker could attempt to swap a path component between validation and open. For that deployment model, open through a root directory descriptor with platform-appropriate no-follow semantics, validate the opened descriptor, and read from the same descriptor. Existing containment is adequate for the normal single-user repository model; this recommendation addresses residual TOCTOU risk.

### 8. GRA-REL-R03 — Inject atomic-publication failures in tests

**Severity / priority:** Low / P2.

The atomic helper contains cleanup logic for write, flush, `fsync`, and replace failures ([`src/graphite/io.py:11-26`](../../src/graphite/io.py#L11)), but current tests assert only successful replacement and no leftover temporary file after success ([`tests/test_reliability.py:76-82`](../../tests/test_reliability.py#L76)). Add injected-failure tests for each stage and assert that the prior destination remains intact where the platform contract permits, temporary files are removed, and the original exception propagates.

### 9. GRA-SEC-R03 — Pin and protect the external Git executable trust boundary

**Severity / priority:** Low / P3; raise for hostile shared build hosts.

The resolver excludes relative, current-directory, and project-contained candidates, but the selected external `PATH` directory remains a user/operating-system trust boundary ([`src/graphite/git.py:177-208`](../../src/graphite/git.py#L177)). A principal able to modify that directory or replace the executable after resolution could still substitute Git; checking and later executing the resolved path also leaves a post-resolution TOCTOU window. On managed or hostile shared hosts, prefer an explicitly configured and deployment-pinned absolute Git path, protect the executable and parent directories with OS ACL/ownership policy, and verify identity through an opened handle or platform-equivalent mechanism when available. Monitor package provenance and upgrades. Normal single-user installations may continue to rely on protected system package locations.

## Model-agnostic assurance

`review-changes` calls only local path, shared Git, graph-validation, graph-query, and formatting logic ([`src/graphite/cli.py:627-659`](../../src/graphite/cli.py#L627)). It has no provider configuration, prompt, model, timestamp, agent SDK, or network transport in its contract. Stable sorting is applied to changes and evidence ([`src/graphite/review.py:261-263`](../../src/graphite/review.py#L261), [`src/graphite/review.py:480-497`](../../src/graphite/review.py#L480)), and JSON output is key-sorted ([`src/graphite/cli.py:655-656`](../../src/graphite/cli.py#L655)). The packet is therefore model/vendor/agent-agnostic and repeatable for the same repository, graph, arguments, Git state, and trusted Git installation.

## Verification record

- **Pre-feature baseline (historical):** on Windows with Python 3.14.5, `python -m pytest -q` produced **92 passed, 3 skipped**.
- **Feature commit `6835227` (historical):** on Windows with Python 3.14.5, `python -m pytest -q tests/test_html_security.py` produced **4 passed**, and `python -m pytest -q tests/test_review.py` produced **114 passed**.
- **Pre-architecture commit `001d246` (historical):** focused tests reported **122 passed, 1 skipped**; the full suite reported **214 passed, 4 skipped**; touched Ruff, build, validation, live blocking review, diff, and status checks were recorded as successful. These results predate the shared Git/ingestion architecture ending at `bd1e13d` and are not release evidence for the current code.
- **Ruff history:** `python -m ruff check . --output-format concise` reported **13 findings** before the feature changes and **11 pre-existing findings** at code head `bd1e13d`, outside the touched implementation files.

### Final release verification — 2026-07-12, code head `bd1e13d`

Fresh verification ran on Windows with Python 3.14.5. Commands used `$env:PYTHONPATH = "src"` and `$env:GRAPHITE_LLM = "none"`; pytest used fresh writable bases under `F:\tmp`. The live Git check used an isolated external HOME with an explicit global `safe.directory`, because the verification sandbox SID differs from the worktree owner and the production runner deliberately removes inherited `GIT_CONFIG_*` overrides.

- Focused security/review/ingestion tests exited `0`: **168 passed, 3 skipped in 16.63s**.
- The complete pytest suite exited `0`: **254 passed, 6 skipped in 36.29s**.
- Touched-file Ruff exited `0`: **All checks passed**.
- `git diff --check 3718a7b..HEAD` and the working-tree diff check exited `0` with no output.
- A zero-LLM build exited `0` and wrote the Markdown, JSON, and HTML graph artifacts.
- Graph validation exited `0`: **1981 nodes / 3717 edges, 0 warnings**.
- The live blocker-enforced review exited `0`: valid JSON, **0 blockers**, **0 warnings**, `stale=false`, `CONFIRM_CLEAN`, no absolute worktree path, and no forbidden model/time keys.
- `git status --short` exited `0` with no output; ignored generated artifacts did not dirty the worktree.

## Acceptance checklist and recommendation

- [x] Repository-controlled graph JSON cannot terminate the data script through literal HTML delimiters.
- [x] Repository title and dynamic DOM text are rendered in context-appropriate safe forms.
- [x] HTML publication uses the shared atomic writer and has focused regression coverage.
- [x] Review and ingestion use the same hardened Git runner with inherited `GIT_*` removal, optional locks disabled, and fsmonitor disabled.
- [x] Git repository ingestion fails closed on Git/protocol errors and unsupported nested roots; contained non-Git directories retain the filesystem fallback.
- [x] Git/native path validation and the final resolved-path boundary reject external escapes while preserving safe internal alias identity.
- [x] Resolved output/cache exclusions prevent custom artifacts from self-ingestion and immediate stale status.
- [x] Git stdout, Git record counts, process timeouts, and cleanup waits are bounded; `cfg.max_files` counts eligible files.
- [x] Review packets validate graph evidence and expose impact, likely tests, risk, blockers, warnings, and acceptance criteria deterministically.
- [x] For a successfully constructed packet, high risk remains advisory; evidence-blocker failure is explicit opt-in behavior. Invalid inputs and operational errors fail independently.
- [x] Custom graph reads are project-contained, capped at 128 MiB, sanitized, and checked against a sibling manifest.
- [x] Local review output is documented as sensitive repository metadata that callers must protect.
- [x] Focused tests passed with a fresh writable base: `python -m pytest -q tests/test_html_security.py tests/test_git_security.py tests/test_hardening.py tests/test_review.py --basetemp F:\tmp\graphite-shared-git-focused-final`.
- [x] The full suite passed with a fresh writable base: `python -m pytest -q --basetemp F:\tmp\graphite-shared-git-full-final`.
- [x] Ruff passed on touched implementation and test files: `python -m ruff check src/graphite/git.py src/graphite/ingest.py src/graphite/review.py src/graphite/cli.py src/graphite/export/html.py src/graphite/io.py tests/test_git_security.py tests/test_hardening.py tests/test_review.py tests/test_html_security.py tests/test_reliability.py`.
- [x] A build passed with LLM use disabled: `$env:PYTHONPATH = "src"; $env:GRAPHITE_LLM = "none"; python -m graphite build .`.
- [x] The generated graph validated: `$env:PYTHONPATH = "src"; $env:GRAPHITE_LLM = "none"; python -m graphite validate`.
- [x] The live blocking review produced the inspected clean packet: `$env:PYTHONPATH = "src"; $env:GRAPHITE_LLM = "none"; python -m graphite review-changes . --json --fail-on-blocker`.
- [x] The patch and worktree checks passed: `git diff --check` and `git status --short`.
- [ ] Triage residual P1 recommendations before managed or multi-tenant deployment.

**Recommendation:** Accept the fixed findings for integration based on the fresh release evidence above. This recommendation does not claim perfect security. Track the residual items as hardening work, with LLM egress governance, LLM and review output bounds, and CI/dependency assurance prioritized before broader managed deployment.
