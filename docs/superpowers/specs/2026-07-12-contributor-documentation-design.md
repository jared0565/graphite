# Contributor Documentation Design

**Date:** 2026-07-12  
**Status:** Design approved; written spec pending review  
**Scope:** Contributor-facing documentation only

## Objective

Give new and returning contributors one reliable path from repository checkout to a reviewable change and, for maintainers, a safe manual release. The documentation must describe Graphite as it exists today, preserve its model-agnostic design, and avoid claiming automation or operational guarantees that the repository does not provide.

## Decision

Use a small documentation set with clear ownership instead of expanding the README into a monolith:

- `README.md` remains the product and operator entry point. It gains a concise contributor section linking to the detailed guides.
- `CONTRIBUTING.md` owns development setup, day-to-day engineering workflow, tests, linting, compatibility expectations, security practices, and pull-request conventions.
- `ARCHITECTURE.md` owns system boundaries, internal data flow, module responsibilities, trust boundaries, artifacts, extension points, and failure behavior.
- `RELEASING.md` owns the current manual release process, verification gates, version synchronization, artifact checks, tagging, publication prerequisites, and rollback guidance.

This split keeps each guide focused, makes responsibilities discoverable from conventional filenames, and limits documentation drift caused by duplicated instructions.

## Alternatives Considered

### Expand only `README.md`

This would maximize initial discoverability but produce a long document mixing user, operator, contributor, architecture, and maintainer concerns. It would also make release details too prominent for most readers. Rejected because it increases maintenance cost and makes the primary usage path harder to scan.

### Create a full documentation site

A generated site could provide navigation and richer diagrams, but the repository has no documentation build or hosting system. Adding one would introduce dependencies, deployment work, and maintenance that are not justified by this documentation-only scope. Rejected under YAGNI.

### Selected: conventional root guides with a README index

Root-level contributor files are visible on Git hosting platforms, work offline, require no build step, and can evolve independently. This is the safest practical fit for the repository's current maturity.

## Information Architecture

### README changes

Add a short `Contributing and project internals` section that:

1. Welcomes contributions without duplicating setup instructions.
2. Links directly to `CONTRIBUTING.md`, `ARCHITECTURE.md`, and `RELEASING.md`.
3. Identifies the intended audience for each guide.

The README remains the canonical location for product purpose, installation, command usage, output formats, and operator-oriented workflows.

### `CONTRIBUTING.md`

The contributor guide will cover:

- Supported Python baseline and repository prerequisites.
- Editable development installation using the existing `dev` extra, with the optional `mcp` extra documented separately.
- Repository layout and links to the architecture guide.
- A minimal first-change workflow: create a branch, install, test, edit, run focused checks, run the full suite, and prepare a pull request.
- Test conventions, including focused tests during development and the full suite before review.
- Ruff checks using the repository's existing configuration.
- Security-first contribution expectations: treat repository content and Git metadata as untrusted, avoid shell interpolation, avoid secret commits, preserve path validation, and report suspected vulnerabilities privately rather than in a public issue.
- Model-agnostic compatibility rules: deterministic core behavior must not require an LLM vendor; integrations must remain optional and must use explicit boundaries rather than embedding provider assumptions in core logic.
- Branch, commit, documentation, backward-compatibility, and pull-request conventions.
- A reviewer checklist with functional, test, security, documentation, and compatibility gates.

Commands will match the existing `pyproject.toml` and repository scripts. The guide will not require a package manager, service, or CI system that is absent from the repository.

### `ARCHITECTURE.md`

The architecture guide will explain Graphite through its observable pipeline:

```text
repository input
    -> filesystem scan
    -> language extraction
    -> symbol/import resolution
    -> graph construction
    -> analysis and review
    -> deterministic artifacts and exports
```

It will map these stages to existing modules and describe:

- Public entry points: CLI and optional MCP server.
- Core analysis boundary and the direction of dependencies.
- Language adapters and how new language support should be isolated.
- Graph model, node/edge identity, and deterministic serialization expectations.
- Git execution boundary, including argument-vector execution, timeouts, output limits, and untrusted repository state.
- LLM boundary: Graphite produces deterministic review context and does not select, invoke, or depend on a model provider in its core workflow.
- Artifact lifecycle, validation expectations, and safe handling of generated HTML/JSON/Markdown outputs.
- Failure domains and the intended fail-closed behavior for invalid paths, malformed data, command failures, and incomplete artifacts.
- Supported extension points and invariants contributors must preserve.

