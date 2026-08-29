# Graphite 1.0 — production-grade PRD

Date: 2026-08-29. Status: decisions approved by the maintainer; implementation
tracked in `docs/superpowers/plans/2026-08-29-production-grade.md`.

## 1. Summary

Graphite is a local-first code knowledge graph with a daemon, git hooks, an MCP
server and a release pipeline that already publishes digest-verified wheels to
PyPI. It self-declares `Development Status :: 4 - Beta` and "pre-1.0, minor
versions may break things". This PRD defines what "production-grade" means for
graphite as a set of machine-checkable declaration criteria, and the work that
makes each criterion true. The declaration is **release 1.0.0**.

The organising principle, proven in the 2026-08-10 readiness round: a claim that
CI cannot turn red is not a claim. Every criterion below is either a gate that
fails on regression or a document that a test keeps in lockstep with code.

## 2. Measured baseline (2026-08-29, commit `b0ccc55`, graphite 0.5.3)

Everything here was measured in-session, not inherited.

| Area | State |
|---|---|
| CI gating | One cell gates merges: `windows-latest` + Python 3.14. Nine `portability` legs are `continue-on-error` at job level (all nine passed on run 33217126451). Ubuntu 3.13 and macOS 3.13 absent. |
| Security | aramid runs only in local git hooks: the pre-commit gate is gitleaks + ruff security rules + shadow (measured `--all`: 16 s), the pre-push gate is gitleaks + semgrep + the full pytest suite through `../.venvs/graphite-dev/Scripts/python.exe`. CI ruff selects `E4,E7,E9,F`. No `SECURITY.md`, no dependabot, no `CODEOWNERS`, no issue templates. `publish.yml` sets `attestations: false` because artifacts are built on the maintainer's machine. |
| Coverage | 82 % branch-inclusive (3024 passed, 49 skipped, 712 s, Windows, dev venv, 2026-08-29; 81 % on 2026-08-10). No `fail_under`, not collected in CI. |
| Typing | No checker configured; no `py.typed`. Baselines: pyright 146 errors / 106 files; mypy (`--ignore-missing-imports`) 128 errors. |
| Daemon supervision | Windows only (`daemon-install-windows`, `daemon-install-startup-windows`). No systemd or launchd path. |
| Docs | README is 701 lines and carries the whole reference; exit codes are scattered over four README paragraphs; no configuration or CLI reference page; no compatibility/deprecation statement. |
| Metadata | `Author` empty in the wheel; classifiers claim Linux and macOS. |
| Scale | Declared limit: `max_graph_bytes` = 128 MiB. No build-time or memory benchmark. This repo (345 files) builds cold in 10.6 s, warm 7.1 s, 9.0 MB graph. |
| Line endings | Index is 100 % LF (`.gitattributes`: `* text=auto eol=lf`), but 40 working-copy files are CRLF — including `pyproject.toml`, `README.md` and 14 `src/graphite/*.py` files, all of which hatchling packages from the working tree. |
| Reproducibility | **Fails across OS today.** v0.5.3 rebuilt under WSL Ubuntu from a clean LF checkout with `hatchling==1.32.0` pinned (`uv build`): wheel `e71ff6c7…c529`, sdist `828a9db0…9796`, versus the retained Windows-built `e399ccbf…609c` / `5d536d9a…c54d`. The CRLF working tree is the expected cause; the build frontend (`uv build` vs `build`) is a confound to isolate in WS-E. |
| Tracker | 0 open issues. 3 unmerged branches (`diag/29-*`, `experiment/29-*`, `fix/27-mcp2-port`). |

## 3. Goals and non-goals

**Goals.** Make every declaration criterion in §4 true, then release 1.0.0.

**Non-goals** (explicitly out of scope; each is either not a production-grade
blocker or already tracked elsewhere):

