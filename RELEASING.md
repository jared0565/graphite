# Releasing Graphite

This runbook is a fail-closed checklist for maintainers. Stop when a prerequisite or
gate fails; investigate and repeat the complete affected gate after the fix.

## Current release model

Graphite releases are manual. The repository has no checked-in publication workflow
and, as of 2026-07-12, no established Git tags. This guide does not authorize a
publication. A release requires explicit maintainer authority, an approved destination,
and credentials configured separately in a secure release environment.

Releases are model-independent. No LLM or other model access is required, and model
output is not accepted as release evidence. Run all Graphite checks below with model
integration disabled.

## Preconditions

Use an isolated release environment with Python 3.11 or newer and Git. Confirm the
semantic version and scoped release notes have been agreed. Confirm the intended remote
and release branch. Use only trusted build tooling that has already been validated under
repository policy; do not install an unreviewed package during a release.

Run these read-only checks:

```text
git status --short
git branch --show-current
git remote -v
git fetch --tags origin
git rev-list --left-right --count origin/main...HEAD
```

The first command must produce no output. The branch and remote must match the approved
release target, fetching tags must succeed, and both divergence counts must be zero.
Replace `main` in the final command if the approved release branch differs. Stop for a
dirty worktree, unexpected branch or remote, fetch failure, or nonzero divergence. Do
not use a force reset or force push to make these checks pass.

## Prepare the version

Set the same version in both authoritative locations:

- `[project].version` in `pyproject.toml`
- `__version__` in `src/graphite/__init__.py`

Use the agreed semantic version without a leading `v` in both files. Review the complete
diff and confirm it contains only the intended version and release-documentation changes:

```text
git diff -- pyproject.toml src/graphite/__init__.py
git status --short
```

Stop if the values differ or unrelated changes are present.

## Verification gates

Run from the repository root in the isolated environment. Global Graphite options must
precede the subcommand; the repository path is the positional argument to `scan` and
`build`. Use disposable output and cache directories that are inside the repository and
excluded from the release commit:

```text
python -m ruff check .
python -m pytest
python -m graphite --help
python -m graphite scan --help
python -m graphite build --help
python -m graphite validate --help
python -m graphite --llm none --output-dir .release-check/graph-out --cache-dir .release-check/cache scan .
python -m graphite --llm none --output-dir .release-check/graph-out --cache-dir .release-check/cache build .
python -m graphite validate --graph-json .release-check/graph-out/graph.json --json
```

The build must deterministically produce valid JSON, Markdown, and HTML at
`.release-check/graph-out/graph.json`,
`.release-check/graph-out/GRAPH_REPORT.md`, and
`.release-check/graph-out/graph.html` without optional model access. Re-run the build in
a fresh disposable output directory and compare the generated content when checking
determinism; do not treat timestamps, environment-specific paths, or model output as
acceptable evidence. Inspect outputs for unsafe paths and unexpected repository data.

Stop on any lint, test, CLI, graph-validation, unsafe-path, nondeterminism, or artifact
failure. Generated directories are not source changes and must not be committed. Before
cleanup, resolve the candidate path, verify it is the intended disposable directory,
and confirm it remains inside this repository. Then remove it using the normal safe file
operation for the current shell or file manager. Never use a wildcard, repository root,
or an unverified computed path for cleanup.

## Build and inspect artifacts

Graphite uses the Hatchling backend declared in `pyproject.toml`; this release does not
add a build dependency. In the isolated release environment, use a trusted,
prevalidated Python build frontend that is already available. Do not install one during
the release merely to satisfy this step.

```text
python -m build --sdist --wheel
```

List `dist` and copy the exact wheel filename it contains. In the command below,
`VERSION` is prose notation: replace it with the approved version before running the
command; do not type angle brackets or shell redirection characters.

```text
python -m zipfile --list dist/graphite-VERSION-py3-none-any.whl
```

Inspect the wheel and source archive for the expected Python sources,
`graphite/ts_resolver.mjs`, and package metadata. Confirm they contain no credentials,
caches, build scratch data, or absolute developer paths.

Create a disposable `.release-smoke` virtual environment and activate it. Activation is
shell-specific:

```text
python -m venv .release-smoke
# POSIX shells:
. .release-smoke/bin/activate
# PowerShell:
.release-smoke\Scripts\Activate.ps1
```

While still at the Graphite repository root, install the exact wheel from `dist` without
any model extra:

```text
python -m pip install dist/graphite-VERSION-py3-none-any.whl
```

Create a small temporary repository inside the verified `.release-smoke` area,
initialize it with Git, and add one small Python source file. Change into that temporary
repository and run the installed commands with global options before their subcommands:

```text
graphite --help
graphite --llm none --output-dir graph-out --cache-dir cache scan .
graphite --llm none --output-dir graph-out --cache-dir cache build .
graphite validate --graph-json graph-out/graph.json --json
```

The install command uses the same `VERSION` replacement described above. Confirm the
smoke build creates valid JSON, Markdown, and HTML. For cleanup, first resolve and verify
that `.release-smoke` is the intended repository-contained disposable environment; then
remove it with a safe shell-appropriate operation. Do not issue a recursive deletion
against an unresolved or unexpected path.

## Tag and publish

Only continue after every gate passes and publication authority, destination, and secure
credentials are confirmed. In the commands below, replace `VERSION` with the approved
version and replace `main` if the approved release branch differs:

```text
git add pyproject.toml src/graphite/__init__.py
git commit -m "release: prepare vVERSION"
git tag -a vVERSION -m "Release vVERSION"
git push origin main
git push origin vVERSION
```

Review the staged diff before committing. Push the approved branch first and its single
annotated tag second; never force push either. Publication to PyPI or another index may
use only the separately approved mechanism from a secure environment with scoped
credentials. Never place tokens in command-line arguments, shell history, files in the
repository, or logs. This repository and guide do not claim that PyPI or any other index
is configured.

## Verify and recover

Verify that the approved remote shows the expected commit on the release branch and that
the annotated tag resolves to that commit. If an artifact was published, download it
from the destination into a new isolated environment, verify its digest and provenance
when the destination provides them, and repeat the wheel inspection and smoke test. Do
not use locally built artifacts as evidence that the published artifact is correct.

Recovery depends on how far the release progressed:

- Before pushing, a local tag may be deleted and recreated only after fixing the issue
  and repeating all gates.
- After pushing, never silently move a public tag. Coordinate a documented correction
  with maintainers and consumers.
- After publication, do not overwrite an immutable version. Use the destination's
  approved yank or revocation process when appropriate, then release a new patch version.
- For suspected credential or artifact compromise, stop immediately, revoke or rotate
  affected credentials, preserve audit evidence, notify the appropriate parties through
  approved private channels, and conduct an incident review before resuming.
