# Changelog

Notable changes to graphite. Format follows [Keep a Changelog]; versioning is
semver, pre-1.0 (minor versions may break things).

**A version number here is a coarse release label, not a fix marker.** Several
behavioural fixes have shipped with no version change at all — the `-P`
agent-hook fix went out under an unchanged `DOC_VERSION`. To answer "does this
install have fix X", survey for the marker X introduced, or compare
`graphite --version` fingerprints between installs. The fingerprint is the
machine-checkable identity; the version is for humans.

[Keep a Changelog]: https://keepachangelog.com/en/1.1.0/

## [Unreleased]

### Fixed

**The generated daemon launcher ran a wrapper instead of the interpreter.**
`daemon_task_command` built its command from `resolve_graphite_executable()` —
whatever `graphite` resolved to on PATH, or `~/.local/bin/graphite.cmd` — and
launched it with the supervised projects root as the working directory, hidden,
at every login. It now emits `<interpreter> -P -m graphite daemon …`, and an
explicit `--graphite-executable` naming a console script is refused rather than
silently accepted.

⚠️ **The commit subject for that change (`ff34b4f`) states the mechanism
incorrectly**, and a published subject cannot be amended. `3c5304f` corrects the
source; this entry is the version a `git log --oneline` reader should trust.

A console script is **not** cwd-shadowable: running a script puts the script's
own directory on `sys.path[0]`, and only `-m` puts the CWD there. The hazard is
an `-m` **inside a wrapper** — which a generator can neither see into nor add
`-P` to. Measured from a directory holding a hostile `graphite.py`:

| launch | result |
|---|---|
| `python -m graphite` | shadow ran |
| `python -P -m graphite` | real graphite |
| `.cmd` wrapper → `python -B -m graphite` | shadow ran |
| `.cmd` wrapper → `python -B -P -m graphite` | real graphite |

So scope a shadowing sweep by *"does anything in this chain reach `-m` with a
repo root as its working directory"* — not by artifact kind, and not by whether
the head of the command looks like an interpreter.

**Fixing the generator does not fix the launcher it already wrote.** An existing
install keeps the old command until `graphite daemon-install-startup-windows`
(or `daemon-install-windows`) is re-run — the same marker-not-version rule this
file opens with.

## [0.2.1] — 2026-08-09

A portability release. `0.2.0` listed Linux and macOS as unverified; the suite
now runs green end to end on Linux. Two of the defects hiding behind that gap
were real, and the rest were tests that had never executed on a POSIX machine at
all.

### Added

- MIT license, declared with PEP 639 (`license = "MIT"` plus `license-files`)
  and shipped in both the wheel and the sdist. The `v0.2.0` tag predates the
  license commit, so that tagged tree carries no LICENSE file.
- `resolve_trusted_file(..., follow_launcher=True)`: POSIX launcher-aware
  resolution, which keeps trust anchored in the resolved target while preserving
  the caller's own spelling for execution.

### Fixed

**Virtual environments on POSIX.** Two call sites canonicalised `sys.executable`
before launching it. `.venv/bin/python` is a symlink there, and Python locates a
virtual environment from the executable it was *invoked as*, so the resolved path
started the base installation instead — which cannot `import graphite` at all.
Both now judge the resolved target and launch the path they were given.
Containment is unchanged: the rejection still tests the resolved path, so a
symlink outside the workspace pointing at a workspace-controlled binary is still
refused. Windows never saw this, because its virtual-environment interpreters are
copies rather than symlinks.

