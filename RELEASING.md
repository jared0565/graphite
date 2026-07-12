# Releasing Graphite

This is a fail-closed maintainer checklist. Stop when a prerequisite or gate fails,
resolve the cause, and repeat the complete affected gate.

## Current release model

Graphite releases are manual. The repository has no checked-in publication workflow
and, as of 2026-07-12, no established Git tags. This guide does not authorize
publication. A release requires explicit maintainer authority, an approved destination,
and credentials configured separately in a secure release environment.

Releases are model-independent. No LLM or other model access is required, and model
output is not release evidence. Run Graphite checks with model integration disabled.

Maintain a release evidence record containing the approved version and destination,
commit SHA, annotated tag object SHA, artifact SHA256 hashes, build-tool versions,
dependency snapshot source and hashes, verification results, and publication result.
Keep credentials and other secrets out of that record.

## Preconditions

Use an isolated release environment with Python 3.11 or newer and Git. Confirm the
semantic version and scoped release notes have been agreed. Confirm the intended remote
and release branch. Build tools and dependencies must come from sources approved before
the release.

Run these non-destructive checks:

```text
git status --short
git branch --show-current
git remote -v
git fetch --tags origin
git rev-list --left-right --count origin/main...HEAD
```

The first command must produce no output. The branch and remote must match the approved
release target, fetching tags must succeed, and both divergence counts must be zero.
Replace `main` if the approved release branch differs. Stop for a dirty worktree,
unexpected branch or remote, fetch failure, or nonzero divergence. Do not force reset or
force push to make these checks pass.

## Prepare the version

Set the agreed semantic version, without a leading `v`, in both authoritative sources:

- `[project].version` in `pyproject.toml`
- `__version__` in `src/graphite/__init__.py`

Review the complete diff and confirm the values match and no unrelated changes exist:

```text
git diff -- pyproject.toml src/graphite/__init__.py
git status --short
```

## Verification gates

Run from the repository root in the isolated environment. Global Graphite options must
precede the subcommand; the repository path is the positional argument to `scan` and
`build`.

Prefer an approved external temporary workspace for generated output and cache. If
policy requires a repository-contained disposable directory, resolve its path first,
confirm its identity and containment, confirm it is absent or empty, and expect it to
appear in `git status --short` because these release directories are not guaranteed to
be ignored. After verification, clean it with a safe operation appropriate to the
current shell, re-check containment before any removal, and require a clean status. No
generic recursive-delete command is provided by this guide.

In the commands below, replace the literal `APPROVED-CHECK-DIR` with the resolved path
to a verified-fresh external directory named for this release. Do not add angle brackets.

```text
python -m ruff check .
python -m pytest
python -m graphite --help
python -m graphite scan --help
python -m graphite build --help
python -m graphite validate --help
python -m graphite --llm none --output-dir APPROVED-CHECK-DIR/graph-out --cache-dir APPROVED-CHECK-DIR/cache scan .
python -m graphite --llm none --output-dir APPROVED-CHECK-DIR/graph-out --cache-dir APPROVED-CHECK-DIR/cache build .
python -m graphite validate --graph-json APPROVED-CHECK-DIR/graph-out/graph.json --json
```

The build must create valid deterministic JSON, Markdown, and HTML at `graph.json`,
`GRAPH_REPORT.md`, and `graph.html` in that output directory without optional model
access. Build again into a different verified-fresh directory and compare outputs.
Inspect them for unexpected repository data or unsafe absolute paths. Stop on any lint,
test, CLI, graph-validation, unsafe-path, nondeterminism, or artifact failure.

## Build and inspect artifacts

Graphite uses the Hatchling backend declared in `pyproject.toml`. Before starting, the
isolated environment must already contain maintainer-approved, pinned or otherwise
recorded versions of the Python build frontend and Hatchling. Validate those versions
against repository policy and record them as release evidence. An approved,
hash-verified internal wheelhouse may be used to prepare the environment before the
release; never allow a release build to download unreviewed tooling.

