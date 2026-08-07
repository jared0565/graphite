# Releasing Graphite

This is a fail-closed maintainer checklist. Stop when a prerequisite or gate fails,
resolve the cause, and repeat the complete affected gate.

## Current release model

Graphite releases are manual. The repository has no checked-in publication workflow.
The first annotated tag, `v0.2.0`, was created on 2026-08-07; it was created outside
the sequence in "Tag and publish" below, so it carries no release evidence and must not
be cited as a released artifact. This guide does not authorize publication. A release
requires explicit maintainer authority, an approved destination, and credentials
configured separately in a secure release environment.

Note what a release is FOR here, because the answer is not only publication. Every
consumer on this machine imports Graphite from one editable install pointed at the
development tree, so a saved file reaches all of them immediately and there is no
previous version to return to. A retained, hash-recorded artifact is what makes that
recoverable: rollback is "reinstall the last known-good wheel", which is only possible
if that wheel was built and kept. Building into a scratch directory and discarding it
satisfies the gate below and leaves the gap open.

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

The command examples target PowerShell and POSIX-like shells only where their semantics
are shared; activation commands remain shell-specific. Before use, the operator must
replace every path token with a resolved absolute path using normalized forward slashes.
This applies to `CHECK_DIR`, `ARTIFACT_DIR`, `WHEELHOUSE_DIR`,
`DEPENDENCY_MANIFEST`, `SMOKE_DIR`, `WHEEL_PATH`, `SDIST_PATH`, and
`RELEASE_NOTES_PATH`. Substituted paths must exclude whitespace, newlines or other
control characters, and shell metacharacters. Validate every substituted value, retain
the quotes shown below, and stop if a path violates this policy. These examples do not
claim universal shell neutrality.

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

There is exactly ONE authoritative source. Set the agreed semantic version, without a
leading `v`, in `__version__` in `src/graphite/__init__.py`.

`pyproject.toml` declares `dynamic = ["version"]` and reads that file through
`[tool.hatch.version]`. It must NOT carry a `[project].version` field. Re-adding one is
not a harmless duplicate: Hatchling refuses to build a field declared both statically
and dynamically, so the next build aborts in `_get_version` rather than producing a
mismatched artifact.

The single source is deliberate. Every consumer here imports from one shared editable
install, and `importlib.metadata` reports whatever was written into the installed
metadata at install time — so a version set only in `pyproject.toml` reaches a consumer
only when someone reinstalls, which routine work never does. `graphite --version` reads
`graphite.__version__` from the source tree for that reason, and reports a
`stale-install` line when the installed metadata disagrees.

Review the complete diff and confirm no unrelated changes exist, and that
`pyproject.toml` still declares the version dynamic:

```text
git diff -- pyproject.toml src/graphite/__init__.py
git status --short
python -c "import tomllib, pathlib; d = tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8')); assert 'version' not in d['project'], 'static [project].version present; the build will abort'; assert 'version' in d['project'].get('dynamic', []), 'version is not declared dynamic'; print('version source OK:', d['tool']['hatch']['version']['path'])"
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

In the commands below, `CHECK_DIR` is the policy-compliant path to a verified-fresh
external directory named for this release. Do not add angle brackets.

```text
python -m ruff check .
python -m pytest
python -m graphite --help
python -m graphite scan --help
python -m graphite build --help
python -m graphite validate --help
python -m graphite --llm none --output-dir "CHECK_DIR/graph-out" --cache-dir "CHECK_DIR/cache" scan .
python -m graphite --llm none --output-dir "CHECK_DIR/graph-out" --cache-dir "CHECK_DIR/cache" build .
python -m graphite validate --graph-json "CHECK_DIR/graph-out/graph.json" --json
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
contains anything; never silently reuse or delete unknown content. Substitute that path
for `ARTIFACT_DIR` and build without network-capable isolation:

```text
python -m build --no-isolation --sdist --wheel --outdir "ARTIFACT_DIR"
```