- Deleting the three unmerged branches (housekeeping; needs the maintainer's ask).
- #60's live prediction (successor after a mid-build stop) — it is an
  observation to wait for, not work.
- The venv-launcher TTL fallback and #37's per-runner probe repetition — named
  residuals with measured 0/400 impact.
- LLM-tier features, the adaptive router, mutation testing budget — orthogonal.
- Performance *optimisation*. §WS-H measures and declares; it does not speed up.

## 4. Declaration criteria (definition of done)

1.0.0 may be cut only when all of these hold on `main`:

| # | Criterion | How it is checked |
|---|---|---|
| D1 | All 12 OS × Python cells (windows/ubuntu/macos × 3.11–3.14) gate merges and are green. | `ci.yml` has no `continue-on-error` on test legs; branch protection is not required, but the run on the release commit shows 12/12 `success`. |
| D2 | The same security scanner configuration that gates local commits and pushes runs in CI, unmodified, and blocks. | A `security` job runs both `aramid check --gate pre-commit --all --strict` and `--gate pre-push --all --strict` against the committed `aramid.toml`; each JSON shows the expected tools in `tools_ran`; a planted known-positive turns it red (proven once, recorded). |
| D3 | Coverage has an enforced floor. | Three OS legs upload coverage data; a `coverage` job combines them and fails under `fail_under`. The floor is the integer part of the first combined measurement and may only rise. |
| D4 | The type gate is clean and the package is typed. | `python -m mypy` over `src/graphite` exits 0 in the lint job; `src/graphite/py.typed` ships; classifier `Typing :: Typed`. |
| D5 | Daemon supervision installs on Windows, Linux and macOS. | `daemon-install-linux` (systemd user unit) and `daemon-install-macos` (launchd agent) exist with the same status/uninstall surface as Windows; generated units pass `systemd-analyze verify` / `plutil -lint` on the CI legs of their platform. |
| D6 | A compatibility contract and deprecation policy are published. | `docs/compatibility.md` names every stable surface; `CHANGELOG.md` no longer says minor versions may break; classifier `5 - Production/Stable`. |
| D7 | Reference documentation exists and cannot drift. | `docs/reference/cli.md` is generated from the argparse tree and a test fails when stale; `docs/reference/configuration.md` lists every `Config` field/env var and a test fails when one is missing; `docs/reference/exit-codes.md` exists and README links to all three. |
| D8 | Governance surfaces exist. | `SECURITY.md`, `.github/CODEOWNERS`, `.github/ISSUE_TEMPLATE/`, `.github/dependabot.yml`, `authors` in `pyproject.toml`. |
| D9 | Scale is measured and declared. | `capabilities` reports `supported_repo_files`; a CI benchmark builds a synthetic repo and records wall time, peak RSS, nodes and edges as an artifact, failing only on a catastrophic budget; `docs/benchmarks.md` records one real-repo measurement. |
| D10 | Release artifacts are built in CI with provenance. | `publish.yml` checks out the approved tag, builds with pinned tools, refuses unless both digests equal the approved ones, attaches the artifacts to the GitHub Release and publishes with `attestations: true`. Two independent CI builds of the same commit are byte-identical (recorded). |
| D11 | 1.0.0 is released, verified from the index, and deployed. | `scripts/verify_published_release.py 1.0.0` 6/6 arms (the sixth is PEP 740 provenance, added in WS-E); store `index.md` "Currently deployed" reads 1.0.0; channel round posted. Publication itself is the maintainer's dispatch. |

## 5. Work streams

Each stream is one branch merged into `main` with a merge commit, in the
repo's existing style (`Merge <branch>: <what changed> (PRD WS-x)`), and each
ends with CI green on the merge.

### WS-A — Every matrix cell gates (D1)

**Change.** Fold `portability` into `test`: a 12-cell `strategy.matrix`
(`os` × `python`), `fail-fast: false`, no `continue-on-error`, every leg runs
`python -m pytest -q --timeout=120 --timeout-method=thread` so a hang names
itself. Lint moves to its own `lint` job (ubuntu, 3.12): ruff and, from WS-D,
mypy. The `workflow_dispatch` `portability` input and its billing comment go
(public repository: minutes are free). The dispatch-only cold deep-probe sample
stays, on the windows 3.14 leg.

**Acceptance.** `ci.yml` parses; a push shows 12 test legs + lint + artifact;
all succeed. The 0.5.3 run already proved the nine existing legs pass; the two
new 3.13 legs are the only unknown and are measured by the first run.

### WS-B — Security in CI and governance surfaces (D2, D8)

**Change.**
- `security` job on **windows-latest**, Python 3.14, `timeout-minutes: 40`.
  Windows rather than ubuntu because `aramid.toml` points the tests slot at
  `../.venvs/graphite-dev/Scripts/python.exe`, and on Windows that path is the
  real venv layout — the job creates that venv and installs `-e ".[dev,mcp]"`
  into it, so the repo's committed gate configuration runs **unmodified**.
  Steps: `pip install aramid==0.5.1` (the version every 0.5.3 gate ran
  under; dependabot proposes bumps); `aramid doctor` (bootstraps gitleaks
  and semgrep the way it does on a developer machine); CI-only keys that the
  repo's `aramid.toml` leaves unset (the LLM reviewer, which has no provider in
  CI) are written to the runner's `~/.aramid/config.toml` — the user layer of
  aramid's defaults ← user ← repo merge, so nothing in the repo is patched;
  then two gate runs, exit status read from files:
  `aramid check --gate pre-commit --all --strict --json` (gitleaks, ruff
  security rules, shadow) and `aramid check --gate pre-push --all --strict
  --json` (gitleaks, semgrep, tests). The job asserts `tools_ran` ⊇
  {`gitleaks`, `ruff`, `shadow`} for the first and ⊇ {`gitleaks`, `semgrep`}
  plus the tests slot for the second — a run where a tool silently never fired
  must not read as clean (see "a skipped arm is not a passed arm"). The suite
  running a thirteenth time inside this job is accepted: it is what makes
  "the local gate runs in CI" literally true rather than approximately.