Record the installed tool versions without changing the environment:

```text
python -c "import importlib.metadata as m; print('build', m.version('build')); print('hatchling', m.version('hatchling'))"
```

Choose a unique external output path named for the version. Resolve it and confirm it is
the intended location. It must be absent or a verified fresh empty directory. Stop if it
contains anything; never silently reuse or delete unknown content. Replace the literal
`APPROVED-ARTIFACT-DIR` below with that path and build without network-capable isolation:

```text
python -m build --no-isolation --sdist --wheel --outdir APPROVED-ARTIFACT-DIR
```

The build must produce exactly one wheel and one source distribution matching the
approved version, normally `graphite-VERSION-py3-none-any.whl` and
`graphite-VERSION.tar.gz`. Stop for extra, missing, or mismatched artifacts. List both
archives:

```text
python -m zipfile --list APPROVED-ARTIFACT-DIR/graphite-VERSION-py3-none-any.whl
python -m tarfile -l APPROVED-ARTIFACT-DIR/graphite-VERSION.tar.gz
```

Using an archive viewer in a separate verified-fresh inspection directory, inspect the
wheel's `METADATA` fields `Name`, `Version`, and every `Requires-Dist`, plus
`entry_points.txt`. Verify the wheel contains the expected `graphite` Python sources,
`graphite/ts_resolver.mjs`, and package metadata. Verify the sdist contains the expected
source and build files. Both archives must exclude credentials, caches, VCS files,
local configuration, scratch output, and absolute developer paths.

Compute and record a SHA256 hash for each exact artifact. The following shell-neutral
Python commands use the same literal `VERSION` substitution:

```text
python -c "import hashlib,pathlib; p=pathlib.Path('APPROVED-ARTIFACT-DIR/graphite-VERSION-py3-none-any.whl'); print(hashlib.sha256(p.read_bytes()).hexdigest(), p)"
python -c "import hashlib,pathlib; p=pathlib.Path('APPROVED-ARTIFACT-DIR/graphite-VERSION.tar.gz'); print(hashlib.sha256(p.read_bytes()).hexdigest(), p)"
```

Smoke testing requires a maintainer-approved, hash-verified dependency snapshot and
offline wheelhouse prepared before release. Record its source and hashes. No dependency
lockfile is currently claimed to be checked in; absence of an approved snapshot is a
stop condition. Create a fresh virtual environment in an approved external temporary
workspace, activate it using the current shell, and install dependencies offline. In
the example, replace every uppercase literal with its resolved approved path:

```text
python -m venv APPROVED-SMOKE-DIR
# POSIX shell activation:
. APPROVED-SMOKE-DIR/bin/activate
# PowerShell activation:
APPROVED-SMOKE-DIR\Scripts\Activate.ps1
python -m pip install --no-cache-dir --no-index --find-links APPROVED-WHEELHOUSE --require-hashes -r APPROVED-HASHED-REQUIREMENTS
python -m pip install --no-index --no-deps APPROVED-ARTIFACT-DIR/graphite-VERSION-py3-none-any.whl
python -m pip check
```

The dependency snapshot may be generated from an approved requirements or constraints
process, but every resolved artifact must be verified; broad minimum bounds from
`pyproject.toml` must not be resolved from a public index during release smoke testing.

From outside the Graphite source tree, verify the installed version and imports, inspect
the installed console-script registrations, and exercise the base `graphite` script:

```text
python -c "import graphite; print(graphite.__version__)"
python -c "import importlib.metadata as m; print(sorted(e.name for e in m.entry_points(group='console_scripts') if e.name.startswith('graphite')))"
graphite --help
```

The printed version must exactly match the approved version, and the registrations must
match the wheel's inspected `entry_points.txt`. Do not start `graphite-mcp` as a help
probe: it is a protocol server rather than a help-style CLI and requires the optional
`mcp` dependency. A separate protocol smoke test is permitted only when that dependency
is included in the approved, hash-verified snapshot.

