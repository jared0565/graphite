# Contributor Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add accurate, security-first contributor, architecture, and manual-release guides, linked from the README and protected by lightweight documentation-contract tests.

**Architecture:** Keep `README.md` as the product entry point and assign one concern to each conventional root guide: development workflow to `CONTRIBUTING.md`, internals to `ARCHITECTURE.md`, and maintainer releases to `RELEASING.md`. Add standard-library-only pytest checks that enforce the required files, headings, README navigation, and valid relative Markdown links without introducing a documentation toolchain.

**Tech Stack:** Markdown, Python 3.11+, pytest, Ruff, Hatchling packaging metadata, Git

---

## File Structure

- Create `CONTRIBUTING.md`: contributor prerequisites, setup, engineering workflow, security and model-agnostic conventions, and review checklist.
- Create `ARCHITECTURE.md`: current scan-to-export pipeline, module ownership, trust boundaries, failure domains, artifact lifecycle, and extension invariants.
- Create `RELEASING.md`: fail-closed manual release procedure, version synchronization, build and smoke checks, tagging, publication prerequisites, and recovery.
- Create `tests/test_documentation.py`: executable contracts for guide presence, required sections, README navigation, version-file references, and relative links.
- Modify `README.md`: concise navigation to the three detailed guides.
- Modify `docs/superpowers/specs/2026-07-12-contributor-documentation-design.md`: mark the design implemented only after all acceptance checks pass.

### Task 1: Contributor Guide and README Navigation

**Files:**
- Create: `tests/test_documentation.py`
- Create: `CONTRIBUTING.md`
- Modify: `README.md` after the `Principles` section and before `Installation`

- [ ] **Step 1: Write the failing contributor-document contract**

Create `tests/test_documentation.py` with the repository-root helper and assertions below:

```python
"""Contracts that keep contributor-facing documentation discoverable and current."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_document(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_contributing_guide_has_required_sections() -> None:
    guide = read_document("CONTRIBUTING.md")

    for heading in (
        "# Contributing to Graphite",
        "## Development setup",
        "## Engineering workflow",
        "## Testing and quality gates",
        "## Security expectations",
        "## Model-agnostic design",
        "## Contribution conventions",
        "## Pull request checklist",
    ):
        assert heading in guide


def test_readme_links_to_contributor_guides() -> None:
    readme = read_document("README.md")

    assert "[Contributor guide](CONTRIBUTING.md)" in readme
    assert "[Architecture guide](ARCHITECTURE.md)" in readme
    assert "[Release guide](RELEASING.md)" in readme
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
python -m pytest tests/test_documentation.py -q
```

Expected: failure with `FileNotFoundError` for `CONTRIBUTING.md`.

- [ ] **Step 3: Write `CONTRIBUTING.md`**

Use the required headings from the test and include these exact operational requirements:

- Opening: Graphite accepts focused, reviewable changes that preserve deterministic, local-first behavior and secure handling of untrusted repositories.
- `Development setup`:
  - Python 3.11 or newer, Git, and an isolated virtual environment.
  - Clone and enter the repository; do not hard-code the current machine's `F:/Projects` path.
  - Install the declared developer extra with `python -m pip install -e ".[dev]"` only after applying the repository's package-validation policy.
  - For MCP work, use `python -m pip install -e ".[dev,mcp]"`; state that MCP is optional.
  - Do not add or install dependencies without validating the exact package name first.
- `Engineering workflow`:
  - Start from a clean, current base branch and create a focused topic branch.
  - Read `ARCHITECTURE.md` before changing module boundaries or trust boundaries.
  - Add or update a focused test first, run it to see the intended failure, implement the smallest complete change, then rerun focused checks.
  - Update the owning document in the same change when commands, architecture, workflows, or release behavior change.
- `Testing and quality gates` with copyable commands:

```bash
python -m pytest tests/test_relevant_area.py -q
python -m ruff check .
python -m pytest -q
python -m graphite --help
```

  Explain that the focused test path must be replaced by the relevant existing test file, while Ruff and the complete suite are required before review.
- `Security expectations`:
  - Treat repository files, filenames, symlinks, Git metadata, generated graph data, and model output as untrusted.
  - Pass subprocess arguments as argument vectors; never interpolate repository-controlled values into a shell command.
  - Preserve path-containment checks, bounded reads and outputs, timeouts, atomic writes, output encoding, and safe error redaction.
  - Never commit credentials or include secrets in fixtures, logs, examples, generated artifacts, or prompts.
  - Avoid publishing exploitable vulnerability details in a public issue; because the project has no published private security contact, ask maintainers to establish a private channel without inventing an address.