- `.aramid-suppressions.toml` stays the single suppression source.
- `SECURITY.md`: supported versions (1.x), private reporting via GitHub Private
  Vulnerability Reporting (an owner-only setting — see §8), what to include,
  and the no-public-exploit rule already in CONTRIBUTING.
- `.github/CODEOWNERS` (`* @jared0565`), `.github/ISSUE_TEMPLATE/bug_report.yml`,
  `feature_request.yml`, `config.yml` (security → SECURITY.md).
- `.github/dependabot.yml`: `github-actions` weekly, `pip` weekly on
  `pyproject.toml`.
- `pyproject.toml`: `authors = [{name = "jared0565", email =
  "jared0565@gmail.com"}]` (the identity every commit already carries).

**Acceptance.** Security job green with the tool assertion; one recorded run
with a planted `graphite.py` at the root turns it red (the known-positive
control); deleting the plant turns it green again.

### WS-C — Coverage floor (D3)

**Change.** On the 3.12 leg of each OS, run pytest with
`--cov=src/graphite --cov-branch` and upload `.coverage.<os>` (relative paths
via `[tool.coverage.run] relative_files = true`). A `coverage` job downloads
all three, `coverage combine`, `coverage report --fail-under=<floor>`, and
uploads `coverage.json` + the text report. Floor policy in `CONTRIBUTING.md`:
set to the integer part of the first combined run; raised when a release
measures higher; never lowered without a CHANGELOG entry saying why.

Local reference (Windows only, dev venv, 2026-08-29): 82 % branch-inclusive,
19 404 of 23 043 statements. The CI combined figure is the one that sets the
floor because it includes the POSIX-only code; it is expected to land near
this number.

**Acceptance.** The job fails when `--fail-under` is set one point above the
measurement (negative control, run once and recorded), passes at the floor.

### WS-D — Type gate (D4)