The build must produce exactly one wheel and one source distribution matching the
approved version, normally `graphite-VERSION-py3-none-any.whl` and
`graphite-VERSION.tar.gz`. Stop for extra, missing, or mismatched artifacts. Resolve the
actual files as the policy-compliant `WHEEL_PATH` and `SDIST_PATH`, then list both:

```text
python -m zipfile --list "WHEEL_PATH"
python -m tarfile -l "SDIST_PATH"
```

Using an archive viewer in a separate verified-fresh inspection directory, inspect the
wheel's `METADATA` fields `Name`, `Version`, and every `Requires-Dist`, plus
`entry_points.txt`. Verify the wheel contains the expected `graphite` Python sources,
`graphite/ts_resolver.mjs`, and package metadata. Verify the sdist contains the expected
source and build files. Both archives must exclude credentials, caches, VCS files,
local configuration, scratch output, and absolute developer paths.

Compute and record a SHA256 hash for each exact artifact. Paths are quoted shell
arguments and are never embedded in Python source:

```text
python -c "import hashlib, pathlib, sys; p = pathlib.Path(sys.argv[1]); print(hashlib.sha256(p.read_bytes()).hexdigest(), p)" "WHEEL_PATH"
python -c "import hashlib, pathlib, sys; p = pathlib.Path(sys.argv[1]); print(hashlib.sha256(p.read_bytes()).hexdigest(), p)" "SDIST_PATH"
```

Smoke testing requires a maintainer-approved, hash-verified dependency snapshot and
offline wheelhouse prepared before release. Record its source and hashes. No dependency
lockfile is currently claimed to be checked in; absence of an approved snapshot is a
stop condition. Create a fresh virtual environment in an approved external temporary
workspace, activate it using the current shell, and install dependencies offline. In
the example, use the validated path tokens defined above:

```text
python -m venv "SMOKE_DIR"
# POSIX shell activation:
. "SMOKE_DIR/bin/activate"
# PowerShell activation:
& "SMOKE_DIR/Scripts/Activate.ps1"
python -m pip install --no-cache-dir --no-index --find-links "WHEELHOUSE_DIR" --require-hashes -r "DEPENDENCY_MANIFEST"
python -m pip install --no-index --no-deps "WHEEL_PATH"
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
example, `RELEASE_NOTES_PATH` follows the path policy above and may be repeated for
multiple approved files. Omit it only when there are no approved release-note or
documentation changes:

```text
git add pyproject.toml src/graphite/__init__.py "RELEASE_NOTES_PATH"
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
confirm through the approved destination-specific mechanism that the approved version is
not already published or reserved and is eligible for publication; inability to prove
all three conditions is a stop condition.

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

## Retention and rollback

Every build that was ever deployed must be retained, with its SHA256 and an
evidence record, in a store **outside** this repository. Artifacts kept inside
it are destroyed by `git clean -xdf` and absent from a fresh clone — precisely
when a rollback is wanted. On this machine that store is
`F:/Projects/.graphite-releases/`, one directory per version plus an `index.md`
recording what is currently deployed.

This is not bookkeeping. Consumers import Graphite from a single editable
install pointed at the development tree, so a saved file is live for all of them
with no build and no boundary. Rollback means "reinstall the last known-good
wheel", and that is possible only for a wheel somebody kept. A build directed at
a scratch directory satisfies the gate above and leaves the gap open.

Rolling back, in an environment that already has the runtime dependencies:

```text
python -m pip install --force-reinstall --no-deps "STORE_DIR/graphite-VERSION-py3-none-any.whl"
```

Returning to live development:

```text
python -m pip install -e REPO_DIR
```

Verify each artifact's SHA256 against its evidence record before installing it,
and confirm the switch by the resolved module path rather than by the version
string — both states report the same version, so the version cannot distinguish
them:

```text
python -c "import graphite; print(graphite.__file__)"
```

Prove a rollback path in a throwaway virtual environment before relying on it,
and prove the return as well. An untested recovery step is not a recovery step.

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