Create a small temporary Git repository under the approved smoke workspace with one
small Python file. From that repository run:

```text
graphite --llm none --output-dir graph-out --cache-dir cache scan .
graphite --llm none --output-dir graph-out --cache-dir cache build .
graphite validate --graph-json graph-out/graph.json --json
```

Confirm valid JSON, Markdown, and HTML. Retain artifact hashes and results in the release
evidence. Clean disposable areas only after resolving and re-verifying their identity;
then require `git status --short` to be clean in the release checkout.

## Tag and publish

Explicitly enumerate every approved candidate file. Stage the two version files and each
approved release-note or documentation file by name; never use `git add .`. In the
example, `PATH-TO-APPROVED-RELEASE-NOTES` is a literal to replace, and may be repeated
for multiple approved files:

```text
git add pyproject.toml src/graphite/__init__.py PATH-TO-APPROVED-RELEASE-NOTES
git diff --cached --check
git diff --cached
git commit -m "release: prepare vVERSION"
git status --short
```

Review the staged diff before committing. After the commit, status must be empty. Record
the commit SHA. Immediately before tagging or pushing, repeat the remote, divergence,
and cleanliness checks:

```text
git fetch --tags origin
git rev-list --left-right --count origin/main...HEAD
git status --short
git rev-parse --verify --quiet refs/tags/vVERSION
git ls-remote --exit-code --tags origin refs/tags/vVERSION
```

Replace `main` if needed. Fetch must succeed and status must be empty. The left-hand
(remote-only) divergence count must be zero; the right-hand (local-only) count must equal
the explicitly reviewed unpublished release commits, normally the single release commit.
Any unexpected count is a stop condition. Exit 1 from the local-tag check means the tag
is absent; exit 0 means it exists, and any other result is inconclusive. For `git ls-remote
--exit-code`, exit 2 means no matching remote tag; exit 0 means the tag exists, while any
other failure is inconclusive. Stop unless absence is conclusively established. Also
confirm the version is available at the approved package destination using its approved
destination-specific mechanism; inability to prove availability is a stop condition.

Only then create the annotated tag, record its tag object SHA, and push the approved
branch followed by the tag:

```text
git tag -a vVERSION -m "Release vVERSION"
git rev-parse vVERSION
git push origin main
git push origin vVERSION
```

Never force push. Publication to PyPI or another index may use only a separately approved
mechanism from a secure environment with scoped credentials. Never place tokens in
command-line arguments, shell history, repository files, or logs. This repository and
guide do not claim that any package index is configured.

## Verify and recover

Verify the remote branch commit and annotated tag object against the release evidence.
If published, download the destination artifact into a new isolated environment, verify
its SHA256 digest and provenance when provided, and repeat archive inspection and smoke
testing. Do not use the local build as evidence that the published artifact is correct.

Recovery depends on the completed state:

- Before any push, a local tag may be deleted and recreated only after fixing the issue
  and repeating all gates.
- If the branch push succeeds but the tag push fails, verify the remote branch is the
  recorded commit, diagnose the tag failure, then push that same verified tag. Do not
  create a replacement commit or tag merely to retry transport.
- If the tag push succeeds but publication fails, verify the remote commit and tag object
  identity, fix the publication mechanism, and publish the already-recorded artifacts.
  Never recreate or move a public tag.
- If immutable publication succeeds but a release page or later verification fails, do
  not repeat the successful publication. Verify exact artifact identity and resume only
  the incomplete destination-specific step.
- After publication, never reuse a released version or overwrite it. Use the approved
  yank or revocation process when appropriate, then prepare a new patch release.
- For suspected credential or artifact compromise, stop, revoke or rotate affected
  credentials, preserve audit evidence, notify appropriate parties through approved
  private channels, and complete an incident review before resuming.