**Change.** `[tool.mypy]` in `pyproject.toml`: `files = ["src/graphite"]`,
`python_version = "3.11"`, `check_untyped_defs = true`,
`warn_unused_ignores = true`, `warn_redundant_casts = true`,
`no_implicit_optional = true`; per-module `ignore_missing_imports` for
`networkx`, `community`, `tree_sitter*`, `louvain` (no stubs published).
`mypy` added to `[dev]`. Fix the ~130 findings — real narrowing, not blanket
`# type: ignore`; an ignore needs an error code and a reason. Add
`src/graphite/py.typed` and `Typing :: Typed`. The lint job runs
`python -m mypy`.

Files with the most findings, from the pyright baseline (mypy's distribution
is similar): `detach.py` 21, `routing/probe_runner.py` 13, `analyze.py` 12,
`probe_process.py` 12, `doctor_probes.py` 10, `doctor.py` 8.

**Acceptance.** `python -m mypy` exits 0 in the lint job; the count of
`type: ignore` comments added is reported in the merge commit; each carries a
code.

### WS-E — Provenance: build in CI, keep the digest guard (D10)

**Prerequisite.** Normalise the 40 CRLF working-copy files (re-checkout
through `.gitattributes`; the index is already LF, so no commit changes
content). Until this is done a Windows-built wheel cannot equal a Linux-built
one. The cross-OS comparison of v0.5.3 (WSL, pinned hatchling) is recorded in
the plan as the before-measurement.

**Change.**
- `release-build-constraints.txt` (committed): exact `build==` and
  `hatchling==` pins. Both workflows install from it.
- `ci.yml` `artifact` job: after building, print both SHA256s, build a second
  time into a separate directory and assert byte-identity (determinism within
  a runner), upload `dist/` as a workflow artifact (retention 90 days).
- `publish.yml`: inputs unchanged (`version`); env gains `APPROVED_COMMIT`.
  Steps: refuse unless `inputs.version == APPROVED_VERSION`; checkout
  `refs/tags/v<version>` with `persist-credentials: false`; assert the tag's
  commit equals `APPROVED_COMMIT`; install pinned tools; build; run
  `scripts/verify_artifact.py dist`; `sha256sum -c --strict` against the
  approved digests (the same guard as today, now proving reproducibility at
  publish time); upload both files to the GitHub Release for the tag
  (`gh release upload`, creating the release if absent, never clobbering an
  existing asset); publish with `attestations: true`. Permissions:
  `id-token: write`, `contents: write` (needed to attach assets).
- `RELEASING.md`: the approval digests come from the `artifact` job's log on
  the release commit **and** the maintainer's local build (two sources, as
  today); the retained wheel in `.graphite-releases/<v>/` is downloaded from
  the GitHub Release after publication and digest-checked, so the store holds
  what was published rather than a local sibling.
- `scripts/verify_published_release.py` gains an arm that fetches the PyPI
  provenance/attestation for the wheel and reports its presence (informative:
  the digest arm stays the load-bearing one).

**Acceptance.** Two CI `artifact` runs on the same commit print identical
digests (recorded in the plan). A dry-run dispatch of `publish.yml` against a
deliberately wrong `APPROVED_COMMIT` stops before upload (negative control,
recorded). The real 1.0.0 dispatch (WS-I) is the positive case.

### WS-F — POSIX daemon supervision (D5)

**Design.** Mirror `windows_task.py` (`TaskCommand`, injectable `run`,
`_result_payload`) rather than invent a new shape:

- `src/graphite/daemon_launch.py`: the one argv builder
  (`interpreter -P -m graphite daemon <base> --scan-interval … --max-projects …`)
  shared by the three platforms; `windows_task.daemon_task_command` delegates
  to it so Windows keeps its exact command.