The guide will use a compact text diagram so it remains accessible in terminals and review diffs. It will distinguish documented current behavior from future recommendations.

### `RELEASING.md`

The release guide will document the repository's current manual process. It will not imply that automated publishing exists.

The process will include:

1. Confirm the intended release version and change scope.
2. Start from a clean branch synchronized with the target remote.
3. Synchronize the version in `pyproject.toml` and `src/graphite/__init__.py`.
4. Run Ruff and the full test suite.
5. Run representative CLI checks and validate generated artifacts without requiring an LLM.
6. Build source and wheel artifacts using the existing Hatchling backend, subject to the required build tooling being available.
7. Inspect package contents and smoke-test installation in an isolated environment.
8. Commit the release, create an annotated tag, and push only after all gates pass.
9. Publish to a package index only when maintainers have configured credentials and an approved publishing mechanism.
10. Verify the remote tag/release and document rollback or forward-fix actions if validation fails.

The guide will state that there are currently no repository tags and no checked-in release automation, so the first release must establish and record the chosen tag and publication conventions. It will avoid embedding credentials or recommending command-line secrets.

## Trust Boundaries and Safety

Documentation commands can be copied into high-trust developer or maintainer environments, so every command must be conservative and explicit.

- No destructive cleanup, forced Git updates, or implicit credential handling.
- No dependency installation beyond the project's declared extras; contributors must follow repository package-validation policy before installing new dependencies.
- No release step continues after a dirty worktree, test failure, invalid artifact, version mismatch, or remote divergence.
- Generated content and repository paths remain untrusted inputs.
- LLMs are optional consumers of deterministic outputs, never a release or correctness authority.
- Security issues are directed to private maintainer contact where available; because no `SECURITY.md` or published security address currently exists, the guide must not invent one. It will advise contributors not to disclose exploitable details publicly and ask maintainers to establish a private channel.

## Consistency and Drift Controls

The implementation will minimize duplicated commands and link to the owning guide wherever possible. Review will verify:

- Every referenced file and command exists or is explicitly described as a maintainer prerequisite.
- Python and package versions reflect repository metadata.
- Release version locations are complete and consistent.
- The architecture module map matches the current source tree.
- Relative Markdown links resolve from their containing files.
- No guide claims CI, release automation, PyPI publication, security contact details, or model-provider integration that is not present.

Future structural changes should update the owning guide in the same pull request. Changes to public commands or outputs should update the README; module or trust-boundary changes should update `ARCHITECTURE.md`; workflow changes should update `CONTRIBUTING.md`; and versioning or publication changes should update `RELEASING.md`.

## Validation Strategy

Documentation validation will be proportional to the change:

1. Scan new files for unfinished sections, draft markers, and unresolved template text.
2. Check all relative links and referenced repository paths.
3. Compare documented commands with CLI help, `pyproject.toml`, and the source tree.
4. Execute safe, read-only help/version commands where appropriate.
5. Run Ruff and the full test suite to ensure documentation edits did not disturb tracked configuration or source files.
6. Review the diff for accidental secrets, misleading guarantees, unsafe shell instructions, and duplicated or contradictory guidance.

## Acceptance Criteria

The documentation is ready for acceptance when:

- A new contributor can identify prerequisites, install the declared development environment, run focused and full checks, and prepare a compliant pull request using `CONTRIBUTING.md`.
- A maintainer can trace the scan-to-export pipeline, identify module and trust boundaries, and locate supported extension points using `ARCHITECTURE.md`.
- A maintainer can perform a gated manual release without relying on undocumented automation using `RELEASING.md`.
- The README links to all three guides without duplicating their detailed content.
- All statements match the current repository, all links and paths resolve, and no model vendor is required or privileged.
- Security and failure-stop conditions are explicit, including dirty state, test failures, artifact failures, version drift, and remote divergence.

## Recommendation for Review and Acceptance

Reviewers should accept this documentation design if the file ownership boundaries are clear, the manual release scope accurately reflects current capabilities, and the model-agnostic and security invariants are sufficiently explicit. During implementation review, use the acceptance criteria above as the approval checklist and request evidence for link validation, command verification, lint, and tests before merging.

## Out of Scope

- Adding CI/CD or package-index publishing automation.
- Creating a hosted documentation site.
- Introducing dependencies or changing runtime behavior.
- Defining a new governance, licensing, or vulnerability-disclosure policy without maintainer decisions.
- Selecting or integrating an LLM provider.