- `Model-agnostic design`:
  - Core scan, graph, validation, review, and export behavior must remain deterministic and usable with LLM use disabled.
  - Provider integrations remain optional adapters behind `CompletionProvider`-style interfaces.
  - Do not make a vendor SDK, model name, hosted endpoint, or API key mandatory.
  - Treat model output as untrusted enrichment, never as correctness evidence or authorization.
  - New integrations require explicit configuration, bounded timeouts, sanitized errors, and tests using fakes rather than live services.
- `Contribution conventions`:
  - One coherent concern per pull request; descriptive branch names; imperative commit subjects.
  - Preserve public CLI and artifact compatibility unless the change explicitly documents a migration.
  - Add tests for behavior changes and update relevant README/guide sections.
  - Do not mix generated artifacts, unrelated formatting, or local environment files into commits.
- `Pull request checklist`: clean diff, focused and full tests, Ruff, security review, documentation, backward compatibility, deterministic/model-agnostic behavior, and no secrets.

- [ ] **Step 4: Add concise README navigation**

Insert this section after `Principles` and before `Installation`:

```markdown
## Contributing and project internals

- [Contributor guide](CONTRIBUTING.md) — development setup, testing, security expectations, and pull-request conventions.
- [Architecture guide](ARCHITECTURE.md) — pipeline, module boundaries, artifacts, extension points, and failure behavior.
- [Release guide](RELEASING.md) — maintainer verification, packaging, tagging, publication, and recovery steps.
```

- [ ] **Step 5: Run the contributor test and confirm the remaining expected failure**

Run:

```bash
python -m pytest tests/test_documentation.py -q
```

Expected: `test_contributing_guide_has_required_sections` passes; `test_readme_links_to_contributor_guides` passes even though the architecture and release targets are created in later tasks.

- [ ] **Step 6: Commit the contributor guide and navigation**

```bash
git add README.md CONTRIBUTING.md tests/test_documentation.py
git commit -m "docs: add contributor development guide"
```

### Task 2: Internal Architecture Guide

**Files:**
- Modify: `tests/test_documentation.py`
- Create: `ARCHITECTURE.md`

- [ ] **Step 1: Add the failing architecture-document contract**

Append this test to `tests/test_documentation.py`:

```python
def test_architecture_guide_has_pipeline_and_boundaries() -> None:
    guide = read_document("ARCHITECTURE.md")

    for heading in (
        "# Graphite architecture",
        "## System context",
        "## Processing pipeline",
        "## Module map",
        "## Trust boundaries",
        "## Artifacts and state",
        "## Failure behavior",
        "## Extension points and invariants",
    ):
        assert heading in guide

    assert "repository input" in guide
    assert "model provider" in guide.lower()
```

- [ ] **Step 2: Run the architecture test and verify it fails**

Run:

```bash
python -m pytest tests/test_documentation.py::test_architecture_guide_has_pipeline_and_boundaries -q
```

Expected: failure with `FileNotFoundError` for `ARCHITECTURE.md`.

- [ ] **Step 3: Write `ARCHITECTURE.md` system context and pipeline**

Use the tested headings. Define Graphite as a local Python application with two public entry points: `graphite`/`python -m graphite` through `src/graphite/cli.py`, and the optional `graphite-mcp` server through `src/graphite/mcp_server.py`. Include this text pipeline:

```text
repository input
    -> collect and classify files
    -> parse and extract symbols/imports/calls
    -> resolve cross-file identities
    -> construct and analyze the directed graph
    -> validate and render deterministic artifacts
    -> optionally enrich a report through a configured model adapter
```

For every stage, name current modules and contracts:

- Collection: `ingest.py`, `config.py`, and `git.py`; contained normalized paths and bounded Git execution.
- Extraction: `extract/ast.py`, `cache.py`, `ts_bridge.py`, and `ts_resolver.mjs`; per-language Tree-sitter parsing with optional compiler-backed TypeScript resolution.
- Resolution: `resolve.py`; `SourceIndex` and normalized project-relative identities.
- Graph: `graph.py`, `cluster.py`, `analyze.py`, and `query.py`; NetworkX directed graph with deterministic JSON conversion and seeded community detection.
- Validation and review: `validation.py`, `context.py`, and `review.py`; schema/invariant checks and deterministic change evidence.
- Export: `export/json.py`, `export/md.py`, `export/html.py`, and `io.py`; encoded output and atomic writes.
- Operations: `watch.py`, `daemon.py`, `daemon_health.py`, `bootstrap.py`, `init.py`, `windows_task.py`, and `windows_startup.py`; lifecycle features around the core pipeline.
- Optional enrichment: `llm.py`; configured adapter boundary after deterministic analysis.