- `src/graphite/systemd_unit.py`: `render_unit(...)` → text
  (`[Unit] Description`, `[Service] Type=simple, ExecStart=<argv, systemd-quoted>,
  WorkingDirectory=<base>, Restart=on-failure, RestartSec=5,
  Environment=PYTHONSAFEPATH=1` as belt-and-braces beside `-P`,
  `[Install] WantedBy=default.target`); `install_unit` writes
  `~/.config/systemd/user/graphite-daemon.service` (0644, atomic via
  `replace_file`), `systemctl --user daemon-reload`, `enable --now`;
  `query_unit` (`systemctl --user show -p ActiveState,SubState,MainPID`);
  `uninstall_unit` (`disable --now`, remove file). Lingering is documented, not
  enabled (it is a policy decision).
- `src/graphite/launchd_agent.py`: `render_plist(...)` via `plistlib`
  (`Label com.graphite.daemon`, `ProgramArguments`, `WorkingDirectory`,
  `RunAtLoad true`, `KeepAlive {SuccessfulExit: false}`, `StandardOutPath`/
  `StandardErrorPath` under `~/Library/Logs/graphite/`); `install_agent`
  writes `~/Library/LaunchAgents/com.graphite.daemon.plist`, `launchctl
  bootstrap gui/<uid> <plist>`; `query_agent` (`launchctl print
  gui/<uid>/com.graphite.daemon`); `uninstall_agent` (`launchctl bootout`).
- CLI: `daemon-install-linux`, `daemon-uninstall-linux`, `daemon-install-macos`,
  `daemon-uninstall-macos`, and `daemon-service-status` (dispatches by
  platform; on Windows it reports the scheduled task and startup launcher).
  Each refuses on the wrong platform with the same message shape as
  `require_windows`.
- `daemon_health` "startup installed" check and `doctor`'s Daemon probe
  consult the platform's installer, so `daemon-health` on Linux/macOS reports
  the unit/agent instead of a Windows-only answer.

**Tests.** Pure rendering tests (argv carries `-P`; no shell interpolation;
paths with spaces quoted per platform rules; plist round-trips through
`plistlib`); fake-`run` tests for install/query/uninstall sequencing and
failure payloads; platform-gated validation on CI — `systemd-analyze --user
verify` on Linux, `plutil -lint` on macOS — skipped with a named reason where
the tool is absent, and the skip reason asserted absent on the leg where the
tool must exist (a skip is not a pass).

**Acceptance.** All 12 legs green; the Linux and macOS legs show the
validation tests *ran*.

### WS-G — Documentation, compatibility contract, metadata (D6, D7)

**Change.**
- `docs/reference/cli.md` generated by `scripts/gen_cli_reference.py` from the
  argparse tree (every subcommand, its help, its options);
  `tests/test_cli_reference.py` regenerates in memory and fails on any diff.
- `docs/reference/configuration.md`: every `Config` field with env var, CLI
  flag, default and meaning; `tests/test_configuration_reference.py` asserts
  every field of `Config` and every `GRAPHITE_*` name the loader reads appears.
- `docs/reference/exit-codes.md`: one table — command, code, meaning —
  consolidated from the four README paragraphs and the source.
- `docs/compatibility.md`: stable surfaces (CLI subcommands and flags listed
  in the reference; JSON outputs carrying `schema_version`; `graph.json`
  schema 1; `GRAPHITE_*` configuration; exit codes; hook/trampoline contract;
  MCP tool names; the channel protocol), what is *not* stable (internal
  modules, `graph-out/` internals other than `graph.json`, routing/overlay
  storage), the deprecation policy (a deprecated surface keeps working with a
  warning for at least one minor release and is removed only in a major), and
  the support matrix (the CI matrix, by construction).
- README: the reference material is replaced by links; the phrases pinned by
  `tests/test_documentation.py` stay.
- `CHANGELOG.md` header: semver, 1.x compatibility per `docs/compatibility.md`.
- `pyproject.toml`: `Development Status :: 5 - Production/Stable`,
  `Typing :: Typed`, `Programming Language :: Python :: 3 :: Only`.