**A test froze the global clock and hung every POSIX CI leg** (#45).
`monkeypatch.setattr(probes.time, "monotonic", ...)` reads as module-scoped and
is not — `probes.time` *is* the stdlib `time` module — so the patch reached
`probe_process`'s POSIX grace loop, whose exit condition became unreachable and
which then spun on a real `time.sleep`. Every POSIX leg was killed at the
45-minute timeout. Fixed in the test by offsetting a real clock rather than
freezing one: an advancing `time.monotonic()` is the function's contract, so a
production guard would defend a condition that cannot occur.

### Changed

- The residual TOCTOU in `_canonical_executable` is now named where it lives.
  Judging the resolved target while executing the given path moves the race from
  "swap the file" to "re-point the symlink". It stays accepted — no path check
  closes it, only an fd-based exec does — and the obvious narrowing, refusing
  group- or world-writable launcher directories, would reject Homebrew's
  `/usr/local/bin` on exactly the platforms the fix exists to support.

### Known limitations

As in `0.2.0`, except:

- **Linux is now verified**: 2835 passed, zero failures, zero timeouts, on
  Ubuntu under WSL2 with CPython 3.12.13. That is one machine, one distribution
  and one interpreter build — it means "no longer failing here", not "portable".
- **macOS remains unverified** (#46). Linux evidence is evidence about Linux.
- **CI has not started a job since 2026-08-05.** Every push since is refused
  with a GitHub billing/spending-limit error before the job begins, so the local
  gate and the WSL runs are currently the only signal.

## [0.2.0] — 2026-08-07

The first tagged release, covering everything since the initial import on
2026-07-10 — 662 commits in total. `0.1.0` was the version the repo was created
at; it was never tagged or released, so there is no earlier baseline to diff
against. Grouped by theme rather than enumerated; `git log` has the detail.

### Added

**Answers that grade themselves.** Every query result carries an `answer` block
scoped to the relations and languages that answer actually used, graded
`decision_grade` / `advisory` / `inconclusive`, with a registry of named
caveats. On a `decision_grade` answer an empty result is a trustworthy absence.
Human output prints `answer health:` and `known limits:` lines only when the
answer is empty or degraded. Published JSON schemas under `docs/schemas/` with
compatibility tests.

**Resolution health as a first-class signal**, now at schema 3: `calls` and
`imports` cells each carry an `external` count, and `total` already excludes it,
so `ratio == bound / total` with no further adjustment.

**Language binding.** Python cross-module call binding via symbol/alias import
maps, plus method dispatch (`is_method` tagging and member flow-through).
TypeScript/JavaScript arrow-assigned definitions, arrow-valued class fields, and
`new X()` construction edges. Rust `use` and `mod` resolution against indexed
Cargo manifests, attributed per crate.

**A daemon that supervises only repos open in a coding agent**, with an
activation registry, a cross-process build lock with TTL staleness, `build
--detach`, and rebuilds queued when the engine identity changes rather than only
at restart.

**An append-only incident ledger** with event-sourced triage
(`incidents list/ack/resolve`), fed by build cycles, observer cycles, extraction
errors, artifact and graph-load failures, and inconclusive queries; surfaced in
`doctor` and `daemon-health`.

**Agent integration.** `graphite init` writes per-agent instruction files and
git hooks (`--no-hooks` opts out; `--adopt` brings legacy unversioned docs under
management by appending, never overwriting). A `graphite-first` PreToolUse hook
in remind or strict mode, where strict denial is gated on proven resolution
health. An MCP server, and a shared agent channel with a broker, identity
derivation, create-only rounds and an append-only status log.

**CLI.** `query` (with `callers`, `calls`, `reaches`, `path`, `depends-on`,
`imported-by`, `community-of`, `stats`), `search`, `capabilities`, `context`,
`impact`, `review-changes`, `doctor`, `incidents`, `channel`, `savings`, and
`--version`.

`graphite --version` reports the engine fingerprint — a digest over the engine's
own source files — alongside the cache and schema versions. It is byte-exact, so
the implication runs one way: two installs agreeing on the fingerprint are
running identical code, but two that differ are not necessarily running
different code. Line endings are bytes, and `.gitattributes` normalizes to LF on
commit, so a working tree holding CRLF fingerprints differently from a fresh
checkout of the same commit. Equality proves sameness; inequality does not prove
difference.

### Changed

- The reported version now comes from `graphite.__version__` in the source tree
  rather than from `importlib.metadata`. Under an editable install the metadata
  is written once, at install time, and never moves again — so a release bump
  reached nobody until every consumer reinstalled. `pyproject.toml` reads the
  version from the source (`[tool.hatch.version]`) rather than declaring it.
- `graphite --version` names a stale install when the source version and the
  installed distribution metadata disagree, instead of silently preferring one.
  That disagreement is routine for an editable install between reinstalls, but
  it is also what a shadowing `graphite` on `sys.path` looks like.
- `requires-python = ">=3.11"` is now backed by measurement rather than
  inherited: 3.11, 3.12, 3.13 and 3.14 each run the full suite clean.
- Extraction cache partitions on engine identity as well as cache version, so an
  extraction change invalidates its own cached extraction and `cache_version` is
  back to being a coarse manual override. Unreachable partitions are reclaimed
  on build.

### Fixed

**Module shadowing (security).** `python -m graphite` puts the current directory
at `sys.path[0]`, so a `graphite.py` — or a `graphite/` directory — at a repo
root beats the installed package, and a module-shaped shadow *runs* before it
errors. Every launch graphite generates or performs now passes `-P`, across six
surfaces: agent hooks, git-hook trampolines, graphite's own repo, `.mcp.json`,
`.vscode/tasks.json` (which fires on folder open, with no invocation), and
graphite's own source, where seven launches carried `-B` — a flag that
suppresses bytecode and does nothing to `sys.path`. A bare `graphite …` console
script is shadowable identically and cannot express the fix. `-P` rather than
`-I`, because `-I` implies `-E` and would strip the `GRAPHITE_*` config the CLI
reads. `doctor` reports foreign hooks in a shadowable form, and never rewrites
what it did not author.

**Channel registry gate.** Emptying, deleting or renaming the committed agent
registry disarmed the commit-message audit gate as thoroughly as removing it;
authorisation is now derived from committed state, so a commit can no longer
register itself, and a corrupt committed registry wedges rather than opening.

**Subprocess decode.** `text=True` without `encoding=` decodes with the locale
codec, and the failure lands on subprocess's reader thread: `stdout` comes back
`None` while `returncode` stays 0, so the crash surfaces frames from its cause.
Observed live. Fixed across seven modules and guarded mechanically via `ast`.

**Graph-first bypass.** The PreToolUse matcher named tools rather than
behaviour, so searches issued through the Bash and PowerShell tools never
reached the hook. Shell commands are now parsed and routed through the same
denial path. Four subsequent parser defects fixed: redirection operands,
separated flag values and out-of-repo targets were falsely denied, and a
case-folded `-E`/`-e` collision failed open.

**Git enumeration hardening.** A trusted-path runner, isolated environment,
containment checks against symlink and absolute-path escapes, and fail-closed
behaviour — no filesystem fallback when a repository cannot be enumerated
safely.

**Portability.** Zero-argument `super()` inside a `@dataclass(slots=True)`
raises on Python 3.11 and 3.12 because the decorator rebuilds the class; the
explicit two-argument form is now an AST invariant, since the interpreter the
gate runs on cannot catch a reintroduction. `build_graph` no longer drops an
edge that shares a node pair but differs by relation. Redirected CLI output is
forced to UTF-8. An MCP probe no longer closes the child's stdin with a reply in
flight: the server read that EOF as end-of-session and tore down mid-reply, so
`initialize` was answered — it is handled inline — while `tools/list`, which is
dispatched to a task racing the same teardown, was dropped.

### Known limitations

- **Dynamic dispatch, decorator rebinding and `getattr` calls stay unbound.**
  They are counted honestly in the resolution ratio rather than hidden.
- **Go and Rust imports ratios are honestly lower** than Python's and
  TypeScript's: neither emits `EXTERNAL_IMPORT`, so external imports are not
  excluded from their denominators. Cross-schema ratio comparison is invalid —
  branch on `schema`.
- **Linux and macOS are unverified** (#45, #46). A POSIX routing defect is fixed
  — `_canonical_executable` rejected symlinks, and every POSIX interpreter is
  one — but the suite has not been run green on either. CI cannot currently
  confirm it.
- **The Windows Store Python distribution is not supported.** Its app-execution
  alias injects `PYTHONUSERBASE`, redirects AppData and changes process
  identity, which breaks environment-sanitization and process-cleanup
  behaviour. Use a regular CPython install.
- **One flaky test remains** (#37).
- Aggregate `resolution_health.healthy` can report true while the language your
  question actually used is degraded. Gate on `answer.grade`, not the aggregate.

## [0.1.0] — 2026-07-10

Initial import as a standalone repository. Never tagged or released.