- [ ] **Step 4: Write the module map and dependency rules**

Add a Markdown table with columns `Area`, `Primary modules`, `Responsibility`, and `May depend on`. State these directional rules:

- CLI and MCP adapt user/tool input into core operations; core modules do not import UI entry points.
- Exporters consume graph/analysis data; extraction must not depend on exporters.
- Optional LLM enrichment consumes deterministic reports; graph creation and validation must not depend on a model provider.
- OS-specific startup modules remain outside the portable analysis core.
- External process execution goes through bounded adapters such as `GitRunner` or explicit subprocess boundaries.

- [ ] **Step 5: Document trust boundaries, artifacts, and failure behavior**

Describe four trust boundaries with controls:

1. Repository boundary: hostile paths, content, symlinks, encodings, and size; normalize, contain, bound, and skip unsafe input.
2. Process boundary: Git, Node/TypeScript, and Windows task commands; argument vectors, isolated environment where implemented, timeouts, output caps, and sanitized errors.
3. Artifact/browser boundary: graph JSON and HTML can contain repository-controlled strings; validate structures, JSON-encode script data, escape display content, avoid absolute paths, and write atomically.
4. Model/network boundary: disabled by default; explicit configuration, no required vendor, timeouts, secret redaction, untrusted output, and no authority over validation or release decisions.

List current artifact/state responsibilities: graph bundle and reports, manifest/freshness data, `.graphite-cache`, daemon status, and generated platform instruction files. Tell contributors to use project-relative identifiers, deterministic ordering, schema validation, and atomic replacement.

State fail-closed behavior: invalid or escaping paths are rejected/skipped; malformed graph bundles fail validation; bounded subprocess failures surface typed/sanitized errors; incomplete artifacts are not accepted; optional enrichment failure must not corrupt deterministic core output.

- [ ] **Step 6: Document supported extension points and invariants**

Cover:

- New languages: classification in `ingest.py`, parser/extractor support in `extract/ast.py`, resolution rules in `resolve.py` when needed, and fixtures/tests for symbols, imports, calls, malformed input, and deterministic output.
- New exporters: consume validated structures, escape for the destination context, use atomic writes, and avoid environment metadata.
- New queries/analysis: deterministic ordering, bounded results, project-node filtering, and stable JSON-compatible output.
- New model adapters: implement the provider protocol without entering core graph construction; explicit configuration, timeouts, sanitized errors, and fake-backed tests.
- New process integrations: no shell interpolation, bounded resources, typed failure modes, and platform isolation.

Declare preserved invariants: local-first and zero-LLM core, deterministic artifacts for identical inputs/configuration, normalized project-relative paths, no secrets/system metadata in outputs, validation before consumption, and backward-compatible public CLI/artifacts unless migration is explicit.

- [ ] **Step 7: Run the architecture contract and Ruff**

Run:

```bash
python -m pytest tests/test_documentation.py::test_architecture_guide_has_pipeline_and_boundaries -q
python -m ruff check tests/test_documentation.py
```

Expected: both commands pass.

- [ ] **Step 8: Commit the architecture guide**

```bash
git add ARCHITECTURE.md tests/test_documentation.py
git commit -m "docs: describe internal architecture"
```

### Task 3: Manual Release Guide

**Files:**
- Modify: `tests/test_documentation.py`
- Create: `RELEASING.md`

- [ ] **Step 1: Add the failing release-document contract**

Append this test to `tests/test_documentation.py`:

```python
def test_release_guide_has_gates_and_version_sources() -> None:
    guide = read_document("RELEASING.md")

    for heading in (
        "# Releasing Graphite",
        "## Current release model",
        "## Preconditions",
        "## Prepare the version",
        "## Verification gates",
        "## Build and inspect artifacts",
        "## Tag and publish",
        "## Verify and recover",
    ):
        assert heading in guide

    assert "pyproject.toml" in guide
    assert "src/graphite/__init__.py" in guide
    assert "model" in guide.lower()
```

- [ ] **Step 2: Run the release test and verify it fails**

Run:

```bash
python -m pytest tests/test_documentation.py::test_release_guide_has_gates_and_version_sources -q
```

Expected: failure with `FileNotFoundError` for `RELEASING.md`.

- [ ] **Step 3: Write `RELEASING.md` current model and preconditions**

Use the tested headings and begin with explicit facts:

- Releases are currently manual; the repository has no checked-in publication workflow and no established Git tags as of 2026-07-12.
- The guide does not authorize publication by itself; maintainers need release authority, an approved destination, and separately configured credentials.
- LLM access is neither required nor accepted as release evidence.

Under `Preconditions`, require an isolated release environment, Python 3.11+, Git, a clean worktree, the intended remote/branch, an agreed semantic version, release notes/change scope, and trusted build tooling validated under repository policy. Provide these safe inspection commands:

```bash
git status --short
git branch --show-current
git remote -v
git fetch --tags origin
git rev-list --left-right --count origin/main...HEAD
```

Explain that the release stops on uncommitted changes, unexpected branch/remote, fetch failure, or non-zero divergence. Do not provide force-reset or force-push instructions.

- [ ] **Step 4: Document version preparation and verification gates**

Require the same release version in:

- `[project].version` in `pyproject.toml`.
- `__version__` in `src/graphite/__init__.py`.

Require a diff review after editing and these gates:

```bash
python -m ruff check .
python -m pytest -q
python -m graphite --help
python -m graphite scan .
python -m graphite build .
python -m graphite validate graph-out/graph.json
```

Tell the implementer to verify the actual CLI syntax and generated graph path against `python -m graphite --help` during execution and correct the guide if the current command contract differs. Require validation of deterministic JSON/Markdown/HTML artifacts without enabling optional LLM enrichment. Stop on any lint, test, CLI, graph validation, unsafe-path, or artifact-integrity failure.

- [ ] **Step 5: Document build, inspection, smoke installation, tagging, and publication**

State that the existing backend is Hatchling and that no new build dependency is added by this documentation change. In an isolated release environment with a trusted, prevalidated Python build frontend available, use:

```bash
python -m build --sdist --wheel
```

Then require:

```bash
python -m zipfile --list dist/graphite-<version>-py3-none-any.whl
python -m venv .release-smoke
```

Explain that `<version>` is replaced with the agreed version and that activation is shell-specific. In the smoke environment, install the built wheel without model extras, then run `python -m graphite --help` and a small temporary-repository scan/build/validate flow. Inspect artifacts for source files, `ts_resolver.mjs`, metadata, absence of secrets/local cache files, and absence of absolute developer paths. Remove `.release-smoke` only after resolving it inside the repository and confirming it is the intended disposable environment.

Provide the gated Git sequence only after every check passes:

```bash
git add pyproject.toml src/graphite/__init__.py
git commit -m "release: prepare v<version>"
git tag -a "v<version>" -m "Graphite v<version>"
git push origin main
git push origin "v<version>"
```

State that the branch name must be replaced if the approved release branch is not `main`. Publication to PyPI or another index occurs only through an approved mechanism with credentials supplied by secure environment configuration; do not show tokens on command lines and do not claim PyPI is configured.

- [ ] **Step 6: Document verification and recovery**

Require checking the remote commit/tag, downloading the published artifact from its destination when applicable, verifying its digest/provenance if the destination provides it, and repeating the isolated smoke test.

Recovery rules:

- Before pushing a tag: fix the issue, rerun every gate, and recreate only the local tag if necessary.
- After pushing but before publication: do not silently move or overwrite the public tag; coordinate a documented correction.
- After publication: do not overwrite an immutable released version; revoke/yank only through approved index controls and issue a new patch version.
- For suspected credential or artifact compromise: stop, rotate/revoke credentials through the provider, preserve audit evidence, notify maintainers privately, and publish a corrected version only after incident review.

- [ ] **Step 7: Run the release contract**

Run:

```bash
python -m pytest tests/test_documentation.py::test_release_guide_has_gates_and_version_sources -q
```

Expected: pass.

- [ ] **Step 8: Commit the release guide**

```bash
git add RELEASING.md tests/test_documentation.py
git commit -m "docs: add manual release runbook"
```

### Task 4: Relative-Link and Documentation Drift Controls

**Files:**
- Modify: `tests/test_documentation.py`
- Modify if validation finds drift: `README.md`, `CONTRIBUTING.md`, `ARCHITECTURE.md`, `RELEASING.md`

- [ ] **Step 1: Add a relative Markdown-link validator**

Add `import re` and `from urllib.parse import unquote` to `tests/test_documentation.py`, then add:

```python
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
DOCUMENTS = ("README.md", "CONTRIBUTING.md", "ARCHITECTURE.md", "RELEASING.md")


def test_relative_markdown_links_resolve() -> None:
    missing: list[str] = []

    for document_name in DOCUMENTS:
        document = ROOT / document_name
        for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>").split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (document.parent / unquote(target)).resolve()
            try:
                resolved.relative_to(ROOT.resolve())
            except ValueError:
                missing.append(f"{document_name}: link escapes repository: {raw_target}")
                continue
            if not resolved.exists():
                missing.append(f"{document_name}: missing link target: {raw_target}")

    assert not missing, "\n".join(missing)
```

- [ ] **Step 2: Run the link test and inspect any failure**

Run:

```bash
python -m pytest tests/test_documentation.py::test_relative_markdown_links_resolve -q
```

Expected: pass. If it fails, correct only inaccurate local links; do not weaken containment or existence checks.

- [ ] **Step 3: Add a no-draft-markers contract for the new guides**

Append:

```python
def test_contributor_guides_have_no_draft_markers() -> None:
    forbidden = ("T" + "ODO", "T" + "BD", "F" + "IXME")

    for document_name in DOCUMENTS[1:]:
        document = read_document(document_name)
        for marker in forbidden:
            assert marker not in document
```

The split string literals prevent this test file from failing its own marker scan.

- [ ] **Step 4: Run all documentation contracts and Ruff**

Run:

```bash
python -m pytest tests/test_documentation.py -q
python -m ruff check tests/test_documentation.py
```

Expected: all documentation tests pass and Ruff reports no errors.

- [ ] **Step 5: Commit the drift controls**

```bash
git add README.md CONTRIBUTING.md ARCHITECTURE.md RELEASING.md tests/test_documentation.py
git commit -m "test: enforce contributor documentation contracts"
```

### Task 5: Repository-Wide Verification and Acceptance Record

**Files:**
- Modify: `docs/superpowers/specs/2026-07-12-contributor-documentation-design.md`

- [ ] **Step 1: Verify documented CLI commands against current help**

Run:

```bash
python -m graphite --help
python -m graphite validate --help
python -m graphite review-changes --help
```

Expected: commands referenced in the guides exist and their documented argument order matches help output. Correct documentation inaccuracies before continuing.

- [ ] **Step 2: Review all documentation for current repository truth**

Compare the guides against:

- `pyproject.toml` for Python, extras, scripts, build backend, and version.
- `src/graphite/__init__.py` for the second version location.
- `src/graphite/` for module ownership and entry points.
- `.github/` and Git tags for any release automation or established tag convention.
- README behavior claims and existing tests for model-provider boundaries.

Expected: no invented CI, publication target, private security contact, tag history, credential flow, or model requirement.

- [ ] **Step 3: Run whitespace, secret, and unsafe-instruction review**

Run:

```bash
git diff --check HEAD~4..HEAD
rg -n "api[_-]?key|secret|token|password|force-push|reset --hard" README.md CONTRIBUTING.md ARCHITECTURE.md RELEASING.md
```

Expected: `git diff --check` passes. Review every search hit in context; examples must not contain real credentials, and the guides must not recommend destructive Git recovery.

- [ ] **Step 4: Run the complete verification suite**

Run:

```bash
python -m ruff check .
python -m pytest -q
```

Expected: Ruff passes and the full pytest suite passes. Existing environment-only warnings may be reported, but test failures block acceptance.

- [ ] **Step 5: Mark the approved design implemented**

Change the spec header from:

```markdown
**Status:** Design approved; written spec pending review  
```

to:

```markdown
**Status:** Implemented and verified  
```

Only make this change after Steps 1–4 pass.

- [ ] **Step 6: Commit the acceptance record**

```bash
git add docs/superpowers/specs/2026-07-12-contributor-documentation-design.md
git commit -m "docs: record contributor guide acceptance"
```

- [ ] **Step 7: Present the review recommendation**

Report the created guides, test and lint evidence, current manual-release limitation, and the model-agnostic/security invariants. Recommend acceptance only if the complete verification suite passed and `git status --short` shows no unintended files.

## Plan Self-Review

- Spec coverage: Tasks 1–3 implement every guide and README ownership boundary; Task 4 implements link and drift validation; Task 5 covers repository-truth review, full verification, and acceptance evidence.
- Scope: Documentation and documentation-contract tests only; no runtime changes, dependencies, CI/CD, hosted site, security policy, or model integration.
- Safety: Release instructions stop on dirty state, divergence, test failures, invalid artifacts, or credential concerns and contain no force-update workflow.
- Consistency: Required headings in tests exactly match the planned guides; version locations match `pyproject.toml` and `src/graphite/__init__.py`; module names match the current source tree.