**Acceptance.** The two lockstep tests pass and each is shown to fail when the
doc is edited (negative control, once); `test_documentation.py` still passes.

### WS-H — Scale: measured and declared (D9)

**Change.**
- `benchmarks/synthetic_repo.py`: deterministic generator (seeded) producing
  N files across Python, TypeScript, JavaScript, Go and Rust with cross-file
  imports and calls in realistic proportions; `benchmarks/build_benchmark.py`
  builds it with `--llm none`, reports wall time, peak RSS (`resource` on
  POSIX, `GetProcessMemoryInfo` via `ctypes` on Windows), node/edge counts,
  ms/file, as JSON.
- `ci.yml` `benchmark` job (ubuntu, 3.12): 3 000 files; uploads the JSON;
  fails only if wall > 600 s or the graph exceeds `max_graph_bytes` — a
  catastrophe detector, documented as such, not a performance SLA.
- `capabilities` limits gain `supported_repo_files: 20000` with a
  `basis` string naming the measurement; `docs/benchmarks.md` records the
  synthetic 20 000-file run (local) and one real repository at a pinned
  commit.

**Acceptance.** Benchmark job green with an artifact; the declared number is
backed by a recorded run at or above it.

### WS-I — Release 1.0.0 (D11)

Follow `RELEASING.md` as updated by WS-E: gates → prepare version → artifact
digests from CI and local → approve in `publish.yml` → tag → push → **maintainer
dispatches publish** → verify from the index → retain from the Release →
deploy on this machine (stop → install → start daemon) → channel round to
consumers → `.graphite-releases/index.md` and memory updated.

## 6. Sequencing

```
WS-A (matrix gates)  ──►  WS-D (mypy)  ──►  WS-C (coverage)  ──►  WS-B (security + governance)
        │
        └──►  WS-F (POSIX daemon)  ──►  WS-H (benchmark)  ──►  WS-G (docs, contract, metadata)
                                                                        │
                        CRLF normalisation ──►  WS-E (provenance)  ◄────┘
                                                        │
                                                        ▼
                                                   WS-I (1.0.0)
```

WS-A first so every later stream is proven on all 12 cells. WS-G last among
the code streams so the reference documents the final surface. WS-E after the
CRLF fix and before the release because the release must run through it.

## 7. Risks

| Risk | Mitigation |
|---|---|
| aramid's tool bootstrap (gitleaks, semgrep) behaves differently on a hosted runner. | `aramid doctor` first; assert `tools_ran`; if a tool cannot run in CI the job fails rather than passes — the design refuses a silent skip. |
| The two new 3.13 legs expose a real platform defect. | That is the point of gating; fix before merging WS-A, or if unrelated to WS-A, file and fix in-stream before 1.0.0 — D1 requires 12/12. |
| mypy fixes change behaviour. | Each fix is a narrowing or a corrected annotation; the whole suite runs on every leg; suspicious sites get a focused test first (TDD per CONTRIBUTING). |
| CI-built wheel differs from the local build even after CRLF normalisation (locale, timestamps). | hatchling builds are reproducible by design (`SOURCE_DATE_EPOCH` fixed); the `artifact` job's double build proves determinism per runner; the publish digest guard proves it across runs. If a diff remains, the approval digests come from CI alone and RELEASING.md says so. |
| launchd cannot be exercised on this machine or on a hosted macOS runner's GUI session. | Tests validate the plist and the command sequence with a fake `run`; `plutil -lint` on the macOS leg validates the artifact; `launchctl` behaviour is documented as verified by contract, not soak. |
| Coverage floor set from a lucky run. | Floor is the integer part; ratchet only upward; the policy is written down. |

## 8. Operator-only steps

These need the repository owner and are listed so nothing waits silently:

1. Enable **Private vulnerability reporting** on the GitHub repository
   (Settings → Code security), or run
   `gh api -X PUT repos/jared0565/graphite/private-vulnerability-reporting`.
   `SECURITY.md` points at it.
2. Confirm the `authors` identity in `pyproject.toml` before WS-B merges (the
   default is the git identity every commit already carries).
3. Dispatch `publish.yml` for 1.0.0 (WS-I). Publication is never automatic.

## 9. Verification doctrine

Applies to every stream, from the repository's working rules:

- A gate is only accepted after it has been shown to **fail** on a known
  positive (negative control) and pass on the fix; both are recorded.
- Exit codes are read from files, never through a pipe.
- "CI green" means the run on the merge commit, on every leg, by SHA.
- A skipped test is not a passed test: platform-gated tests assert on the
  platform where they must run that they did run.
- Figures in this document are snapshots dated 2026-08-29; re-measure before
  quoting them later.

## 10. Execution record (2026-08-29)

What the streams found while making the criteria true. Each item changed a
design decision above or is evidence a later reader will want.

- **WS-A** — the nine formerly advisory legs plus the two never-run 3.13
  cells all passed on the first gating run (dispatch 33231751551, main
  33233419549). The cost argument in `ci.yml` had expired: standard runners
  bill zero on a public repository, measured from the timing API.
- **WS-D** — 142 mypy findings in 34 files fixed with zero `type: ignore`;
  one real defect (`lifecycle_storage.py`: `AttributeError` on a
  no-prior-observation event over an UNAVAILABLE row). Platform-only code
  narrows through typed call-time views (`_win32_ctypes.py`, `_PosixOs`)
  rather than `sys.platform` guards, because tests reach POSIX branches on
  Windows by patching `os.name`. Clean on win32, linux and darwin.
- **WS-C** — combined coverage 83.04 % (run 33235961969); floor 83;
  negative control at 84 failed (run 33236479723). Windows and Linux data
  files combine without separator duplicates under `relative_files`.
- **WS-B** — the security job needed three iterations that were all
  runner facts, not aramid's: a `pwsh` default shell collapsing exit 2 to
  1, `bash -e` aborting before diagnostics, and a backspace byte a heredoc
  put into the workflow. Proven green (33240107635); the shadow plant failed
  the security job and, under `python -m pytest`, every test leg
  (33240529198). Two aramid defects reported (rounds 139/141): the
  typecheck runner hands non-Python files to mypy (accepted, 0.6.1) — bridged
  by moving mypy's configuration to `setup.cfg` — and a `doctor` exit the
  runner had misreported.
- **WS-H → #64** — the real-repository row found what the synthetic corpus
  never could: `analyze()` enumerated every simple cycle of the graph;
  Django 5.2 (2 930 sources) took 13 GB and did not finish in 30 min. Bounded
  level-by-level search under a budget with a `cycle_search` block in the
  analysis; Django builds in 66.9 s at 520 MB. `supported_repo_files` is
  declared at 7 000 from Django's density (~18 KB of graph per file reaches
  the 128 MiB cap near 7 400), not the synthetic corpus's 18 000.
- **WS-F** — proven on the platforms themselves: `plutil -lint` has no skip
  path on the macOS legs; `systemd-analyze --user verify` passes under WSL
  Ubuntu (systemd 255) and on ubuntu-latest.
- **WS-G** — the generated CLI page first rendered `F:\Projects` (the value
  of `GRAPHITE_PROJECTS_ROOT` here) as a default and failed every CI leg; a
  committed page names the rule, never an environment value, and a test
  renders it with and without the variable.
- **WS-E** — cross-OS archive digests are not a valid check even after the
  working copy was normalized: the same commit's Windows and Linux builds
  are content-identical while the zip `create_system` byte and the deflate
  stream (zlib-ng 1.3.1 vs zlib 1.3.2) differ in every member. The approval
  digests are therefore CI's, agreed across two runs; the maintainer's
  build is a content check (`scripts/compare_dist.py`). The `publish.yml`
  negative control (dispatching 0.5.3, whose approved digests are a
  maintainer build) stops at the digest guard before any upload.
