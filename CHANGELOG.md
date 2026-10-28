# Changelog

Notable changes to graphite. Format follows [Keep a Changelog]; versioning is
semantic. From 1.0.0 the surfaces listed in `docs/compatibility.md` are
stable: a breaking change to any of them is a major release.

**A version number here is a coarse release label, not a fix marker.** Several
behavioural fixes have shipped with no version change at all — the `-P`
agent-hook fix went out under an unchanged `DOC_VERSION`. To answer "does this
install have fix X", survey for the marker X introduced, or compare
`graphite --version` fingerprints between installs. The fingerprint is the
machine-checkable identity; the version is for humans.

[Keep a Changelog]: https://keepachangelog.com/en/1.1.0/

## [Unreleased]

## [1.7.1] — 2026-10-28

A patch release: three fixes, and no change to any stable surface. No command,
option, exit code, entry point or JSON field is added, removed or renamed, so
this is a patch by the rule in `docs/compatibility.md` -- "fixes are patches".

**A launch failure keeps its cause.** `probe_process.py` raised
`ProbeProcessError("launch_failed") from None`, so the exception under it was
discarded and the CI log said only `probe process failed: launch_failed` --
nothing said why the interpreter failed to start. The cause is now kept, as
the `__cause__` and not in the message, so the text a report or a log line may
carry still holds no path and no process data.

**A child that exits first no longer fails the launch.** The cause the first
fix made visible turned out to be a race, and it is now fixed rather than
merely explained. On macOS the exit observer is a kqueue registration, and the
kernel refuses it for a process that is no longer there. The registration is
made right after the fork, so a child that exits before it reaches the kernel
lost the race -- and the launch was then reported as a transport failure, for a
child that had run and succeeded.

The child is a zombie at that point, so its status is still readable, and it is
read where it was always read: at the reap, after the containment. That order
is what keeps the process group held -- the zombie keeps the group non-empty,
so the group's id cannot be recycled under the signals that follow -- and the
status stays unread until then rather than carrying a placeholder that would
be a false status.

A test beside it was repaired too. The sibling that claims "fails closed
before spawn" was failing in the environment validation and never reached its
own spawn check, and its "must not spawn" was an assertion that the launch's
own `except Exception` would have swallowed into the same verdict. It now
reaches the check and asserts it.

**A directory's moving times are not the trusted route's identity.** The POSIX
trusted-command route captured every directory on the path from the filesystem
root down to the executable, and refused a component whose stat moved between
the capture's two `lstat` calls -- comparing a directory's size, mtime and
ctime. None of those is a directory's identity: the size is its entry count,
and the times move whenever a child is created or removed.

pytest's xdist workers all create their `popen-gwN` directory inside the
SHARED `pytest-N` parent, so one worker's `mkdir` landed inside another
worker's route capture, and the route was refused -- a route that never went
near the executable. The refusal was silent where it matters: the manager
resolution returned `None` (`manager_unavailable`) and the revalidation failed
(`command_changed`). Two CI cells, two shapes, one defect, and both were a
security control failing CLOSED on a harmless event.

What the route's trust rests on is the hop's inode (is it still the same
object), its mode (who may write it) and its owner (who owns it), and all
three are still compared: a swap changes the inode, a `chmod` the mode, a
`chown` the owner. The code already said this about a directory's link count,
which is its number of subdirectories; the fix extends the same reasoning to
the size and the times. A file keeps every field -- its content is hashed and
compared separately, and its times are its identity.

Two further changes in this release are TEST-ONLY and change no product
behaviour: the published `check` schema is now checked against every shape it
answers -- which is what kills the recorded mutation survivor in `cli.py`,
measured by mutating that one line and watching the suite fail at the new
assertion; and a fourth wall-clock race in the doctor tests -- a teardown
waited for by a one-second poll rather than by the teardown's own signal -- is
fixed the way its three siblings already were.

The graph changes by what these fixes add. Measured on this repository: the
v1.7.0 tree built by the installed 1.7.0 engine, and this tree built by this
engine, each into fresh output, cache and state directories:

- 9,322 nodes become 9,352 and 25,655 edges become 25,714.
- every node id survives: 9,322 of 9,322 are in both, 30 are new and 0 is removed.
- 6,768 definition ids become 6,793: 6,479 keep their id and their definition, 289 keep their id but move, 0 is removed and 25 are new.
- `calls` goes from 16,457 to 16,490, `contains` goes from 6,766 to 6,791, `imports` goes from 2,480 to 2,484, `inherits` stays at 122.
- 25,825 relation triples become 25,887, with 3 only in the old graph and 65 only in the new.
- the cycle count goes from 18 to 18, and the communities from 178 to 180.
- every one of those moves is in the 6 files this release touches (`src/graphite/dependency_install.py` 43, `src/graphite/probe_process.py` 17, `tests/test_dependency_install.py` 16, `tests/test_doctor.py` 201, `tests/test_probe_process.py` 6, `tests/test_published_schemas.py` 6).
- the control holds: the same tree built by both engines gives the same 9,352 nodes, the same 25,714 edges and the same 25,887 relation triples, with no id moved and none removed, and identical cycles, communities and `resolution_health` -- so the difference above is attributable to this change and not to the engine behind it.

`metadata.engine.fingerprint` moves, as it does whenever the engine's own
files change -- and they do: the identity hashes every `.py` and `.mjs` file
in the package, so a change to the probe transport or to the trusted-command
route moves it whether or not the extraction changed.

The two builds above used different `tree-sitter` versions (0.25.2 for the
installed 1.7.0 and 0.26.0 for the dev environment), so the difference was
controlled for rather than assumed away. The grammar that actually parses
Python is the same in both -- `tree-sitter-python` 0.25.0 -- and the control
build settles the rest: the SAME tree built by both engines gives the same
ids, the same edges and the same triples, node for node, with no id moved and
none removed, and identical cycles, entry points, god nodes, orphans,
surprising connections, top files by links, clusters and `resolution_health`.
The figures above are therefore attributable to this change and not to the
parsers.

## [1.7.0] — 2026-10-28

A minor release: the daemon's log now says WHICH way enumerating a repository
failed, and `check --json`, `validate --json` and `daemon-health --json` carry
`schema_version`, with a schema published for each of them.

The three outputs are stable surfaces and a field is added to each, so this is
a minor by the rule in `docs/compatibility.md` -- "Fields are added in minors"
-- and by the precedent of 1.1.0, whose own note reads "The agent channel's
read surfaces gain fields, which is an addition to a stable surface." No
command, option, exit code or entry point is added, removed or renamed.

The field was added because the three outputs were the only ones in that list
without it, so a consumer branching on it had nothing to branch on. The
published schemas are deliberately open at the top level, because the same
rule lets a later minor add fields to them.

The daemon's change is a fix that adds no field: every way enumerating a
repository can fail raised the same path-free `IngestError`, so the cause
chain rendered the class name alone and four different failures read
"IngestError" and could not be told apart. Each failure now raises a cause
carrying its own reason, and the chain names it. The message a caller sees is
unchanged.

The graph changes by what these fixes add. Measured on this repository: the
v1.6.0 tree built by the installed 1.6.0 engine, and this tree built by this
engine, each into fresh output, cache and state directories:

- 9,298 nodes become 9,322 and 25,599 edges become 25,655. The 24 new nodes
  are the functions the daemon's fix adds, the three published schemas, the
  new test's function, and the fixtures and helpers that go with them;
- every node id survives: 9,298 of 9,298 are in both, 24 are new and none is
  removed; 8,742 of them keep their denotation exactly;
- 6,752 definition ids become 6,768: 6,196 keep their id and their definition,
  and 556 keep their id but move line -- every one of them in the six files
  this release touches (`tests/test_doctor.py` 383, `src/graphite/cli.py` 89,
  `src/graphite/daemon.py` 35, `tests/test_agent_hooks_session_tagging.py` 18,
  `src/graphite/ingest.py` 16, `tests/test_published_schemas.py` 15). No
  definition id is removed and 16 are new;
- `calls` goes from 16,418 relation triples to 16,457, `contains` from 6,750 to
  6,766, `imports` stays at 2,480, and `inherits` goes from 121 to 122. 25,769
  relation triples become 25,825, with 2 only in the old graph and 58 only in
  the new one;
- 18 cycles where there were 18, the same ones, and 178 communities where
  there were 175. The entry points, the orphans and the surprising
  connections are identical; the god nodes and the top files by links differ,
  because the new code adds and moves them;
- `resolution_health` is healthy on both sides: the `calls` ratio is 0.983
  before and after, and `imports` is 1.0 on both.

`metadata.engine.fingerprint` moves, as it does whenever the engine's own
files change -- and they do: the identity hashes every `.py` and `.mjs` file
in the package, so a change to the daemon or the ingest moves it whether or
not the extraction changed.

The two builds above used different `tree-sitter` versions (0.25.2 for the
installed 1.6.0 and 0.26.0 for the dev environment), so the difference was
controlled for rather than assumed away: the SAME tree built by both engines
gives the same ids, the same edges and the same triples, node for node --
9,322 nodes, 25,655 edges and all 25,825 relation triples identical, with no
id moved and none removed, and identical cycles, entry points, god nodes,
orphans, surprising connections, top files by links, clusters and
`resolution_health`. The figures above are therefore attributable to this
change and not to the parsers.

### Added

- `check --json`, `validate --json` and `daemon-health --json` now carry
  `schema_version`, which `docs/compatibility.md` already listed them as
  carrying, and a schema for each is published under `docs/schemas/`. The
  three outputs were the only ones in that list without the field, so a
  consumer branching on it had nothing to branch on. A field is added to
  three stable surfaces, so this is a minor by the rule in
  `docs/compatibility.md` -- "Fields are added in minors" -- and the
  published schemas are deliberately open at the top level, because the
  same rule lets a later minor add fields to them.

### Fixed

- The daemon's log now says WHICH way enumerating a repository failed.
  Every one of those failures raises the same path-free `IngestError`
  message, and the cause chain rendered the class name alone, so four
  different failures read "IngestError" and could not be told apart -- the
  same loss #113 fixed for Git's exit status. Each failure now raises a
  cause carrying its own reason (`nested_git_root`,
  `output_not_nul_terminated`, `too_many_records`, `path_not_contained`),
  and the chain names it. The message a caller sees is unchanged, and no
  command, option, exit code or JSON field changes.
- Tests: the three mutation survivors in `src/graphite/agent_hooks.py` are
  now killed. A mutant the suite cannot kill is a behaviour the suite does
  not pin, and the mutation gate's ledger recorded three of them: `or` ->
  `and` in `_entry_is_this_session`, and `0` -> `1` twice in `handle_stop`
  -- in the ledger's byte length when there is no ledger, and in the
  cursor's offset when the state has none. The first two are pinned by the
  one case that distinguishes them. The third had no such case and needed
  one: a cursor state that lost its offset is a damaged cursor, and the
  ledger is newline-delimited JSON, so an offset of 1 would cut the first
  entry in half -- it would no longer parse, and the turn's own use would
  go uncounted without a word. The default is 0, which is what the
  function already does with a damaged cursor elsewhere. Each kill is
  proved by applying the mutation and watching the test that is meant to
  kill it fail, with the source restored byte-identical afterwards.

## [1.6.0] — 2026-10-27

A minor release: the daemon's health report no longer reads "ok" while every
snapshot of an activated project fails (#113), and the daemon's log now
carries the cause of a failure instead of only its message. The
`daemon-health --json` output gains two fields on a failing project and a
new error code, and the `dev` extra gains one dependency, so this is a
minor by the rule in `docs/compatibility.md` -- "Fields are added in
minors" -- and by the precedent of 1.1.0, whose own note reads "The agent
channel's read surfaces gain fields, which is an addition to a stable
surface." No command, option, exit code or entry point is added, removed or
renamed, and the `daemon-status` output gains a listing where it had none.

The graph changes by what this fix adds. Measured on this repository: the
1.5.0 release tree built by the installed 1.5.0 engine, and this tree built
by this engine, each into fresh output, cache and state directories:

- 9,244 nodes become 9,298 and 25,415 edges become 25,599. The 54 new nodes
  are the functions and classes this change adds, the file of the new test
  file, and the fixtures and helpers that go with them;
- every node id survives: 9,244 of 9,244 are in both, 54 are new and none
  is removed;
- 6,712 definition ids become 6,752: 6,495 survive naming the same
  definition at the same line, and 217 survive by id but move line -- every
  one of them in the eight files this change touches (`cli.py` 43,
  `doctor.py` 37, `daemon.py` 31, `daemon_health.py` 18,
  `tests/test_typescript_activation.py` 17, `git.py` 12,
  `ingest.py` 2, `tests/test_hardening.py` 57). No definition id is
  removed and 40 are new;
- `contains` goes from 6,710 relation triples to 6,750 and `imports` from
  2,465 to 2,480; `calls` goes from 16,290 to 16,418; `inherits` (120)
  becomes 121. 25,585 relation triples become 25,769, with none removed;
- 18 cycles where there were 18, the same ones, and 177 communities where
  there were 175. Orphans and surprising connections are identical; the
  entry points, the god nodes and the top files by links differ, because
  the new code adds and moves them.

`metadata.engine.fingerprint` moves, as it does whenever the engine's own
files change -- and they do: the identity hashes every `.py` and `.mjs`
file in the package, so a change to the daemon, the health report, the
ingest or the Git wrapper moves it whether or not the extraction changed.

The two builds above used different `tree-sitter` versions (0.25.2 for the
installed 1.5.0 and 0.26.0 for the dev environment; `tree-sitter-python`
is 0.25.0 in both), so the difference was controlled for rather than
assumed away: the SAME tree built by both engines gives the same ids, the
same edges and the same triples, node for node -- 9,298 nodes, 25,599
edges and all 25,769 relation triples identical, with no id moved and none
removed, and the same resolution health. The figures above are therefore
attributable to this change and not to the parsers.

### Fixed

- Daemon: the health report no longer reads `ok` while every snapshot of an
  activated project fails (#113). A project the daemon could not snapshot
  was logged as `project_snapshot_failed` and then dropped: it never entered
  the supervised set, so it was absent from `status.json`, nothing counted
  it as failing, and `daemon-health` read `ok, 0 projects, 0 failing` while
  no graph was being maintained at all. On 2026-10-27 the daemon failed every
  snapshot of an activated repository -- ten failures across nine discovery
  cycles, 01:50:22Z to 02:03:59Z -- and the report said `ok` throughout. The
  daemon now keeps each failure -- its root and its reason -- under
  `snapshot_failures` in `status.json`, counts the project as failing, and
  names it with the reason in `daemon-health` and `daemon-status`. A
  `status.json` written before this key existed is read unchanged, and a
  failure whose reason is missing is still counted and named: the reason is
  absent, not the failure. A project whose snapshot failed is still never
  built -- with no snapshot there is no change set, and building on an empty
  one would publish an empty graph over a working one -- and one broken
  repository no longer blinds the daemon to the healthy ones beside it.
  Measured on this machine against a repository whose `.git` is not a valid
  repository, the failure the daemon hit on 2026-10-27: the health report
  goes from `ok`, `0 total, 0 failing` to `degraded`, `2 total, 1 failing`,
  naming the project and the reason, while the healthy repository beside it
  is still built.
- Daemon: a snapshot failure now logs the cause, not just the sentence
  (#113's follow-up). The daemon logged `str(exc)`, which for a failed
  enumeration is the constant `unable to enumerate Git repository safely` and
  nothing else: which step failed and which errno it was lived in the
  exception's `__cause__` chain, and the chain was discarded. On 2026-10-27
  the daemon failed every snapshot of an activated repository for thirteen
  minutes, and the log could not say why. The `project_snapshot_failed` and
  `project_scan_failed` events now carry a `cause`: the exception and its
  chain, rendered path-free -- a `GitError` by its own `diagnostic()` (class,
  step, errno) and every other exception by its class name alone, so no path
  and no Git text can reach the log. The chain is bounded and a cycle cannot
  make it loop. A Git command that runs and exits non-zero now leaves a
  cause too: Git's own text is discarded on purpose, and the run's stderr
  was never captured, but the exit status is an integer and is the only
  thing left that says which failure this was -- previously that error
  carried no cause at all.
- Tests: a stubbed module no longer outlives the test that made it. The
  boundary test in `tests/test_typescript_activation.py` installs fake `mcp`
  modules -- `mcp.types.Tool = object` -- to check that importing the MCP
  server never reaches activation orchestration, and then imports
  `graphite.mcp` against them. `monkeypatch.delitem(sys.modules, name,
  raising=False)` records NOTHING when the key is absent, so the
  `graphite.mcp` / `graphite.mcp_server` imported against the stubs stayed
  reachable after the test: in `sys.modules`, and as the ATTRIBUTE the import
  system sets on the parent package, which `from graphite import mcp_server`
  returns without importing anything. A later test in the same worker that
  read the module got the stub, and died with `AttributeError: type object
  'object' has no attribute 'model_validate'`. The serial suite never saw
  it, because every file that reads `graphite.mcp_server` sorts before the
  one that stubs it; the parallel suite does, because `--dist loadfile`
  reorders the work queue by test count per file (`--loadscope-reorder`
  defaults to on) and the stubbing file is the largest in the suite. The
  import is now undone in a `finally`, from both places it was reachable, and
  a regression test pins that.

### Changed

- The test suite runs in parallel. The suite is WAIT-bound, not CPU-bound
  -- the channel tests alone spend about 760s mostly sleeping on locks and
  broker round trips -- so serial execution paid for nothing.
  `pytest-xdist` is a declared dev dependency, which adds one
  `Requires-Dist` line to the wheel's metadata for the `dev` extra alone,
  and the pre-push gate runs the suite across six processes, distributed by
  file so that a file's tests share one process and the suite keeps the
  per-file semantics it has today. Measured on this machine, same
  interpreter, the same tree six tests apart: **1,190s serial becomes
  323s**, 3.7x faster, and green either way -- six tests cannot account
  for the difference. No assertion changes; only how many of them run at
  once. The gate's budgets are re-derived from that measurement rather than
  left stale: `timeout_s` 1800 -> 1200 and `pre_push` 2100 -> 1800.
  The gate also runs it with `--no-loadscope-reorder`, because `--dist
  loadfile` alone does NOT keep the suite's order: pytest-xdist reorders the
  work queue by test count per file unless that is turned off, so the
  parallel run stops matching the serial one. Measured on this machine, same
  tree, same interpreter: 412.5s with the reorder on, 329.9s with it off --
  not slower, because the suite is WAIT-bound rather than CPU-bound.

## [1.5.0] — 2026-10-26

A minor release: one fix to the agent hooks' savings line (#110), and with
it one environment variable added to the configuration surface. No command,
option, JSON field, exit code or entry point is added, removed or renamed,
the ledger's other fields and the hook's wire format are unchanged, and
`Requires-Dist` and the entry points are those of 1.4.1.

The graph changes by what this fix adds. Measured on this repository: the
1.4.1 release tree and this one, each built by the same interpreter with the
same parsers, into fresh output, cache and state directories:

- 9,221 nodes become 9,244 and 25,347 edges become 25,415. The 23 new
  nodes are the two functions this fix adds (`current_session_id`,
  `_entry_is_this_session`), the fixture that keeps the suite hermetic, the
  eighteen functions and the file of the new test file, and the
  `os.environ.get` call site;
- 6,664 of 6,691 definition ids survive naming the same definition at the
  same line, and 27 survive by id but move line -- every one of them in the
  three files this change touches (`agent_hooks.py` 3, `usage_ledger.py` 13,
  `conftest.py` 11). No definition id is removed and 21 are new;
- `contains` goes from 6,689 relation triples to 6,710 and `imports` from
  2,457 to 2,465; `calls` goes from 16,251 to 16,290; `inherits` (120) is
  unchanged. 25,517 relation triples become 25,585, with none removed;
- 18 cycles where there were 18, the same ones, and 177 communities where
  there were 179;
- two builds of this tree are byte-identical.

`metadata.engine.fingerprint` moves, as it does whenever the engine's
environment changes: it folds in the parser versions present, and the
interpreter's tree-sitter-python moved from 0.25.2 to 0.26.0 since 1.4.1's
build. The graph is unchanged by that: the same tree built by both parser
versions gives the same ids, the same edges and the same triples, node for
node.

### Added

**`GRAPHITE_SESSION_ID` names the session a usage-ledger entry belongs to**
(#110). This release adds to the configuration surface, so
`docs/reference/configuration.md` now lists the new variable together with
the two harness variables graphite falls back to when it is unset --
`CLAUDE_CODE_SESSION_ID` and `CODEX_COMPANION_SESSION_ID`. The first of the
three that is set is the one an entry records. Unset means the entry is
recorded untagged and every session counts it, as it did before.

### Fixed

**Two sessions open in one repository no longer count each other's use**
(#110). The usage ledger is per repository and every session appends to it,
and an entry said nothing about who made it. So where two sessions were open
at once each one's Stop counted the other's queries as its own: the second
session's Stop saw the first session's query, because that query was written
after the second session's books were opened, and the first session's next
Stop saw the second's. Each session reported the other's use as saved "this
turn", and both session totals ended at both.

An entry now records the session that made it, and a Stop counts only the
entries that name its own session. The tag is what the process can know:
`GRAPHITE_SESSION_ID` first, then `CLAUDE_CODE_SESSION_ID`, then
`CODEX_COMPANION_SESSION_ID`, the first that is set -- the same value the
Stop hook is given, so an entry and the hook that counts it name one session.
An entry no session can be attributed to -- a person running graphite in a
terminal in the same repository -- is counted as it was, so no real use is
dropped. The ledger's other fields, the hook's wire format and
`graphite savings` are unchanged: that command reports the repository's
totals, which is what it is for.

## [1.4.1] — 2026-10-25

A patch release: three fixes, two to the Python graph (#106, #107) and one
to the agent hooks' savings line (#108). No field, command or option is
added, removed or renamed, and `Requires-Dist` and the entry points are
those of 1.4.0.

The graph changes in this release. On this repository, measured against the
deployed 1.4.0 engine, with the same interpreter, from one tree:

- every definition and file node id survives (6,691 and 429), each naming
  the same definition or file, and none is new; 10 placeholder nodes are
  gone, so 9,231 nodes become 9,221;
- 59 call sites that ended at a placeholder for a name a module only
  imports now end at the function or class that name is (#107);
- 14 calls edges at 10 sites are gone: each was a `super().__init__` in an
  error class over a builtin exception, bound by name to an unrelated
  constructor (#106). `calls` goes from 16,265 to 16,251 edges, and the
  Python `calls` ratio from 0.978 to 0.983: 12,236 of 12,452 call sites
  are bound, where 12,191 of 12,466 were;
- `contains` is unchanged: 6,519 edges, and 6,689 relation triples, the
  difference being 170 edges that carry `contains` as a second relation
  beside `calls`. `imports` (2,457) and `inherits` (120) are unchanged;
- `analysis` lists 18 cycles where it listed 20: the two it no longer lists
  ran through those edges. Entry points, god nodes, orphans, surprising
  connections and top files are identical; the community count goes from
  174 to 179.

Two builds of this tree with either engine are byte-identical (`graph.json`,
`GRAPH_REPORT.md`, `graph.html`).

The entries below quote figures measured when each change was made, on
earlier commits of this tree, so they differ from the ones above.

### Fixed

- Python: a name a module only imports is followed to the file that defines
  it (#107). Where `front.py` binds `target` by `from pkg.impl import target`
  and nothing else, `front.target(x)` from another file, the local alias
  `t = front.target; t(x)` (#90) and `from pkg.front import target` each
  ended at a placeholder `pkg_front_py_target` that no file defines, so
  `callers target` left the caller out while `callees` of the caller
  answered `decision_grade` with that placeholder. #87 followed such a name
  through a package's `__init__.py` only, and only for `from pkg import
  name`: `import pkg; pkg.name()` stayed at the package. Each Python module
  now records the names it binds only by one top-level `from X import name`,
  by the rule #87 applies, and the dispatch post-pass follows them across
  files for as many hops as the source states: a call or `inherits` target,
  a stated receiver (`Base().step()`) and the bases a `self.m()` or
  `super().m()` is looked up along all end at the definition. A name the
  module binds any other way too is not followed, and neither is a
  submodule (`from pkg import sub`); a name a function of the module
  rebinds under `global` is no longer followed either, through
  `__init__.py` as elsewhere. A star import is not read: a name it could
  rebind is followed all the same. What a module records is on its own
  cached extraction, so a change to it reaches an unchanged caller with no
  new cache key. On this repository, the #106 engine against this one with
  one interpreter, from one tree: 59 calls edges, at 59 sites, move from
  10 placeholders to the functions and classes they name, and nothing else
  changes. Those placeholder nodes are gone, every definition id is the same,
  and the Python `calls` ratio goes from 0.978 to 0.983.
- Python: `super().m()` has no edge where the method it finds is a builtin's
  (#106). In `class StepError(AppError)` with `AppError(RuntimeError)`,
  `super().__init__(message)` runs `RuntimeError.__init__`, but the binding by
  class read the builtin base as one it cannot read, found no class of the
  repository with an `__init__`, and left the call to the name: it was a call
  to every `__init__` in reach, up to three, at `decision_grade`. On this
  repository 14 such edges went to constructors of unrelated classes, and two
  entries of `analysis.cycles` ran through them. Where no class the source
  states binds the name, every other base on the way is a builtin class the
  file does not bind, and a builtin (or `object`) has the name, the call now
  has no edge, as a call of a builtin has none. A name no builtin of the chain
  has, a base that cannot be read beside the builtin, a class with no base,
  and `self.m()` are bound as before.
- Agent hooks: a session's savings line no longer counts what other
  sessions ran before it started or while it was away (#108). The usage
  ledger holds every session's entries, and a session the stop cursor had
  not seen started reading it at byte 0, so its first Stop reported all
  earlier sessions' use as saved "this turn" and its session total carried
  that history from then on, and
  a resumed session counted everything other sessions had run since its
  last turn. SessionStart now opens a new session's books where the ledger
  ends, and on a resume moves a known session's books there, keeping its
  totals; a compact falls inside a turn and leaves them where they are. A
  Stop for a session with no books (its start was not seen, or it was
  pruned from the cursor) starts at the ledger's end too: that turn's own
  use is not counted, and no other session's is; neither is the use of a
  turn whose Stop never fired before a resume. Two sessions open in one
  repository at once still each count what the other ran between two of
  their turns: a ledger entry does not say which session made it.

## [1.4.0] — 2026-10-25

A minor release: eight fixes, to the Python graph, the build, the CLI and the
agent hooks, and one addition to the hook contract (#101).

The `hooks` check of `doctor --json` gains the field `chain_slots`, and
`graphite init` reports the same slots beside the hooks it installs; both are
additions under `docs/compatibility.md`. No field is removed or renamed,
`Requires-Dist` and the entry points are those of 1.3.0, and `graphite init`
writes what it wrote (`DOC_VERSION` 15): in a new repository, `init --all`
leaves 17 files, the three trampolines among them, and a `.git/config`, each
byte for byte as 1.3.0 leaves it.

The graph changes in this release. On this repository, measured against the
deployed 1.3.0 engine, with the same interpreter, from one tree:

- every definition and file node id survives (6,616 and 425), each naming
  the same definition or file, and none is new. This tree has no pair of
  paths that #82 tells apart;
- 24 placeholder nodes are gone and 4 are new, so 9,164 nodes become 9,144;
- 32 of the 120 `inherits` edges ended at a placeholder and now end at the
  class the base names (#86);
- `calls` goes from 16,065 to 16,066. 21 call sites through a local alias
  ended at a placeholder named for the local: 16 now end at a definition,
  and 5 at a placeholder for a name the module only imports, which is where
  a direct call of it already ended (#90; that gap is #107). One call site is
  new (#85). `contains` is 6,446 edges where 1.3.0 held 6,447, and 6,614
  relation triples on both sides: 168 edges carry `contains` as a second
  relation beside `calls` where 167 did. `imports` (2,431) is unchanged, and
  25,177 of the 25,230 edge triples are unchanged;
- the Python `calls` ratio goes from 0.976 to 0.978: 12,001 of 12,276 call
  sites are bound, where 11,984 of 12,275 were;
- `analysis` lists 20 cycles before and after, and one entry is new, in
  `git.py`: the new `inherits` edge closes a loop through a
  `super().__init__` call that both engines bind to the wrong `__init__`
  (#106). The entry it displaced is still a cycle in the graph. Entry
  points, god nodes, orphans, surprising connections and top files are
  identical, and so is the community count (177).

Two builds of this tree with either engine are byte-identical (`graph.json`,
`GRAPH_REPORT.md`, `graph.html`).

The entries below quote figures measured when each change was made, on
earlier commits of this tree, so they differ from the ones above.

### Added

**Where another tool's git hook goes is declared, not only written in
shell.** Each of graphite's trampolines (`post-commit`, `post-merge`,
`post-rewrite`) runs `<hook>.local` beside it first when that file exists. The
name was stated only inside the trampoline's text. When graphite was installed
first, nothing was in that slot, and a hook manager that would not chain to
graphite's trampoline left its own hook out entirely (channel rounds 322 to
324). The name is now part of the hook contract in `docs/compatibility.md`.
It is reported in two places:

- the `hooks` check of `doctor --json`, as `chain_slots`: for each trigger,
  the `file` relative to the hooks directory and whether it is `present`;
- the hook line of `graphite init`, in either install order.

An empty slot is not a finding. The trampolines are byte for byte those of
1.3.0. This adds a field to `doctor --json`, so the next release is a minor.

### Fixed

- Python: a name a package re-exports is followed to the file that defines it
  (#87). `from pkg import Base` used to bind to `pkg/__init__.py`, so a call of
  a re-exported function ended at a placeholder, and a `self.m()` in a class
  whose base was re-exported had no edge and was not counted, which let
  `callers` answer `decision_grade` without that caller. Only a plain
  top-level `from X import name` in an `__init__.py` is followed, for as many
  steps as the source states; a name the package binds some other way, a cycle,
  and a module other than `__init__.py` stay as they were. The Python cache key
  gains a digest of the re-exports, so an unchanged importer is re-resolved
  when an `__init__.py` changes.
- Python: an `inherits` edge ends at the class its base names (#86). Its far
  end was always a node made from the base's bare name, of kind `unknown`, so
  the graph held no link from a class to its base and every class of one name
  shared one placeholder. A base that is a bare name the file binds by a
  class statement or imports by name now ends at that class's node; a
  builtin, a dotted name or a call keeps its placeholder.
- Python: a call through a local alias binds to what the alias names (#90).
  `t = mod.target` followed by `t(x)` used to end at a placeholder named for
  the local, so `callers target` left that function out and still answered
  `decision_grade`. A name bound by one assignment of a bare name or of
  `module.attr` is read as that name or attribute, as the call written
  directly is, also through a chain of such assignments. A name bound a
  second time in its scope, a `with ... as` name, a cycle, and a longer chain
  such as `a.b.c` stay as they were.
- Build: two paths that differ only by `/`, `.` or `_` keep a file node each
  (#82). `a/b.py` and `a_b.py` both made `a_b_py`, so one had no node and its
  definitions hung off the other's. Only the paths of the tree that actually
  collide are marked, each with a hash of its exact path; every other file
  node id is what it was. A tree with such a pair re-extracts once, because
  the marked ids are folded into the cache key.
- Python: a bare call of a name the file binds gets its edge also when the
  name is a builtin of another language (#85). The list that drops calls of
  builtins holds JavaScript, Go and Rust names beside Python's, so a class
  `Error` or a function `make` had no callers in the graph and its call sites
  were absent from the `calls` ratio. A listed name that nothing in the file
  binds still gets no edge.
- CLI: `build` and `report` of a path that is not there say `path not found`
  and exit 1, as `scan` does, and create nothing (#88). The build lock made
  its cache directory first, which made the path, and a complete empty
  `graph-out/` was written into it with exit 0. `build --detach` is refused
  the same way. Below a repository root the message is still the one that
  names the root.
- CLI: `doctor` typed below a repository's root reports on the repository
  (#89). It took the folder for the repository, so in a subfolder of a
  repository with a fresh graph it said `Graph artifact is missing` and
  offered a build that is refused there, and its incident line read a ledger
  nothing writes. It now follows the root, as the read commands do (#81); a
  `.git` above that graphite does not take is not followed.
- Agent hooks: the strict hook reads files piped to `Select-String` as a
  search of them (#84). `Get-ChildItem -Recurse src | Select-String name` was
  let through as a filter, while `Select-String name -Path src` was refused.
  A listing (`Get-ChildItem`, `gci`, `ls`, `dir`) now hands its paths to a
  `Select-String` after it, also across `Where-Object`, `Select-Object` and
  `Sort-Object`, and the search is judged as the path form is. `-Name`
  listings, other commands' output and any other stage between them stay
  filters.

## [1.3.0] — 2026-10-16

A minor release, with a security fix: from a folder below a repository's
root, the agent hooks of 1.2.1 could take a directory another account made
for the repository (see Security). Upgrade.

JSON outputs that carry `schema_version` gain fields and codes, which are
additions under `docs/compatibility.md`: a `no_path` result is graded
(`answer`, `resolution`, `inconclusive`; #76); `check --json` gains the
reason `below_repository_root` and the field `repository_root` (#81); and a
Python `calls` answer may carry the caveats `python-member-call-bound-by-name`
(#79) and `python-self-call-bound-for-enclosing-class` (#73). `--graph-json`
left out below a repository root is now looked for at the root, and
`docs/compatibility.md` records why that is a fix and not a change of
default. `docs/reference/exit-codes.md` names the new failure and the new
reason. No field is removed or renamed, `Requires-Dist` and the entry points
are those of 1.2.1, and `graphite init` writes what it wrote (`DOC_VERSION`
15).

The graph changes in this release, in its edges only. On this repository,
measured against the deployed 1.2.1 engine, with the same interpreter, from
one tree:

- all 9,031 node ids survive, each naming the same definition, file or
  placeholder, and none is new;
- `calls` goes from 15,811 to 15,771: 44 call triples are gone and 4 are
  new. `contains` is unchanged: 6,337 edges, and 6,502 relation triples, the
  difference being 165 edges that carry `contains` as a second relation
  beside `calls`. `imports` (2,383) and `inherits` (120) are unchanged, and
  24,772 of 24,816 edge triples are unchanged;
- `analysis` (20 cycles, entry points, god nodes, orphans) is identical. The
  community count goes from 169 to 170;
- the Python `calls` ratio is 0.976 before and after. The cell counts 40
  fewer call sites (12,001 where there were 12,041), and 40 fewer bound, so
  every site that left it had been bound.

Two builds of this tree with either engine are byte-identical (`graph.json`,
`GRAPH_REPORT.md`, `graph.html`). On a tree where two files of one language
have the same content, the graph gains the file that had no node, with what
it defines (#74).

The entries below quote figures measured when each change was made, on
earlier commits of this tree, so they differ from the ones above.

### Added

**A "no path" answer is graded (#76).** `reaches` and `path` answer an
unreachable pair with the error `no_path`. Of everything those two verbs
return it is the one result that asserts an absence, and nothing graded it.
On a fixture where nine of a function's ten call edges are unbound, `callers`
on a node answered `inconclusive` and `reaches` to that same node answered "no
call path" as a plain error.

A `no_path` result now carries three more fields:

- `resolution`: how each end was matched, as a found path reports it;
- `answer`: the block every other answer carries, scoped to the languages of
  the two nodes the question resolved to;
- `inconclusive`: true exactly when `answer.grade` is `inconclusive`.

The grade is `inconclusive` where a cell the answer used is degraded or was
not measured, and `advisory` otherwise. It is never `decision_grade`, for the
reason an empty `callers` answer is not: a call the graph did not bind, or a
caller registered as a callback, leaves a real path out of the graph, and no
ratio can see an edge that was never emitted.

Where one end was a pick between spellings (`Verdict` typed, a function
`verdict` chosen), that end's `resolution` entry carries `case_ambiguous` and
the answer carries the caveat `target-resolved-to-another-spelling`. Before
this change the node id in the message was the only sign of the pick.

Not changed: `error_code: no_path`, the message, `truncated` and `limits`. A
`no_path` cut short by the depth bound still says `truncated: true`. Between
two files that are not code there is nothing to grade by, and the result
carries neither `answer` nor `inconclusive`, as before. The grade is taken
from the two resolved nodes and never from the whole graph: grading a
`no_path` against every language in the graph was the defect fixed on
2026-07-31, and it is why such a result then carried no block at all.

`graphite query` records a `query_inconclusive` incident for a `no_path`
graded `inconclusive`, as it does for every other inconclusive answer. It
used to skip every error result, which was right while a `no_path` was never
graded. Such a result carries no `resolution_health`, so the incident's detail
is the scoped cell and the grade (`answer calls(python) 0.1 inconclusive`).

These are fields added to a JSON output that carries `schema_version`, which
`docs/compatibility.md` makes a minor release. The sentences in
`docs/agent-integration.md` and in the `query-result` schema that said a
`no_path` is not graded are replaced with what it now carries.

**`check --json` says when it was asked about a folder below a repository's
root (#81).** The answer for such a folder, below the root of any Git
repository whether graphite is set up in it or not, is `"reason":
"below_repository_root"`, with one new field, `"repository_root"`: the
directory to run it from, or `null` where the `.git` above is not one graphite
takes (see Security). `stale` is `true` and the exit status is 1, as they
were for that folder, and `added`, `changed`, `removed` and
`resolution_health` are still there. `manifest` is not: it belongs to the
reason `missing manifest`, which this answer replaces for such a folder, and
no other reason carries it. The fix this belongs to is below.

### Fixed

**The strict graph-first hook sees a search behind a newline, a wrapper, a
subshell, a fused regex escape or a variable (#75).** Each of these passed a
search the hook refuses when it is written plainly. On a fixture with a
function `dependents` in `src/`, `grep -rn dependents src/` was refused and
every one of these was allowed:

- **A later line.** `ls`, then `grep -rn dependents src/` on the next line. A
  newline was read as a space, so the first word of the first line was the
  head of the whole command. Most multi-line commands have this shape.
- **A wrapper.** `sudo`, `env LC_ALL=C`, `time` and `command` before the
  search; `ls src | xargs grep dependents`; `find src -type f -exec grep
  dependents {} +`. Also read now: `nice`, `nohup`, `timeout`, and the shell
  keywords a search follows (`if`, `then`, `do`, `!`, `{`). A `cd` behind one
  of these is not followed: `env cd docs` moves a program that then exits, and
  the shell stays where it was.
- **A subshell.** `(grep -rn dependents src/)` and
  `wc -l $(grep -rln dependents src/)`, in backticks and inside double quotes
  as well. A subshell is read in the shell of the command that holds it. After
  `cd docs && (...)` it searches `docs/`, and it reads the variables set
  before it. Where it moves and what it sets stay inside it.
- **A fused escape.** The Grep tool with `\bdependents\b` or `\sdependents`.
  The escape and the name were read as one word, `bdependents`.
- **A variable.** `P=dependents; grep -rn $P src/`. The hook now reads a
  variable where the same command assigns it: through a chain
  (`S=src; D="$S/pkg"; grep -rn dependents $D`) and in PowerShell
  (`$p = 'dependents'`) as well. The value is dropped once something the hook
  does not model may have set the variable again: a command that names it
  (`read P`, `for P in ...`, `P+=...`), `source`, `.` and `eval`, or an
  assignment after `&&` or `||`, which runs or does not.

One more was not in the issue, and it hid a search on a single line too:

- **A `#` inside a word.** `echo a#b; grep -rn dependents src/`, and
  `[ $# -gt 0 ] && grep -rn dependents src/`. The lexer took any `#` for the
  start of a comment and dropped the rest of the line, search and all. A
  comment starts only at a `#` that starts a word.

Three things changed with these, because reading every line made them matter:

- **A pipeline runs where it started.** `cd docs && find . -type f | xargs
  grep word` searches `docs/`. Every stage of a pipeline is read in the
  directory the pipeline started in.
- **A PowerShell path keeps its backslashes.** A PowerShell command was read
  with the POSIX escape, so `-Path src\pkg\core.py` lost its backslashes,
  named no file, and a search of that one file was refused. The backtick is
  the escape there now.
- **A PowerShell block comment is text.** What stands between `<#` and `#>`
  is not read as commands.

What is text stays text: the body of a heredoc, a PowerShell here-string,
quoted strings and comments are not read as commands, so
`git commit -m "$(cat <<'EOF' ... EOF)"` with a search quoted in the message
is allowed as before. `graphite query ... | grep name` still filters. A search
of one file, of a directory with no code, of a path outside the repository,
and of a word that differs in case from the symbol are allowed on a later
line and behind a wrapper exactly as they are when written plainly.

A search on a later line is held to the rules a plain one is. One of them is
that a search whose path the hook cannot read is refused when its pattern
names a symbol: a path in a variable the hook could not expand, or a path
after a `cd` it could not follow. Such a search on a later line was allowed
only because the line was not read.

Limits, stated in `docs/agent-integration.md`. Each of these is allowed, as it
was:

- a pattern in a variable set in the user's profile, by an earlier command,
  from a command's output or as a prefix to the search itself
  (`P=name grep $P`). The variable is not expanded, so the hook has no name
  to check. A path in such a variable is the refused case above;
- a search behind a wrapper flag that takes a word
  (`sudo -u someone grep ...`), or with its tool named by a substitution
  (`$(which grep) ...`);
- a search in a command handed to another shell as a string
  (`bash -c 'grep ...'`, `eval`);
- a search in a shell function written on one line (`f() { grep ...; }`). A
  body on lines of its own is read, whether or not the function is called;
- a substitution inside the body of a heredoc. The body is read as text, and
  where its delimiter is not quoted the shell runs a `$(...)` in it;
- in PowerShell, files piped to `Select-String`
  (`Get-ChildItem -Recurse src | Select-String name`). A command after a pipe
  is read as a filter, `xargs` apart.

The hook still fails open on a command it cannot read.

**Two files with the same bytes each keep their place in the graph, and a
moved file is named where it is (#74).** The extraction cache keyed an entry
on a file's bytes and its language. What an entry holds names the file: every
node id and every `source_file` in it is built from the path. So two files
with the same bytes shared one entry, and one of them was answered with the
other's nodes and had none of its own.

- **On every build after the first**, one file of each such group was in the
  graph and the others were not, with everything they define and every edge
  from them. A tree of seven files, six of them in identical pairs (two
  modules, two templates, two empty `__init__.py`), went from nine nodes to
  five on a second build with nothing changed.
- **On a first build** it was a race between worker threads, which is how the
  issue was filed: one tree, built twice from an empty cache, gave two node
  sets.
- **An import of a package whose `__init__.py` is empty** pointed at a file
  node that was not there, once another empty `__init__.py` had been read.
- **A file moved with its bytes unchanged** kept its old path in the graph.
  Python and TypeScript were spared, because the set of paths is already in
  their key. Go, Rust, templates and every other file were not, and neither
  were two Python files that exchange their contents.

The issue was filed as file nodes with no edges coming and going. It was
wider than that: definitions went with their file. Measured on Django 5.2.7,
1.2.1 against this change, each built from an empty cache two or three times
and then once more on the cache it had left:

- 1.2.1 gave three different graphs, with 109, 110 and 118 files of a kind
  the graph takes left without a node;
- this change gave one graph four times, the same `graph.json` byte for byte.
  Nothing a 1.2.1 graph holds is missing from it. It gains 128 to 137 nodes
  and 166 to 168 edges, and 24 of the nodes are classes and functions in
  Python files that had a twin.

The file's path is now part of the key. An entry belongs to one file, an
unchanged file is still answered from the cache, and nothing else about the
cache changes. The first build after the upgrade extracts every file again,
as after any change to the engine. On a tree where no two files of one
language share their bytes the graph is what it was: this repository builds
the same 8,655 nodes and 23,691 edges before and after.

Five Django files still have no node, and none of them is this defect: three
are binary, one is under a dot-directory, and one has the node id of another
path beside it (`file_txt` and `file.txt`).

**A Python member call is bound by what its receiver is, where the source
says so (#79).** A member call (`obj.m()`) was bound by the name `m` alone: to
every method of that name in the caller's file and the files it imports, up to
three. So `Beta().setup()` was also a call to `Alpha.setup`, and
`callers Alpha.setup` listed a function that only ever builds a `Beta`, at
`decision_grade`. With one method of the name in reach the call was simply
pointed at it: `f.read()` on a file opened on the line above was a call to
the repository's one `read` method.

Where the source states the receiver, that now decides:

- **One class.** A class called to build the object (`Beta().m()`), the class
  itself (`Beta.m()`), or a local name bound only from such a call
  (`x = Beta()`, then `x.m()`) is bound to that class's own `m` and to nothing
  else. The class may be defined in the file or imported by name.
- **Not an object of the repository.** A literal (`", ".join(parts)`), the
  result of a builtin that returns exactly a builtin type (`open(p).read()`,
  `set(x).add(y)`), or a local name bound only from those
  (`with open(p) as f:`) has no method the repository defines. The call gets
  no edge.

No edge, and not an `EXTERNAL_CALL` edge. That tag is for a call whose NAME
is shown to leave the repository (`re.compile`, `expect`), and its placeholder
node carries that name. Here the name on the edge is a local variable's
(`f.read`), so a placeholder would say nothing, and for a literal the
placeholder is a function of the module (`", ".join(x)` beside a `def join`),
which is the false edge over again. No edge is also where a member call that
binds to nothing already ends: the dispatch pass drops one with no method of
the name in reach, and the noise list drops `parts.append(x)` before that. So
these calls are not counted under `external`.

A name is "bound only from" a value when every statement that binds it in its
scope says the same; `None` is skipped, because it has no method to call.
Flow is not followed.

Bound by name as before: a parameter,
annotated or not, because an annotation admits a subclass; an attribute
(`self.x.m()`); what any other call returns; a method the stated class
inherits and does not define; `a = b = Beta()` (only `b` is read);
`(x := Beta())`; `y = x`; what `with Beta() as x` hands over; a builtin's
name in a file with a star import, which may bind it; and `str(x)`,
`repr(x)`, `format(x)` and `bytes(x)`, which return whatever the argument's
own method returned. `self.m()` and `super().m()` are the next entry.

Measured on Django 5.2.7 (2,224 Python files), with every member call of the
1.2.1 engine set beside the same call under this change, and each receiver
read a second time from CPython's own `ast`:

- 201 calls on a builtin object that were bound to a method of the
  repository have no edge (`"...".format()` was a call to
  `NumberAwareString.format`, `s = set(); s.add(x)` to `Collector.add`);
- 6 calls of a string's method that were recorded as calls to a module
  function of that name have no edge;
- 327 calls bound to their own class's method and to one or two others keep
  the one;
- 250 calls that had no edge, because more than three methods share the
  name, are bound to their own class's method;
- no call changed whose receiver the source does not state.

On this repository: 3, 0, 45 and 3. The three are `read` called on an opened
file, which had put two functions that only read a file among the callers of
`Cache.read` (10 rows before, 8 now). `callers Cache.write` is unchanged at 17
rows: it still lists a function that writes to a stream it is handed as a
parameter. That row is false, it is bound by name, and the answer now says
that a member call may be.

What is still bound by name is now said on the answer: every Python `calls`
answer carries the new caveat `python-member-call-bound-by-name`, next to
`python-member-call-over-dispatch-cap`, which is the same pass over the cap.
A new caveat code is an addition to a JSON output that carries
`schema_version`.

**`self.m()` and `super().m()` are bound by class, where the classes say
which method runs (#73).** A call on `self` or on a bare `super()` was bound
like any other member call, by the name `m` alone: to every method of that
name in the caller's file and the files it imports when there are at most
three, and to nothing when there are more. So `self.get_queryset()` in a
class with a `get_queryset` of its own was a call to two other classes' as
well; `super().__init__()` was a call to the `__init__` it is written in; and
for the names many classes share (`__init__`, `get_queryset`, `to_dict`) the
call had no edge at all. That last part is the issue as filed: since every
method has a node of its own (1.2.0), more names are over the three.

Python says which method runs: the first `m` in the linearisation of the
object's class, from the class for `self` and after it for `super()`. The
call is now bound to that method, for an object of the class the call is
written in, where the classes graphite reads state which it is:

- **Every base read.** Where each base of the class, and of its bases, is a
  class graphite can read (a name that is a class of the file, or one
  imported by name from the file that defines it), the linearisation is
  computed as Python computes it (C3).
- **A base not read.** A builtin, a dotted name (`models.Model`), a name
  that reaches the file through a re-export, a call, a subscript. Such a
  base may have an `m` of its own. It may also derive from a class that WAS
  read, and Python then puts that class later than the readable bases
  suggest: `class Child(Left, Right)`, `Left(Top)`, `Right(Unread)` reads as
  Child, Left, Top, Right, and is Child, Left, Right, Unread, Top when
  `Unread` derives from `Top`. So a method is bound only if what is written
  puts its class before every other class that binds `m` and before every
  base that was not read. What is written is two things Python keeps
  whatever the rest is: a class comes before its bases, and the bases of a
  class keep their order. So the class's own `m` is bound, and the first
  base's (`class Child(Mixin, models.Model)`: `self.m()` is `Mixin.m`), and
  a single chain up to the class whose base was not read.

A method found this way is bound whatever file it is in and however many
methods share its name. The reach rule and the cap of three exist to limit a
guess by name, and here the bases are the evidence.

**A class is more than the `def`s in its body.** Where the source shows
another way for a class to get a method, the `def`s do not say which method
runs, and the call is bound by name as before:

- a decorated class on the way, its own `def` included. `@dataclass` makes
  `__init__`, and any decorator may set or replace a name. No list of
  decorators that leave a class alone is kept, and that has a price: an
  engine that trusts every decorator binds 419 more calls on Django and 6
  more on this repository. Telling such decorators from the rest is a
  follow-up;
- a class made with a keyword (`metaclass=M`, or any other but
  `metaclass=type`) among the classes read, or an `__init_subclass__` on one
  above the class: either runs for every class below it;
- a class whose name its scope binds more than once: `C = deco(C)`, an
  import of the name beside the class, or the class written twice under an
  `if`, which is two classes and one node of the graph. Nothing is bound in
  such a class, and for a class derived from it, it is a base that was not
  read;
- `m` set on one of the classes from outside its body in the same file
  (`K.m = ...`, `del K.m`), or by one of its class methods (`cls.m = ...`);
- a first class that binds `m` some other way than one `def` written
  straight in its body: an assignment, a property object, two `def`s, a
  `def` under an `if`, a `try`, a loop or a `with`, or under `global`;
- for `self`: a method of any class read assigns `self.m`, because an
  instance attribute shadows a method; or one of them has a
  `__getattribute__`;
- `m` behind a base that was not read, or nowhere among the bases:
  `super().__init__()` under `class E(Exception)` is still a call to
  whichever `__init__` methods are in reach;
- a `__private` name, except on the class the call is written in;
- `super(X, obj)` with arguments; `cls.m()` in a class method, in `__new__`,
  `__init_subclass__` and `__class_getitem__`; a `self` that is given another
  value; `super()` in a lambda, a nested function or a comprehension; a
  `super` the file binds.

**What is not read.** A call bound by class is bound to the method that
runs where none of the following applies. graphite does not see these, and
binds all the same:

- anything behind a base that was not read. A metaclass, an
  `__init_subclass__` or a decorator there can replace even the class's own
  `def`, and a method there can assign `self.m`;
- `setattr`, `K.__dict__`, `type(self).m = ...`, `self.__class__ = ...`, and
  an assignment made in another file;
- an alias: `me = self; me.m = f`, or `K = C; K.m = f`. Flow is not followed;
- a decorator on a method. The call is bound to the `def`, whatever the
  decorator returns, and the method the call is written in is taken to be
  handed the instance, whatever decorates it;
- a class decorator that returns a class with other bases than the one it
  was handed;
- a star import written after the class statement, which may bind its name
  again.

**Not followed: what runs for an object of a subclass.** For such an object
`self.m()` runs an override below the class, and `super().m()` can reach a
class the subclass added to its bases. Those edges are missing, and every
Python `calls` answer says so with the new caveat
`python-self-call-bound-for-enclosing-class`. Bound by name, an override was
listed only where its file happened to be within reach, beside methods of
unrelated classes. `python-member-call-over-dispatch-cap` and
`python-member-call-bound-by-name` stay: calls on any other receiver are
bound by name, and so are these wherever the classes do not state the
method.

Measured on Django 5.2.7 (2,224 Python files; 47,654 calls on `self` and
1,705 on `super()`). Each of them under the engine before this change was
set beside the same call after it, and the method that runs, for an object
of the class the call is written in, was worked out from a fuller reading
than the engine has: re-exports and dotted bases followed, builtin and
standard-library classes taken as Python holds them, and "not known"
wherever a class of the tree may get the method without a `def`.

- 1,742 calls changed. 1,262 are now bound to exactly the method that runs.
  425 are bound to the method the classes state under a standard-library
  class whose own hook sets no method: 420 under `unittest.TestCase`, which
  has an `__init_subclass__` since Python 3.11, and 5 under `abc.ABCMeta`.
  For 55 the fuller reading still has a base it cannot read, or finds a
  metaclass behind one, so what runs is not known, and they are bound as the
  rule above says. Where the method that runs is known, no call bound by
  class is bound to another.
- 659 of them had no edge (609 with more than three methods of the name);
  1,065 were bound to two or three methods and keep one; 18 were bound to
  one method and are bound to another.
- 1,332 bindings went. 271 were a `super()` call bound to the method it is
  written in; 714 were methods of a class neither above nor below; 178 were
  a base class's method behind the one that runs. 168 were an override in a
  subclass, which does run for an object of that subclass: that is the loss
  the new caveat names. One was a method whose name differs by case.
- Of the 12,570 calls for which the fuller reading names a method of the
  tree and no class on the way may get one without a `def`, 7,258 were bound
  to exactly it before and 8,945 are now. 3,541 have no edge, and for 3,443
  of those the one thing in the way is a base imported through a re-export
  (`from django.test import TestCase`), which graphite does not read as a
  class.

The issue's table, on Django's `django/` package alone (15,563 member
calls): calls with more than three methods of the name and no edge, 3,413
before and 2,872 after; among them `self` calls 327 and 128, `super()` calls
772 and 435. On this repository: 192 and 191, `self` calls 4 and 4. Those
four are `self.to_dict()` in `@dataclass` classes: the price above, at home.

`graphite impact <symbol>`, every definition asked of both graphs: on Django
304 of 27,513 methods answer differently (89 lose an impacted file, 160 gain
one; 32 lose a likely test, 47 gain one), with 9 of 10,546 class nodes and 9
of 2,751 functions; on this repository 4 of 809 methods and nothing else.
`impact <file>` is unchanged for every file of both. The built graphs keep
their nodes: Django 854 edges gone and 653 new, this repository 15 and 1.
The Python `calls` ratio is 0.697 before and 0.696 after on Django, because
an edge bound by name to the wrong method counted as bound, and 0.976 on
both sides here.

Also in the test suite: classes made at random, some behind names graphite
cannot read, some given a method by a decorator, a metaclass, a hook or an
assignment, are run by Python itself, and each call graphite binds by class
is held against the method that ran.

**A call bound by name looks only in the files it can reach.** A Python
call bound by name goes to the methods of that name in the caller's file and
the files it imports. The engine found them by testing every method of the
name, wherever it was, against that reach, and did so again for every call:
in a tree where many classes share a name (`__init__`, `get`, `step`), what
one call cost grew with the whole tree. The methods of a name are now held by
the file they are in, and a call looks in its own file and the files it
imports. The filter that keeps a call among the methods of its own language
family, in every language, is worked out once for each name and family
instead of once for each call.

What a call is bound to is unchanged. The extraction of this repository, of
two generated trees of 400 Python files of class hierarchies, and of a small
tree that calls one name from Python and from TypeScript, which is now a
test, is identical, node for node and edge for edge, under the engine before
this change and after it. Measured once each, on one machine: extracting
either generated tree takes 2.9 or 3.1 seconds where it took 4.3 or 4.5; and
the test that holds each call bound by class against the method Python runs,
which extracts such a tree, ran past the 120 seconds the CI run allows one
test under Linux, Python 3.12 and coverage with branch measurement, and now
takes 7 to 9 seconds there.

**The CLI works from a folder below the repository root (#81).** A command
took the directory it was typed in for the repository. One `cd` below the
root, with the graph fresh at the root:

- `query`, `search`, `context` and `impact` exited 1 with `graph unavailable:
  graph_missing`, and `validate` with `graph not found`.
- Each of those runs wrote a `.graphite/local/` ledger into the subfolder and
  registered the subfolder as an open project, which no build can run in (the
  failure 1.2.1 describes for the hooks).
- `check .` said the graph was stale (`missing manifest`), which sends the
  caller to a build. `build .` made `.cache/graphite/` in the subfolder and
  was then refused with "nested Git roots are unsupported".

The repository is now found by the rule the agent hooks have used since
1.2.1 (#80): the nearest directory upward that holds `.git`, taken only where
graphite is set up there (a `graph-out/graph.json`, or the `GRAPHITE.md` that
`init` commits) and, new in this release for the hooks as well, only where it
is the user's own (see Security). The rule is in one place,
`ingest.repository_root`, and the hooks call it. In a folder of no checkout, and below a checkout graphite is
not set up in, a read uses its own directory, as it did. A repository inside
the repository, with its own `.git`, stays its own root.

Below the root of a repository that is set up:

- **Reads follow the root.** `query`, `search`, `context`, `impact`,
  `validate` and `savings` answer from the repository's graph and say what
  they say at the root.
- **`--graph-json` left out is the repository's graph.** The option shows the
  default it always showed, `graph-out/graph.json`, and
  `docs/reference/cli.md` does not change. What changes is where that path is
  looked for when the option is left out: at the repository's root, not in the
  directory the command is typed in. `docs/compatibility.md` now records why
  that is a fix and not a change of default. Typed out, the option means what it
  meant: a path from the directory it is typed in, which must lie inside that
  directory. The root's graph is never read in place of a file that was named.
- **A file argument is read the way a shell reads it.** `context core.py`
  typed in `app/` is `app/core.py`. A path from the root typed below it
  (`context app/core.py` in `app/`) still works, and so does a node id or a
  name; where both readings name a file, the one from the working directory
  wins. Only a file is read this way, and from the text typed: no link is
  followed. Beside `--graph-json` the arguments are left as typed.
- **What a read records goes to the repository**: the usage ledger and the
  `query_inconclusive` and `graph_load_failed` incidents. `incidents list`,
  `ack` and `resolve` work on the ledger of the repository their path is in.
  A `.graphite/` that these commands left in a subfolder under an earlier
  version is no longer written or read by them, and can be deleted.
- **A run registers the repository**, not the subfolder. Below a checkout
  graphite is not set up in, a run now registers nothing, where it registered
  a folder no build can run in.

**`build`, `report`, `scan` and `check` name a path and do not follow the
root.** Given a path below the root of any Git repository, set up for graphite
or not, they stop before anything is written and say where the root is:
"`<path>` is below the root of its Git repository; graphite builds and checks
a repository from its root: run it from `<root>`". `check --json` still
answers in JSON (see Added). Below a checkout graphite is not set up in, this
too is new: `check .` used to say the graph was stale (`missing manifest`),
and `build .` left `.cache/graphite/` behind before it was refused. Below a
`.git` that is not the user's own, or one at a drive or filesystem root, they
refuse as well but name no place to run (see Security).

One thing changes at the root as well, because a file argument is read by one
rule wherever it is typed: `context ./app/core.py`, and `context` given a
whole path, did not find the file and now do. `impact` likewise.

The agent hooks told a session below the root to run graphite from the root,
every command of it. They now say that queries work from where the session
is, and that a build or a check is run from the root.

Not changed, and still wrong below a root: `doctor` given such a folder
reports on the folder ("Graph artifact is missing", with `graphite build .`
as the remedy), and its incident line reads the folder's own ledger, which
nothing writes any more. `watch`, `review-changes` and the MCP server take
the path or the directory they are given, as before.

### Security

**A repository root is taken from below it only when it is the user's own.**
From a folder below a repository's root, the agent hooks since 1.2.1 and, in
this release, the CLI's reads take the nearest directory upward that holds
`.git` for the repository: they read its graph, write its ledger, and
register it with the daemon, which then builds it. Nothing checked who owned
that directory or its `.git`, so one made by another account on the same
machine, anywhere above the folder, could be taken. Such a directory is now
taken only when the user running graphite owns it and its `.git`, both as
named and where a link to either leads (the owner of a link is whoever made
it, and the hooks name the directory as the session does, links and all),
and never when either is a drive or filesystem root. Anywhere else the
folder stands for itself, as it did before 1.2.1 for the hooks and before
this release for the CLI. `build`, `report`, `scan` and `check` still refuse
below such a `.git`, and no longer name it as the place to run;
`check --json` gives `"repository_root": null` there.

Who owns a path is asked of the operating system (`graphite.ownership`): on
POSIX the effective uid; on Windows the owner SID, which must be the user's,
or the Administrators group while graphite runs with an elevated
administrator's token. Where the owner cannot be read or is not the local
user, as can happen on network shares and on volumes without permissions,
or where a repository belongs to the Administrators group and the token is
not elevated, or where graphite runs under `sudo`, the folder stands for
itself: queries below the root then fail as they did before #81, and are run
from the root instead.

Found by aramid's review of #81, before release.

## [1.2.1] — 2026-10-11

A patch release. It carries one fix, to the agent hooks (#80). No stable
surface in `docs/compatibility.md` changes: nothing under `docs/reference/` or
`docs/schemas/` moves, `graphite agent-hook` still always exits 0, and the
wording of a hook's message is not a stable surface. The graph itself does not
change: on this repository, measured against the deployed 1.2.0 engine,
8581 of 8581 node ids survive, 23612 of 23612 edge triples are unchanged,
every relation count is equal, and `analysis` is identical. Only the engine
fingerprint moves, because graphite's own bytes did.

### Fixed

**The agent hooks find the repository from a subfolder (#80).** A hook payload
carries the session's directory, and in Claude Code that follows a `cd` in the
shell. All three hooks took it for the repository root. One directory below the
root, with the graph fresh at the root:

- `session-start` said `Graph status: missing`, the status that licenses a full
  fallback from graph-first. The remedy it printed, `python -m graphite build .`,
  is refused there ("nested Git roots are unsupported").
- `pre-tool-use` wrote nothing: no refusal in strict mode and no reminder. A
  `cd` into any subfolder switched the gate off.
- `stop` created a `.graphite/local/` in the subfolder for its cursor and looked
  for the turn's usage there.
- The subfolder was registered as an open project. The daemon cannot build
  one, so it logged `project_snapshot_failed` for it at every discovery cycle,
  and the repository's own marker was no longer renewed.

The repository is now the nearest directory upward that holds `.git`, the test
the build already uses to refuse a nested path (`ingest.enclosing_git_root`).
It is taken only where graphite is set up: a `graph-out/graph.json`, or the
`GRAPHITE.md` that `init` commits. In a subfolder of any other checkout, and in
a directory outside every checkout, the hooks read the session's directory as
they did. A repository inside the repository, with its own `.git`, stays its
own root.

One reading changes inside a checkout that is set up. A folder there that has
its own `GRAPHITE.md` or `graph-out/` but no `.git` was read as a project of
its own: `Graph status: missing`, or `unknown` over a leftover graph, with a
`build .` that is refused there. It is now read as part of the checkout.

Below the root:

- The graph, the freshness check, the usage ledger and the activation marker
  are the repository's.
- A search's relative paths, and a search that names no path, belong to the
  directory the shell is in. `grep <symbol> core.py` beside the file is one
  file. A search with no path in a `docs/` that holds no code is text. `..` and
  `../src` are judged as the directories they are.
- Once the command line has run a `cd` the hook does not follow, or any command
  it does not model, the shell may be anywhere in the repository. A search then
  keeps an absolute path, and a relative one that names a file in the session's
  directory. With a directory, or no path, it is judged as a search of the
  whole repository: from a `docs/` with no code, `cd ../src ; grep -rn <symbol> .`
  is refused. At the root this case resolves paths against the root, as before.
- Every status, refusal and reminder ends by naming the root: "This session is
  below the repository root: run graphite from `<root>`." The CLI still looks
  for the graph in its own working directory (#81), so the commands those
  messages print work from the root only.

The hooks no longer register a directory below a checkout's root with the
daemon, whether or not graphite is set up in that checkout.

Nothing changes for a session at the repository root. Measured on the 1.2.0
wheel and on this change: 32 strict-hook arms from the #72 work (25 refused, 7
allowed) and 12 hook arms on a checkout with `.git` and `GRAPHITE.md` read the
same on both.

## [1.2.0] — 2026-10-10

A minor release. Query and search results, channel rounds and Python
definition nodes gain fields, which are additions under
`docs/compatibility.md`. No field is removed or renamed, and nothing under
`docs/reference/` changes. Python node ids change, which that document allows
when the CHANGELOG says so: **do not persist node ids across this upgrade.**

The graph changes in this release. On this repository, measured against the
deployed 1.1.1 engine:

- 4,776 of 5,716 definition ids keep both their id and their definition. 937
  are gone and 1,322 are new. No id names a different definition. 3 survive
  as strings and now name the placeholder for a call made through a local
  name (`poll = getattr(process, "poll", None)`, then `poll()`), which 1.1.1
  bound to the method of that name;
- all 408 file ids survive, and 2,046 of 2,049 placeholder ids;
- nodes go from 8,173 to 8,555 and edges from 23,073 to 23,363. `calls` goes
  from 14,758 to 14,994 and `contains` from 6,042 to 6,096; `imports` (2,310)
  and `inherits` (118) are unchanged. 18,983 of 23,228 edge triples are
  unchanged;
- `analysis.cycles` goes from 20 to 19, and the community count from 170 to
  171;
- the Python `calls` ratio goes from 0.978 to 0.975.

Two builds of this tree are byte-identical (`graph.json`, `GRAPH_REPORT.md`,
`graph.html`). The parser version plays no part in these figures: each engine
gives the same graph under tree-sitter 0.25.2 and 0.26.0.

The entries below quote figures measured when each change was made, on earlier
commits of this tree, so they differ slightly from the ones above.

`graphite init` now writes `DOC_VERSION` 15. Re-run it in each repository to
refresh the managed files.

### Changed

**Consumers use graphite only as a tool, and the agent channel carries only bug
reports and recommended improvements.** A consumer has the tool, never the
agent behind it, so anything that needs an agent to answer cannot exist in a
real installation. The files `graphite init` writes (`DOC_VERSION` 14 -> 15:
`GRAPHITE.md`, the `CLAUDE.md` / `AGENTS.md` pointer, the Cursor rule) now
say so, and stop telling agents to ask other agents. Version 14 told them "If
you need a fact from another repo, ask for it" and to "ask the owning agent to
look". Facts about a tool come from the tool: its CLI, its MCP tools and its
documentation. The `graphite_channel_post` description states the same rule
when an agent posts. `graphite channel --help` states it for an agent that
has the CLI but not the MCP tools. Re-run `graphite init` to pick it up; the
sentence "The channel carries only bug reports and recommended improvements."
marks a refreshed file.

### Added

**Channel rounds carry a `kind`: `bug_report` or `improvement`.**
`graphite_channel_post` takes it, and the round records it. These surfaces
show it:
- `graphite_channel_list` and `graphite_channel_read`;
- `graphite channel list` and `graphite channel report`, as text and as
  `--json`;
- `graphite channel show`.

The text views tag a classified round as `[bug_report]` or `[improvement]`
before its title. JSON carries `null` for a round posted without one, which is
every round before this release. Any other value is refused with
`invalid_kind` before a round number is spent.

**Two Python call-binding blind spots are now declared on every Python
`callers` / `calls` answer**, the day they were confirmed (#70, #71):

- `python-nested-name-shared-id`: nested functions and methods share one node
  per file and short name, so a call to or from one may be attributed to
  another definition of the same name;
- `python-bare-call-ignores-scope`: a bare call through a parameter or other
  local binding, or inside a method body, may be bound to a same-named module
  function or sibling method.

Both produce wrong callers at `decision_grade`. Measured on graphite's own
code with the real extractor against CPython `symtable`: 118 of 12,082
bare-name calls bound to the wrong definition, and 1,345 call sites credited
to the wrong caller; on Django 5.2.7, 76 and 6,614. They are scope disclosures,
present on empty and non-empty answers alike, because the wrong binding is
made during extraction and no single answer can tell whether it is affected.
Both are retired in this same release by the fix under "Fixed".

**A new Python `calls` caveat, `python-member-call-over-dispatch-cap`, declared
the day it was measured (#73).** A member call (`obj.m()`, `self.m()`,
`super().m()`) whose method name has more than three definitions in the
caller's file and the files it imports gets no edge. So a `callers` or
`impact` result on a method may be missing real callers. It is a scope
disclosure, present on every Python `calls` answer; the measurement is under
"Fixed".

**Query by qualified name.** A dotted Python qualified name (`Worker.run`,
`test_one.fake_build`) now selects that definition in `query`, `context` and
`impact`, as match type `qualname`, tried after `exact-id` and before `name`.
Among same-named definitions at equal path depth, a bare name picks the
module-level one, then a method, then a nested definition. Two published
schemas gain an optional field, an additive change:
- `search-result` rows carry `qualname` when it differs from `name`;
- `query-result` `resolution[]` items carry `alternates_total`, the number of
  alternates. `alternates` still lists at most three (four for `path-suffix`).

### Deprecated

**Posting a channel round without `kind`.** It still lands, with a
`kind_missing` warning in the result naming the replacement, and becomes an
error in the next major release (`docs/compatibility.md`: the channel
protocol is a stable surface).

### Fixed

**Python methods and nested definitions get their own node, and bare-name
calls bind the way Python resolves them (#70, #71).**

- Every `def` and `class` that is not at module scope now has a scope-qualified
  id, built from its qualified name (`Worker.run`, `test_one.fake_build`), so
  same-named methods and nested helpers stop sharing one node.
- Module-scope ids are unchanged.
- Every Python definition node carries `qualname`.
- A bare call now resolves in the scope it is made in: parameters,
  assignments, loop and `with` targets, `except` and `match` captures,
  comprehension targets, function-local imports, `global` and `nonlocal`.
  Enclosing class scopes are skipped from inside a method.
- A call through a local name is kept as an unbound edge rather than bound to
  a same-named definition.
- A call on a module name bound by two different imports is kept as an unbound
  edge rather than guessed by method name. Examples: `import pkg` beside
  `import pkg.sub`, or an in-repo module with an external fallback. 1.1.1 bound
  the first shape through its file-wide alias map; neither corpus has one.
- A file's Python node ids are distinct by construction. A module-level def
  spelled like a scoped id (`def worker_run_7c6b1a()`) no longer merges into
  `Worker.run`; the scoped id is re-salted instead. Real names never collide.

Measured with `scripts/pyscopeoracle.py` against CPython's `symtable`:
- wrong-target bare-name calls went from 118 to 0 on graphite and from 77 to 0 on
  Django 5.2.7;
- 1,075 call sites on graphite (4,411 on Django) were credited to a different caller
  before this change.

The oracle grades bare-name calls only; member calls (`obj.m()`) are not
graded. Its figures differ slightly from the census quoted under "Added":
- Django's 77 is the census's 76, plus two generator-target sites the census
  missed, minus one site the oracle reports as rebound and does not score;
- 1,075 (4,411) counts only call sites that produce an edge, while 1,345
  (6,614) also counts calls that produce none, such as `len()`.

Python node ids changed:
- 4,505 of 5,420 definition ids survive with the same meaning on graphite;
- 8,959 of 34,609 survive on Django (the whole sdist, tests included);
- id strings that now name a different definition: 0 on graphite and 6 on
  Django. Each of the 6 is a module-level function whose id a same-named method
  had taken.

Do not persist node ids across this upgrade. graphite's own node-id holders
were audited: the incident ledger, daemon manifest, `check` records, rollback
store, usage ledger, overlays and extraction cache. None reads a node id back
across an engine change.

The Python `calls` ratio moved from 0.977 to 0.975 on graphite (0.704 to 0.700
on Django). The change was not broken down by cause. The candidates are:
- local-value calls kept as unbound edges;
- split nodes no longer sharing deduplicated edges;
- over-cap member edges leaving the denominator.

The retired caveats `python-nested-name-shared-id` and
`python-bare-call-ignores-scope` stay in the registry, marked retired.

Member calls on a commonly reused method name now lose their edge more often,
though method dispatch itself is unchanged. A member call with more than
three candidate methods gets no edge, counting candidates in the caller's own
file and the files it imports. Same-named methods in one file used to be one
candidate; now each is its own. On Django 5.2.7:
- over-limit member-call sites rose from 2,889 to 3,817;
- dropped member-call sites rose from 7,149 to 8,518.

Many of the lost edges were wrong, such as `super().__init__()` bound to the
class's own `__init__`. Some were right but imprecise.

`graphite impact <file>` is unchanged for every file on graphite and on
Django, both impacted files and likely tests, measured over the whole
population. `impact` and `callers` on a method can lose callers: 8 of 1,803
definitions on graphite and 236 of 8,439 on Django lost at least one likely
test. This is disclosed as the caveat `python-member-call-over-dispatch-cap`.
Class-aware `self.`/`super()` dispatch is #73.

**`graphite init` no longer moves its hooks to the end of
`.claude/settings.json` when they are already current.** It used to strip and
re-append them on every run, so in a repo where another tool's hook came after
graphite's, each `init` re-run reordered the file. That showed up as a change
to a committed file although nothing had changed: Claude Code runs matching
hooks in parallel, so their order has no effect. A current entry now stays
where it is. A stale, duplicated or legacy graphite entry is still replaced,
and a missing one is still appended.

**A name typed in its exact case now picks the definition spelled that way.**
`graphite query "callers Verdict"` could answer about a function named
`verdict`, and grade that answer like any other. The whole query string was
lowercased before the matcher ranked same-named definitions, so the tie
between `Verdict` and `verdict` fell to id order. Now only the verb is folded.
Where two matches rank equally by path depth and scope, the one spelled as
typed comes first. The same holds for a dotted qualname (`Worker.run`), and
for `context` and `impact`. Matching still ignores case: `callers verdict`
still finds a class `Verdict` when nothing is spelled `verdict`.

A query plan (`query --plan-only`) and each `resolution[].input` now carry a
target as it was typed. They used to carry it lowercased.

Path depth still outranks case: a shallower `verdict` is still chosen over a
deeper `Verdict`. The next entry is what such an answer now says about itself.

**An answer about a different spelling than the one typed is no longer graded
`decision_grade` (#77, channel round 317).** When a name matched definitions in more
than one spelling and the node chosen is not spelled as typed, the answer may
be about another symbol. `callers Verdict` could list the callers of a function
`verdict` at `decision_grade`, with the class named only in `alternates`. Now:

- the `resolution[]` entry carries `case_ambiguous: true`, and so does the
  `matched` entry in `context`. It is an optional field of the `query-result`
  schema, an additive change;
- the answer grades `advisory` at most, in `query`, `context` and `impact`;
- the answer carries a new caveat, `target-resolved-to-another-spelling`. It is
  conditional: present only where that pick was made, and listed first;
- `impact` and `context` print their `answer health:` and `known limits:`
  lines for such an answer even when it is not empty;
- an empty listing reads "none found — UNVERIFIED: the name matched more than
  one spelling ...".

A name with one spelling is not affected, however it is typed and however many
definitions share it. Those are listed in `alternates` as before. Ask again by
node id for an answer about exactly one node.

`query --natural` now keeps the case of the question. It used to lowercase it,
so "Who calls Verdict?" reached the matcher as `verdict`. The grammar itself
still ignores case. A suggested `impact` or `context` command now names the
path as typed. Search-fallback terms are still lowercased.

Ranking exact case before path depth was measured and not taken. Over every
name with more than one spelling:

| | graphite | Django 5.2.7 |
|---|---|---|
| names with more than one spelling | 5 of 5,264 | 163 of 28,868 |
| queries, each spelling typed exactly | 10 | 330 |
| answered about another spelling | 2 | 128 |
| the same, with exact case ranked first | 0 | 0 |
| the same names typed in lowercase | 5 | 163 |
| of those, resolving to a different node with exact case ranked first | 1 | 78 |

Lowercase is how a name is usually typed, and those queries rely on a bare name
meaning the shallowest definition. On Django, 20 of the 78 would move from one
test definition to another. The grade change moves no query to another node. It
applies to the 2 and the 128 exact-case queries above, and to 1 of the 5 and 88
of the 163 lowercase ones.

Not changed: a `reaches` or `path` result of "no path" is an error with no
`answer` block, so it has no grade to lower. Its message names the node ids it
used. That such a result is never graded, on a degraded graph included, is #76.
Two published sentences said otherwise and are corrected: the integration guide
called a `no_path` with `truncated: false` proof of absence, and the
`query-result` schema described the `answer` block as present on `no_path`.

**The strict graph-first hook no longer refuses searches for directory names,
filenames and plain words (#72).** It refused a cross-file search whenever one
fragment of the pattern matched a node in the graph. `graph-out`,
`graph\.json` and the word `round` under `docs/` were all turned away, against
the hook's own "literal text and filename searches are fine". Four things
changed:

- **A placeholder is not a symbol.** `json` is in the graph because a file
  imports it, not because the repo defines it. About a quarter of the nodes in
  graphite's own graph are placeholders.
- **A directory with no code is searched as text.** When no source file the
  graph models (Python, TypeScript, JavaScript, Go, Rust) lies under any
  directory the search names, the search is allowed. Every named path must
  exist: a glob or a shell variable reaches the hook unexpanded, so it gets no
  exemption. A directory the repository lies under, such as `..`, holds all of
  its code.
- **The symbol check ignores case only when the search does.** `DONE` in a log
  line is not the function `done`. The Grep tool without `-i` is compared
  case-exactly. So are `grep`, `git grep` and `rg`, unless they carry `-i`,
  `-y`, `--ignore-case`, `-S` or `--smart-case`. An inline `(?i)` in the
  pattern folds on every route. Every other tool (`Select-String`, `ag`,
  `ack`, `findstr`) is still treated as ignoring case. So is `grep` while
  `GREP_OPTIONS` is set, and `rg` while `RIPGREP_CONFIG_PATH` is set: either
  can turn folding on without showing in the command. The Grep tool is built
  on ripgrep and is treated the same way while `RIPGREP_CONFIG_PATH` is set.
  Whether it reads that file was not measured, so the hook does not assume it
  matches case exactly.
- **A piece of a longer literal is not judged alone.** A name joined to its
  neighbour by a hyphen (`graph-out`, `--no-verify`) or a slash (`src/pkg`), or
  followed by a file extension (`graph\.json`, `query.py`), is skipped.
  `name-?`, `name->` and member access such as `self\.run` are still judged.

Still refused: the name of a real definition, searched across a directory that
holds code. `dependents` over `src/` is the control.

**The strict hook resolves a search's paths where the shell will.**
`cd <scratch> && grep check ci.log` reads one file outside the repository. The
hook resolved `ci.log` against the repository root, found no such file, and
refused the search for a symbol named `check`. A `cd` or `pushd` is now
followed when the directory exists and it is joined to the search by `&&`,
with nothing between them but another `cd` or another search. A search that
then names no path searches that directory. After `;`, `||`, `&` or a pipe the
search can run without the `cd` having worked, so paths resolve against the
repository root as before. The same goes for a directory spelled with syntax
only the shell can expand (`$VAR`, `~`, `-` or a glob), for any other command
in between (`cd docs && ls && grep ...`: the hook cannot tell `ls` from a
function that changes directory), and while `CDPATH` is set.

Four kinds of search are now refused that used to pass:

- `cd docs && grep -rn <symbol> ..`, where `..` is the repository root. The
  path was resolved against the root itself and read as leading outside.
- On Windows, a Git Bash drive path into the repository, such as
  `grep -rn <symbol> /f/Projects/repo/src`. Read by Python it was a path on
  the current drive. The Bash tool's `/c/...` paths are now read as the drive.
- A search rooted at a directory the repository lies under, such as
  `grep -rn <symbol> ..`. It was read as a search outside the repository,
  although it covers all of it.
- A search behind another search. Only the first search of a command line was
  judged, so `grep x notes.md ; grep -rn <symbol> src/` passed. Every search in
  the command is judged now, and one refusal refuses the command.

## [1.1.1] — 2026-10-03

A patch release. Security fixes ship in the next patch release of the current
minor (`docs/compatibility.md`, `SECURITY.md`). The graph itself does not
change: on this repository, measured against the deployed 1.1.0 engine,
7797 of 7797 node ids survive, 22154 of 22154 edge triples are
unchanged, every relation count is equal, and `analysis.cycles` is identical.
Only the engine fingerprint moves, because graphite's own bytes did.

### Security

**graphite no longer launches a program by bare name, so a program sitting in
a repository root cannot run in place of the real one.** On Windows a bare
name is looked up in the launching process's current directory before PATH.
That covers a list-form `subprocess.run(["git", ...])`, cmd.exe and
`shutil.which`, unless `NoDefaultCurrentDirectoryInExePath` is set. Claude
Code sets that variable, but the daemon at login and a commit from a terminal
do not.

graphite's builds, hooks and channel broker run with a repository root as
their current directory. A `node.exe` committed to a TypeScript repo's root
ran on every build, and the build still exited 0. This was measured against
the released 1.1.0 wheel; channel round 304 reported it.

Every launch now goes through `graphite.programs.resolve_program`, which:

- reads only absolute PATH entries;
- skips a PATH directory inside the target repository, or one that IS the
  current directory, judged both as written and as resolved, so a committed
  symlink cannot choose which outside binary runs (aramid llm-review
  `1d8d8cf`);
- refuses any candidate that resolves into one of those;
- on Windows, tries `.exe` as CreateProcess does, unless the caller asks for
  more.

Windows' own tools come from the system directory the OS reports. The
sites changed are:

- the TypeScript bridge's `node` (every build);
- the channel broker's `git`;
- the channel's commit-msg hook, which fails closed when no git is found;
- the `git` calls in `init` and `hookinstall`;
- `daemon-health`'s `powershell.exe`;
- the four `schtasks.exe` calls;
- the Startup launcher's `powershell.exe`;
- routing's `claude`/`codex` lookup, which keeps `.cmd` shims, excludes the
  repository and the task worktree whatever the current directory (aramid
  llm-review `30e2473`), and sets the CLI's environment (see below);
- `doctor`'s `graphite-mcp` check;
- the mutation-test launcher's `git`.

Resolving the first program is not enough when that program is a `.cmd` shim.
An npm-installed CLI with no `node.exe` beside it runs bare `node` through
cmd.exe, which searches the task worktree first. A `node.exe` committed to the
repo would then run before the CLI's own sandbox. Routing now sets
`NoDefaultCurrentDirectoryInExePath=1` in every CLI's environment, which turns
that search off for the whole chain. The commit-msg hook also has its own status
when it cannot find git. It used to fall through to "this commit names no agent".

Two older private copies of the same PATH walk, in `git` and `doctor`, now
delegate to the shared resolver. A test walks every launch call in `src/`,
`scripts/` and the hook's embedded program, and fails on a program named by a
literal or on `shell=True`.

The current directory is refused itself, not its subtree, because that is
what Windows searches. Refusing everything beneath it would refuse every tool
on drive C for a run started from `C:\`, and `~/.local/bin` or an
nvm-installed `node` for a run started from `$HOME`. A pre-release build
measured exactly that. The repository's own subtree is refused through the
repository each caller passes.

Artifacts already installed keep the old command until they are regenerated:
- the Startup `.vbs` (`graphite daemon-install-startup-windows`);
- the channel's commit-msg hook (rewritten by `graphite channel register`).

### Fixed

**`doctor --deep`'s LLM probe no longer spends the provider's timeout on
starting its worker.** One number was both the provider timeout and the worker
process's whole budget. The interpreter's start was therefore paid from the
provider's time, and a loaded machine reported `timeout` for a provider that
was only misconfigured: 2 s read `timeout` 7 times in 10 under 36 CPU burners.

The worker already enforces the provider timeout itself. Its process budget is
now that timeout plus `_LLM_WORKER_START_ALLOWANCE_SECONDS` (30 s), which only
bounds a worker that never answers. A 0.1 s provider timeout, below any
interpreter's start, now reaches `configuration`.

**The channel reports an abandoned lock on every path that can break one.**
These come from aramid llm-review findings:
- **`1810f9e`:** `inbox()`, the path every agent takes at session start, was
  the one writer that dropped the recovery. `graphite_channel_inbox` now
  carries `lock_recovered` when a delivery had to break a lock, and stays
  silent otherwise.
- **`9751aab`:** a residue scan that failed read as `[]`, the same as a clean
  one. It now reads `null`, meaning "we could not look". A wedged git, the
  likeliest cause of an abandoned lock, is also what makes the scan fail.
- **`9a23b84`:** the recovery report no longer returns the dead holder's host
  name. Nothing decides on it: identity is pid + start time, and staleness is
  the clock.

**The channel's commit-msg hook is installed with exactly `0o755`, pinned on
every platform.** aramid's mutation drain found `0o755 -> 0o756`, a
world-writable audit hook on POSIX, surviving (`e37afd86`). On Windows,
`os.chmod` honours only the write bit, so no on-disk check there could see it.
A test now pins the argument, which kills the mutant on Windows too. A
POSIX-only test checks the on-disk property: executable, and not group- or
world-writable.

## [1.1.0] — 2026-09-27

A minor release. The agent channel's read surfaces gain fields, which is an
addition under `docs/compatibility.md`. The channel's audit trail also
closes three holes: status events taken on trust, deletions nobody saw, and
an audit gate that could fail open. The graph itself does not change. On
this repository, measured against the deployed 1.0.1 engine, 7659 of
7659 node ids survive, 21788 of 21788 edge triples are unchanged, every
relation count is equal, and `analysis.cycles` is identical. Only the
engine fingerprint moves, because graphite's own bytes did.

### Added

**Every channel status now says who recorded it, and where each recipient
stands.** `graphite_channel_list`, `graphite_channel_read` and `graphite
channel list --json` carry `status_actor`, `status_at` and
`status_verification` beside `status`, plus `recipients` (each recipient's
own latest status and its verification, or `null`) and `unreceipted`
(recipients with no event of any kind). `status` keeps its
meaning: the newest event by any agent. `graphite_channel_read` also returns
`history`, where every event carries its actor, time, commit and
verification grade. `graphite channel show N` prints the same history after
the body. Measured on the live channel on 2026-09-25: the one folded word
misstated at least one recipient in 39 of the 212 rounds that carry events.
Round 256 read `acknowledged` while one of its two recipients had recorded
nothing. The read tool's description now says that reading records nothing,
and that `graphite_channel_inbox` registers receipt.

### Fixed

**The channel report now verifies status events against their commits, as it
already did for rounds.** A status event's `actor` was taken on trust. An
event written by hand under another agent's trailer, rewritten after its
commit, or edited and left uncommitted was invisible, because the forged
actor was a participant and the file was tracked. Each event is now graded
from one `git log` over `status/`. It is `verified` only when it has exactly
one commit, a clean working copy, and a trailer naming its actor.
`status_modified`, `status_discrepancy` and `status_uncommitted` are
anomalies and fail `ok`. On the live channel all 408 events verified, with
a planted-forgery control flagged. A comment had claimed this check existed.
The read surfaces carry the same grade, so an actor is never shown bare:
`status_verification` sits beside `status_actor`, each recipient's entry has
its own `verification`, and the human report marks a failing attribution
`UNVERIFIED`. This closes aramid llm-review finding `53368ee`.
One trust limit remains, and it is stated here because it bounds what
"verified" means. Every agent commits under the operator's git identity, so
a trailer proves which agent label was attached, not which process
committed. A hand-made commit under the claimed actor's own trailer still
grades `verified`. Closing that needs signed commits.

**The channel report now sees deletions, and a status event's number is
never reused.** The report graded only the events and rounds still on disk,
and a new event took `len(files) + 1`. An agent could therefore delete its
own `blocked` event, commit the deletion, write a `done` into the same slot,
and the report still read OK. A round file removed the same way vanished
from the audit view entirely. The next number is now one past the highest
the round has ever used, on disk or in history, and a history git cannot
read refuses the write instead of passing for an empty one. Any status file
or round that history or the index knows but the working tree lacks is
reported as `status_deleted` or `round_deleted`, and a hole in a round's
1..N sequence is reported as `status_seq_gap`. All three fail `ok`. The live channel's history
holds no deletions or renames, so none fire there. This closes aramid
llm-review finding `5db5889`.

**Installing the channel's audit gate now fails closed.**
`ensure_channel_hook` set `core.hooksPath` with `check=False` and always
reported `changed: True`. A gate git never armed (a locked `.git/config`, or a
higher-precedence override) was therefore reported as in place, and later
commits would have passed with no agent trailer. The write now raises on
failure. The value git will actually use is read back, and anything other
than `.githooks` raises `hook_not_armed`. `changed` reports the observed
before-and-after state. This closes aramid llm-review finding `0e87805`.

**Stalling is judged per recipient.** A co-recipient's `done` no longer
hides a recipient that was handed the round and never followed up. A
withdrawn or superseded round never stalls. Rows gain `stalled_recipients`.
The report lists recipients with no receipt per agent under `unreceipted`.
They are visible but do not fail `ok`, because several registered agents
have never called inbox, and a check that is always red is one nobody
reads.

**Reading the channel no longer rewrites its git index.** The report's
`git status` refreshed stale stat data by rewriting `.git/index` under
`index.lock`, the lock a concurrent broker `git add` needs. Channel git now
runs with `GIT_OPTIONAL_LOCKS=0`. A test pins the index bytes across a
report and a read with stale stat data.

**A PEP 508 marker of the shape `(A) or (B)` was mis-parsed by the doctor's
MCP distribution walk.** The marker evaluator unwrapped outer parentheses by
checking only that the text started with `(` and ended with `)`, so
`(extra == "a") or (python_version == "3.14")` was stripped to
`extra == "a") or (python_version == "3.14"`. An `extra` marker of that shape
then failed closed to "does not apply"; a plain marker of that shape raised
and aborted the whole distribution walk, which is `probe_mcp`'s manifest
source. Outer parentheses are now unwrapped only when the first `(` is closed
by the last character, counting depth outside quoted values. Found by the
new `tests/test_doctor_probes.py`; no installed distribution on the reference
machine carried the shape, which is why the walk had never failed.

### Changed

**`scripts/mutation_tests.py` targets `scripts/` changes too.** The
certifying launcher for aramid's mutation gate diffed `src/` only, so a
mutant in the launcher itself skipped the targeted stage and its confirm ran
the whole suite (465 s on the gate's first live drain, 2026-09-04, where the
targeted stage kills in seconds). A changed `scripts/<stem>.py` now targets
`tests/test_<stem>.py` and `tests/test_<stem>_*.py` like a changed source
module.

**Twelve modules gained a `tests/test_<stem>.py`.** aramid's mutation stage 1
runs `tests/test_<module>.py` when it exists and otherwise `pytest -k
<module>`; a run that selects nothing is booked a stage-1 survivor and every
such mutant goes to a full-suite confirm, of which an item gets three. Nine
modules had a `-k` fallback that collected zero tests (`doctor_probes`,
`dependency_install`, `probe_process`, `ts_bridge`, `_win32_ctypes`,
`process_contracts`, `routing/ollama_executor`, `routing/context_builder`,
`routing/route_pool_execution`) and three collected one (`mcp_server`,
`llm_probe`, `cluster`). Each now has a stage-1 file that pins its pure
helpers directly; every file was proven able to fail by a hand mutant of its
module. Three quirks were pinned as-is rather than changed: a cluster's
kind label is `f"{kind}s"` verbatim (`classs`), a blank path in a TypeScript
bridge edge normalises to `.` instead of being dropped, and an empty MCP
`node_id` fuzzy-matches the first node.

## [1.0.1] — 2026-09-05

A patch release. Measured against the deployed 1.0.0 engine on this repository,
the tree produces the same graph: 7260 of 7260 node ids survive, 20700 of 20700
edge triples unchanged, every relation count equal, `analysis.cycles` identical.
Only the engine fingerprint moves, because graphite's own bytes did.

### Changed

**Vendor-neutral wording throughout the replacement audit and docs.** The
README opening, the `audit-replacement` help and reference text,
`skill/SKILL.md` and the audit's own messages no longer name the legacy
graph tool, and the README's opening comparative against it — an unmeasured
claim dating to the initial commit (`fd58583`) — is removed rather than
defended. In the audit's report, which is not a `schema_version`-stable
surface, the `graphify` block is renamed `legacy` and the warning codes
`graphify_paths_exist`, `graphify_text_references` and
`graphify_gitignore_entries` become `legacy_paths_exist`,
`legacy_text_references` and `legacy_gitignore_entries`. Detection is
unchanged: the on-disk path patterns and the text-scan needle still match
the legacy tool's literal artifact names — the one place the name must
remain for the audit to find anything.

### Fixed

**The extraction cache grew without bound inside a partition (#66).** #23
reclaims whole partitions a build can never read again and left the one it
uses alone. Entries are keyed on the file's content hash, so every edit that
triggers a rebuild wrote a new entry and orphaned the previous one, and
nothing ever removed an orphan: a consumer checkout rebuilt by the daemon on
every change reached 15,406 entries / 419 MB in a single partition, 11,592 of
them from four days, against 1,780 / 62 MB for graphite's own tree on the
same engine. Same rule as #23, one level down -- reachability, not age or
size: a `Cache` now records every key it read (and found) or wrote, and after
a build has extracted the tree and built its graph, `prune_unreachable_entries`
deletes every other entry in the partition, reporting
`[graphite] reclaimed N unreachable cache entries (X MB)` when it removed any.
Older content checked out later re-extracts once (performance, never
correctness). Safe without the build lock: `read` already treats a vanished
file as a miss, so a concurrent reader re-extracts rather than fails. A build
that touched nothing prunes nothing, only the exact `<2 hex>/<64 hex>.json`
shape the cache writes is eligible, and a per-entry failure (a Windows handle
race) is swallowed.

**The deferred stdin close never fired on Linux or macOS.** `run_bounded_process`
reads the child's pipes with a fixed-size read, and its two transports hand
that reader different objects: on Windows a raw `io.FileIO`, whose `read(n)`
returns whatever the child has written, and on POSIX a default `Popen` pipe,
a `BufferedReader` whose `read(n)` keeps reading until it has n bytes or EOF.
Off Windows the reader therefore saw nothing until the child exited, the
`stdin_close_when` predicate never saw its marker, and every deferred close
was the budget fallback -- so the #29 fix was inert there and the doctor's MCP
deep probe spent its whole budget on every POSIX run. The reader now uses
`read1` on buffered pipes; the Windows path is unchanged. The tests that
pinned the deferral could not fail (one matched a marker that text-mode
stdout writes as CRLF on Windows, the other's lower bound was satisfied by
the child's own sleep) and now kill a never-defer mutant; the interval test
that blocked the pre-push gate measures from the close instead of from
launch, against the child's own life rather than a 0.25s constant.

**The post-publication verifier's provenance arm asked an endpoint that
never carries provenance.** `scripts/verify_published_release.py` read
`urls[].provenance` from PyPI's legacy `/pypi/<project>/<version>/json`
API, which has no such key, so the arm reported "no provenance" for 1.0.0
while the Integrity API was already serving both GitHub attestation bundles
(`jared0565/graphite`, `publish.yml`). The arm now reads the PEP 691 Simple
JSON index (`Accept: application/vnd.pypi.simple.v1+json`), where PEP 740
publishes a `provenance` URL per file; a test pins the endpoint so a
regression to the legacy API fails. Re-run against 1.0.0: 6 of 6 arms.

**Three findings from aramid's `llm-review` tier on the CI workflow and the
benchmark, all confirmed and closed:**

- The `coverage_floor` dispatch input could *lower* the floor for one run,
  so a dispatched run could report green against a weaker gate than the
  committed one. The enforcing step now refuses any requested value that is
  not an integer at or above the committed `COVERAGE_FLOOR`; a dispatch can
  only raise it (the negative control is unchanged). Proven with a fake
  coverage across six arms: empty, 84 and 83 pass; 80, `abc` and -5 fail
  before coverage is read.
- `test_core_probe_cleanup_timeout_uses_single_global_slot_and_recovers`
  raced its 0.5 s probe budget against runner load (#65): more than 0.4 s of
  scheduling before the workspace lease is acquired yields a pure `timeout`,
  after it a `cleanup_timeout` carrying `masked_*` keys — two CI failures in
  three runs on one day, both shapes reproduced with a paced fake clock. The
  test now freezes its injected `_clock` like the sibling probe tests (the
  subject is the classification of a hung cleanup, not phase pacing; the
  cleanup join still runs on real time), and the frozen-clock test still
  kills both guarding mutants: a hung cleanup reported as `None`, and a
  probe slot that never releases.
- The `security` job's self-check asserted only that gitleaks and semgrep
  ran, while the step's name claimed typecheck and a dependency audit. The
  labels were measured from the job's own `prepush.json` artifact
  (`gitleaks, mypy, python.exe, semgrep, shadow`): the assertion now covers
  every configured scanner, and the step name, `SECURITY.md` and
  `CONTRIBUTING.md` no longer claim a dependency audit — no gate run on
  either machine has ever emitted one, although `aramid doctor` lists
  pip-audit as present (reported to aramid). Measuring this exposed two
  worse things: under aramid 0.6.0 the CI typecheck slot had **never run**
  in `--all` mode (no `mypy` in `tools_ran`, nothing degraded) and the old
  assertion called that clean; under 0.6.1 it runs over every `.py`, and
  the job passed with exit 0 while its own report carried 786 block-tier
  findings that the same report fails with on the maintainer's machine
  (exit 1). Discriminated on the first run under the 0.7.0 pin (33286250634):
  still 786 block-tier findings and exit 0 with CI's empty ledger, so
  aramid's exit depends on ledger presence — `.aramid/` is gitignored, so
  a fresh checkout cannot be gated by exit status at all. aramid confirmed
  the mechanism from its code (channel round 151): its **fresh-ledger
  rule** — the first pre-push run on a ledger with no baseline writes one
  and downgrades findings the ratchet alone escalated to exit 0, so every
  CI checkout is "the first run" — and from 0.7.1 the report says so
  (`fresh_ledger_baseline`, `grandfathered`; both gate steps print them).
  Both CI gate steps now
  read the verdict from the report itself: any block-tier finding from a
  non-mypy scanner, or from mypy inside the project's type-gate scope
  (`src/graphite`), fails the step regardless of the exit status; mypy
  findings on the deliberately untyped `tests/`, `scripts/` and
  `benchmarks/` trees are counted and printed.
- `benchmarks/build_benchmark.py` prepended the repository root to
  `sys.path` to import its sibling — the shape the `-P` launch contract
  forbids. It loads `synthetic_repo.py` by location now, CI runs it under
  `-P`, and two tests pin the shape (no `sys.path` access in the module; the
  script starts under `-P`).

### Changed

- CI pins `aramid==0.7.2`, the tool this machine runs (channel round 157;
  0.7.0 and 0.7.1 were pinned briefly at rounds 148 and 152). 0.7.2 fixes
  the tests-slot skip-streak false alarm this repository reported — the
  registry key `tests` rode into `run_started.expected` beside the slot's
  label, so `status` counted every pre-push run as a skipped suite (channel
  rounds 155–157) — and stamps `run_id`/`recorded` in the check report, so
  a `--no-record` measurement is distinguishable from a recorded one.
  0.7.1's typecheck slot honours
  `[tool.mypy] files`, so the 683 mypy rows that a whole-tree measurement
  under 0.7.0 had written into the maintainer's ledger for `tests/` (675)
  and `scripts/` (8) are retired with `ledger resolve --out-of-scope`, each
  carrying the reason.
  The two `mypy:syntax` ledger rows that 0.6.1 left unresolvable were NOT
  retired with `ledger resolve --out-of-scope` as first written here: it
  refused them (`is not open (status=fixed)`), because aramid's gate had
  already marked them `fixed` on a push that ran mypy over other files
  without opening `ci.yml` — a defect in aramid 0.6.1's examined-set
  stamping, fixed on aramid's `main` (`e65f296`) for 0.7.1. The rows stay
  `fixed`, the log being append-only; channel round 151 is their record.
- mypy's configuration is back under `[tool.mypy]` in `pyproject.toml` and
  `setup.cfg` is gone. It had moved there only because aramid 0.6.0's
  typecheck runner armed on `[tool.mypy]` and then fed every changed file to
  mypy; aramid 0.6.1 hands mypy only `.py`/`.pyi` (channel round 144), so
  the CI pin is now `aramid==0.6.1` and the two `mypy:syntax` suppression
  entries that bridged the defect are deleted rather than kept — a
  workaround that outlives its root cause misdirects the next reader.

### Documentation

- **The generated CLI reference now renders nested subcommands.**
  `scripts/gen_cli_reference.py` walked only the top level of the parser,
  so the twenty-four commands that live one level down — under `route`,
  `lifecycle`, `channel`, `incidents` and `overlay` — and every option they
  own (`route reconcile --attempt-id`, for one) were absent from
  `docs/reference/cli.md`, and the lockstep test could not see them drift.
  Each nested command now has its own section under its group, and
  `tests/test_cli_reference.py` requires every nested command and every
  option it declares to appear there.
- **Every CLI argument now declares help text.** Rendering the nested
  commands exposed sixty-two arguments with none — every nested `--json`,
  the `lifecycle policy prepare` and `verification prepare` inputs,
  `route accept|reject|cleanup|review --task-id`, `record-outcome`'s
  flags, `reconcile --attempt-id`, `policy --promote/--rollback`, the
  `incidents ack|resolve` positionals and options — so both `--help` and
  the reference were silent about them. Each now says what it takes and
  what it binds to; a test walks the whole parser and fails on any
  argument without help, so none can be added back.
- New [user guide](docs/user-guide.md) and [knowledge base](docs/knowledge-base.md).
- README: installation is `pip install graphite-code` (it had still said
  `git clone` + `pip install -e .`, wrong since the 0.3.0 wheel), the `-P`
  launch rule is explained where the command is introduced, the MCP
  examples launch with `-P`, and a documentation index points at every
  support page. CONTRIBUTING puts the dev environment outside the clone
  and lists what CI enforces. ARCHITECTURE covers the platform
  supervisors and the bounded cycle search. The Claude Code skill is
  machine-neutral and versioned 1.0.0.

## [1.0.0] — 2026-08-29

The production-grade release. Nothing here is a claim CI cannot turn red:
each item names the gate or test that enforces it. The declaration criteria
and their evidence are in `docs/superpowers/specs/2026-08-29-production-grade-design.md`.

### Fixed

**The cycle report could not finish on a repository with dense import
cycles** (#64). `analyze()` enumerated every simple cycle of the project
graph before sorting and keeping twenty; on Django 5.2 (2 930 source files,
`tests/` and `django/` importing each other) that is exponential — 13 GB and
unfinished after thirty minutes, while each half alone reported cycles in
seconds. The search is now bounded: cycles are enumerated inside strongly
connected components one length level at a time up to length 8, under a
10 000-cycle budget, and stop once a completed level holds the twenty
shortest — exact whenever it stops that way, and deterministic across
processes even when the budget bites. `analysis.cycle_search` records
`length_bound`, `budget`, `examined`, `budget_exhausted`,
`complete_through_length`, `cyclic_components` and `largest_component`, so a
truncated answer says so. Django 5.2 now builds in 82 s (45 620 nodes,
109 373 edges, 53 MB). Found by the real-repository benchmark; the synthetic
corpus alone would never have shown it.

**A lifecycle event claiming "no prior observation" over an UNAVAILABLE row
raised `AttributeError`** instead of `lifecycle_transition_stale`
(`routing/lifecycle_storage.py`); found while making the type gate clean.

### Added

- **Every supported cell gates merges.** The CI matrix is windows/ubuntu/macos
  × CPython 3.11–3.14, all twelve cells blocking; the advisory `portability`
  job is gone. Ubuntu and macOS 3.13 ran for the first time and pass.
- **A type gate.** `python -m mypy` over `src/graphite` runs in the lint
  job; 142 findings were fixed with zero `type: ignore` comments added, and
  the gate is clean under `--platform` win32, linux and darwin.
  `graphite/py.typed` ships (the artifact verifier refuses a wheel without
  it) and the distribution carries `Typing :: Typed`.
- **A coverage floor.** Branch coverage is collected on the 3.12 leg of each
  OS, combined (83.04 % on the first measurement), and enforced at
  `COVERAGE_FLOOR = 83`; a `workflow_dispatch` input overrides the floor for
  one run as the negative control (floor 84 turns the job red).
- **The security gates run in CI, unmodified.** A `security` job creates the
  dev venv `aramid.toml` names and runs `aramid check --gate pre-commit|pre-push
  --all --strict` against the committed configuration, asserting the tools
  that ran (gitleaks, ruff, shadow; gitleaks, mypy, the suite, semgrep,
  shadow) and refusing any degraded tool. A planted repo-root `graphite.py`
  fails it at the pre-commit gate — and, under `python -m pytest`, every test
  leg with it, which is the hazard the `-P` launch shape exists for.
- `SECURITY.md` (GitHub private vulnerability reporting), `CODEOWNERS`, issue
  templates, dependabot for actions and pip, `authors` metadata.
- `daemon-install-linux` (systemd user unit), `daemon-install-macos`
  (launchd agent), `daemon-uninstall-*` and `daemon-service-status`; all
  three supervisors build their launch argv from one place, so `-P` is
  present by construction. `daemon-health` reports the platform's supervisor.
- `benchmarks/`: a deterministic synthetic corpus and a build benchmark; a CI
  `benchmark` job records a 3 000-file build as an artifact and fails only
  past a catastrophic budget. `capabilities` declares
  `supported_repo_files: 7000` with its basis; `docs/benchmarks.md` holds the
  measurements, including Django 5.2.
- Reference documentation that cannot drift: `docs/reference/cli.md`
  (generated from the parser; a test fails when stale),
  `docs/reference/configuration.md` (every `Config` field and every
  `GRAPHITE_*` variable the code reads, both directions tested),
  `docs/reference/exit-codes.md` (every subcommand has a row),
  `docs/compatibility.md` (what 1.x promises, the deprecation policy, the
  support matrix).
- Releases are built in CI from the approved tag with pinned tools
  (`release-build-constraints.txt`), refused unless both digests equal the
  reviewed ones, attached to the GitHub Release, and published with PEP 740
  attestations that are true because this workflow built the bytes.

### Changed

- `Development Status :: 5 - Production/Stable`. From 1.0.0 the surfaces in
  `docs/compatibility.md` are stable; a breaking change to any of them is a
  major release.
- The CI security job pins aramid 0.6.0 (the version the maintainer's
  machine runs). mypy's configuration lives in `setup.cfg` for now: aramid
  0.6.0's typecheck runner arms on `[tool.mypy]` and hands non-Python files
  to mypy (accepted in channel round 140, fixed in aramid 0.6.1); both
  files say when to move it back. Two reasoned entries in
  `.aramid-suppressions.toml` from the same defect retire themselves.

## [0.5.3] — 2026-08-28

### Fixed

**The `git --version` probe no longer has a fixed two-second budget of its
own** (#37). Every production caller gave its real Git command 5–300 seconds
and then paid a fixed 2.0 seconds for the version probe that gates it -- the
one number in that path tuned to a quiet machine, in the step that runs first
on a loaded CI runner. A probe that outlived it was folded into
`GitUnsupportedVersionError` and widened upstream to `git_unavailable`, which
is how #37's single Windows sighting logged an installed, working Git as
unavailable. The probe now runs under the budget the caller gave its command:
`--version` is Git's cheapest command, so a budget the real command can
survive is one the probe can survive -- a derivation, not a wider number.
Probe and command keep separate budgets on purpose; a shared deadline would
make the command's budget vary with probe latency, a new way to time out
spuriously. `_GIT_VERSION_TIMEOUT_SECONDS` is gone. Recorded rather than
fixed: `_version` is still cached per `GitRunner`, so one routing operation
still runs the probe several times (exposure, not cause), and the 0.1 s
reader-thread join in `_run_bounded` is a second fixed bound feeding the same
bucket -- measured 0 of 400 runs live on a loaded 12-core box, so left alone
on evidence.

## [0.5.2] — 2026-08-28

### Fixed

**Force-stopping the daemon mid-build no longer wedges its successor for the
lock TTL** (#60). The build lock was held by the daemon on its child's behalf
(`ENV_LOCK_HELD` told the child not to contend), so `Stop-Process -Force` --
the documented post-install restart, which runs no `finally` -- left
`.build.lock` on disk naming a dead pid while the orphaned child kept writing
`graph-out/`. The successor could neither trust "holder dead" (the writer was
still alive) nor see "writer done", so it logged `build_skipped_locked` every
cycle and `daemon-health` warned for up to 600 s over a graph that was already
correct. A pid-liveness probe would not have helped and would have raced the
orphan in the very incident that filed the issue.

The lock now follows the writer: the daemon's child takes it itself, exactly
as a hook or manual `graphite build` does, and a killed daemon leaves no lock
at all. `build_skipped_locked` is therefore true for as long as a build is
really running and not a second longer. Two consequences are handled. The
child answers a refusal with exit status 75 (`EX_TEMPFAIL`) when the daemon
asks for it via `GRAPHITE_BUILD_LOCK_REPORT_REFUSAL`, so the cycle can book
"another builder owns the repo" as a skip and not a failed build; a human or a
hook still gets exit 0. And because the daemon's timeout kill now kills the
holder, the daemon releases the lock its dead child left -- by pid match only,
never by guessing. Under a Windows venv launcher `Popen.pid` is the launcher,
not the writer, so there the timeout path falls back to the TTL instead of
retrying next cycle: conservative by design, and noted here so nobody reads it
as a hang. `GRAPHITE_BUILD_LOCK_HELD` is gone; nothing outside the daemon set it.

## [0.5.1] — 2026-08-27

### Fixed

**A reader holding the graph open made the writer fail on Windows** (#59).
`MoveFileEx` cannot replace a file another handle holds open without
`FILE_SHARE_DELETE`, and neither `open()` nor `os.open()` grants that share
mode — so graphite's own readers (`graph_io`, the MCP server, a hook running
`query`) made `atomic_write_text`'s single `os.replace` raise `WinError 5` for
the length of a read. Three occurrences in 1,585 daemon builds; the daemon
masked each by rebuilding next cycle, while a hook- or agent-driven `build`
exited 1 and left the previous graph in place. The rename is now retried with
bounded backoff (2 s) on Windows only — on POSIX an EACCES from rename is the
directory's permissions, which no wait changes. Reproduced by a test that holds
the target open in a thread: red on the old writer, green now.

The writer's cleanup path could also **mask the error that mattered**: a temp
file held by an indexer made `unlink` raise inside `finally: raise`, so the one
`WinError 32` in the log named only the temp file and nothing else. Cleanup
failures are now suppressed and the original error propagates.

The same retry now backs every temp-file-then-rename writer in graphite, not
only the graph's: `activation._atomic_write` (the marker every hook refreshes
and the daemon reads each discovery cycle — a lost refresh was silent, because
`mark_active` is fail-open) and `overlays._atomic_write_secure`, through one
public `io.replace_file`. Neither had been observed failing; both had the
same single `os.replace`.

## [0.5.0] — 2026-08-26

### Fixed

**Distinct definitions collapsed into one node id** (#57, #58). Two independent
mechanisms, one silent symptom: the losing definition vanished from the graph
entirely, every edge naming either landed on whichever survived, and nothing in
the answer signalled it — a contaminated `callers` list grades exactly like a
clean one, and an empty one for the vanished name reads as a trustworthy absence.

`_make_id` had **five** lossy operations — `strip("_.")` per part, `[^\w]+ -> _`,
`_+ -> _`, `casefold()` and truncation — so within one file `Path`, `path` and
`_path` were one id. Measured on this repo: **5 collisions, 6 definitions absent**.
`routing/storage.py` defines `initialize` (L950) and `_initialize` (L3965); the
graph kept `initialize`, and `self._initialize()` was recorded as a call to it.

Removing a normalisation does not fix this, which is worth knowing before trying:
drop the strip and `path`/`_path` are *still* merged, because the `_+` collapse
puts them back; drop both and `path`/`Path` remain merged by the casefold. An
ambiguous input now gets a short **hex** discriminator — the one alphabet that
survives both. A canonical input keeps its plain, readable id.

`_file_node_id` built its id from `path.stem`, **discarding the extension**, so
`index.ts` and `index.js`, or a component and its stylesheet, were one node — the
shape that most affects the TypeScript consumers. It also inherited the
sanitisation collisions: `src/graphite/init.py` (791 lines) and
`src/graphite/__init__.py` (41) were one node, so the larger module had no file
node at all and its 21 symbols hung off the smaller one. Both are fixed; file ids
now carry the full filename.

**Every node id in the graph changes.** Consumers re-extract on the engine
fingerprint anyway, so the rebuild costs nothing extra — but any node id written
down in a document or a script must be re-read from the new graph. Both
docstrings on `_file_node_id` claimed the old scheme kept every file distinct;
both were false and are rewritten rather than appended to.

Known residual, measured at zero occurrences here: a path separator and a literal
underscore are still indistinguishable, so `a/b.py` and `a_b.py` would collide.
Closing it would mean discriminating every path containing an underscore.


**Method dispatch overruled the call classifier, in every language** (#56).
`_call_confidence` tags an edge `EXTERNAL_CALL` only when the call *provably*
leaves the repo — an attributable receiver root bound by an import that did not
resolve in-repo, or an `_EXTERNAL_GLOBALS` name no in-repo binding shadows.
`_resolve_method_dispatch` then re-pointed that same edge to an in-repo definition
by bare method name alone, producing an edge whose `confidence` said the call left
the repo and whose `target` was a function inside it.

The #54 reachability gate cannot catch these: the import that makes the definition
"reachable" is real and irrelevant, because the receiver is `os`. Externality is
evidence that gate never consults.

Measured on this repo, two builds through one interpreter differing only in this
pass: **42 `calls` edges removed, every one `EXTERNAL_CALL`, none `LOCAL_CALL`.**
Every removed edge read at its source line is a standard-library call —
`os.close`/`os.open`/`os.write`/`os.kill` (24), `subprocess.run` (8),
`threading.Event` (4), Node's `path.resolve` (2), `sys.stdin.read` (1).
`os.kill(pid, 0)` in one test was bound to **three** different in-repo `kill`s.

    callers resolve   5 decision_grade → 3 decision_grade
    callers kill      4 decision_grade → 1 decision_grade
    callers event     6 decision_grade → 2 decision_grade
    callers run       3 decision_grade → 0 ADVISORY

The last line is the honest-answer contract working: the answer is now empty *and
says so*, rather than confidently naming three wrong callers.

It is a **refusal to re-point, not a deletion**. The edge stays where it already
pointed, so `health.py` can go on excluding it from the resolution denominator —
it can only exclude what is present. **The ratio therefore does not move**
(`calls.python` 0.977 → 0.977, total 9427 → 9387, external 3044 → 3076): under
schema 3 these edges were already excluded as external, so the fix shifts them
from `total` to `external` in the same step. Grading this on health would have
read as "no effect" — the trap #54's first acceptance criterion fell into,
arriving from the other direction.

**Dispatch could cross a language boundary** (#56). No FFI is modelled, so a
JavaScript call site cannot reach a Python `def` — but the name match ran across
the whole node set with no notion of language, and did:
`path.resolve(input.root)` in `src/graphite/ts_resolver.mjs` bound to a `resolve`
defined in `tests/test_git_security.py`, at `decision_grade`. Dispatch is now
confined to one interop **family** (`javascript`/`typescript`/`jsx`/`tsx` together;
every other language its own), never to the raw `LANGUAGE_BY_EXT` label — `.ts`
and `.tsx` are different labels and one ecosystem. Unlike the #54 gate this is an
exact invariant rather than a proxy, so it needs no per-language census: it removes
only bindings that are impossible. **Zero marginal effect on this repo** — both
crossings here were also `EXTERNAL_CALL` and already gone — which is why its
fixture makes the crossing a `LOCAL_CALL`.

**A Rust call on an unnameable receiver was a false external** (#56).
`_simple_rust_value` returns `None` for anything but an identifier or `self`, so
`build().format()` reached `_call_confidence` as the bare name `format`, which is
in `_EXTERNAL_GLOBALS`. The Rust site passed no `attributable=`, so the edge came
out `EXTERNAL_CALL` — excused from the health denominator without ever being shown
to leave the repo. That is #14 mechanism A, which Python and TS/JS were threaded
for and Rust was not. Go is deliberately unchanged: its `selector_expression`
branch builds the call name from the operand's raw source text, so nothing here
reaches its bare-name fallback, and a guard nothing can trip is not a guard.

### Still open

The #54 reachability gate remains Python-only. That is a separate, weaker
question — bindings that are *possible but unlikely* in duck-typed code — and it
still owes the per-language measurement `_dispatch_is_gated` describes.

## [0.4.0] — 2026-08-14

**A minor bump because consumer graphs change, not because an API did.** Both fixes
below remove or add graph edges, so every consumer that rebuilds will see different
`callers`/`context` answers and a slightly different Python `calls` ratio. Nothing in
the CLI or the JSON schema changed. Under this project's pre-1.0 rule — minor versions
may break things — a patch number would have understated a release that silently
changes what the graph says.

### Fixed

**Python method calls bound to any same-named definition in the repo, however
unreachable** (#54). `_resolve_method_dispatch` re-pointed `recv.method()` to any
`is_method` node sharing the name, justified in its own docstring by "method names
are almost always globally unique" — true of domain names, false of every name a
standard library already owns. On graphite's own graph `query "callers resolve"`
returned **232 callers graded `decision_grade`**, every one an ordinary
`pathlib.Path(...).resolve()` re-pointed to a test double's `resolve` in
`tests/test_git_security.py`; `src/graphite/io.py::atomic_write_text` was recorded
as calling a `write` defined in `tests/test_doctor.py`. The mis-bindings counted as
`bound`, so the health ratio rose as the defect got worse.

A Python call now dispatches only to a definition in the calling file or in a file
it directly imports. Filtering happens **before** the ambiguity cap, because
reachability is the disambiguation that cap was standing in for — so a common name
with one reachable definition now resolves instead of being abandoned as "too
generic to guess".

Measured on this repo: `callers resolve` 232 → 5, `callers get` 87 → 0,
`callers write` 30 → 0; `calls` edges 13498 → 12497 (−1428, +427); `calls.python`
0.980 → 0.977. Every one of the 1428 dropped edges buckets as
`src|scripts|tests → tests` or `src → src`, and every sampled member was another
false binding (`sqlite3.connect`, `Thread.start`, a file handle's `write`).

**Expect consumer graphs to lose call edges and report a slightly lower Python
`calls` ratio.** That is honest accounting, not a regression — the same shape as the
constructor-edge change that moved some javascript cells down. Do not "fix" it back.

The gate resolves language through `LANGUAGE_BY_EXT`, the same table `collect_files`
uses to choose an extractor, so it cannot disagree with the walk that produced the
edge. Verified to reach a **warm** cache too: a graph built by the old code, rebuilt
in place by the new code, loses the false binding — the dispatch pass runs after
`cache.write`, so a stale partition cannot preserve it.

Scoped to **Python only, on evidence**. The gate's premise — "the caller does not
import the definer" standing in for "the caller cannot be holding one" — is never
exact in a duck-typed language. TypeScript/JavaScript stay ungated because
`test_member_call_ambiguous_small_set_links_to_all` pins the documented fan-out on
an `x: any` receiver with no import at all, a shape the gate would delete; Rust
because this repo has none to measure. Go could not be gated at all: its in-repo
imports target a synthesized package id that never equals any file's node id.
Residual: the same defect survives in ungated JS/TS.

**`import pkg.sub` emitted no import edge to `pkg/__init__.py`** (#55), though that
statement binds the name `pkg` and executes the package. Every dotted importer was
therefore invisible as a dependent of that `__init__.py` — the file where re-exports
and shared constants live, so a high-traffic edit target whose blast radius was
understated. Silently: a missing edge cannot lower the imports ratio, which is
computed over the edges that *were* emitted.

Found because the #54 gate refused `pkg.build()` and a test caught it. The gate
initially compensated with its own ancestor expansion; with the real edges emitted
that compensation became unfalsifiable — removing it changed no test — so it was
deleted rather than left looking like protection.
`test_a_dotted_import_reaches_the_package_it_binds` now guards the coupling.

Measured on this repo: import edges 1890 → 1920 (+30, −0), imports health 740/740 →
770/770 with `ratio` 1.0 and `external` **unchanged** at 1148 — purely additive, no
reclassification. `impact src/graphite/routing/__init__.py` went from 1 likely test
to **8**.

Scoped to `import a.b.c`, not `from a.b import c`. Both execute the package, but only
the first binds `a`, so only the first can carry a `a.something()` call to attribute;
the from-import spelling has two existing specifications pinning one edge per module,
and expanding it would have added ~234 further edges here with no measured demand.
Ancestors are edged only when they resolve in-repo, so a PEP 420 namespace package
(no `__init__.py`) and an external `import os.path` both emit nothing extra.

## [0.3.0] — 2026-08-12

**The first release consumers install as a built wheel.** Through 0.2.1 every
consumer on the development machine imported graphite from one shared editable
install pointed at the source tree, so a saved file was live everywhere with no
build, no review and no version boundary. That is why `__version__` could not
distinguish two states of the tree, and why "which fix does this install have"
was answerable only by surveying for markers. A wheel install writes source and
metadata together, so from 0.3.0 the version means something again.

### Fixed

**Daemon discovery only worked on one machine, and the wheel shipped that path.**
`_default_daemon_base` compared each parent directory against a single hardcoded
absolute path — the maintainer's own layout, present since the initial commit.
Two separate defects in one line. Functionally, discovery succeeded on exactly
one machine and silently fell back to the project root everywhere else, so
`daemon-health` and `bootstrap` reported no daemon for every other user.
Distribution-wise, the wheel carried an absolute developer path, which
`RELEASING.md` forbids in a release archive.

The value is only ever used to reach `base/.graphite-daemon/status.json`, so it
now searches upward for that marker instead of matching a name. Behaviour is
unchanged where the old literal applied, and correct everywhere else.
`GRAPHITE_PROJECTS_ROOT` still takes precedence, and the directory name is now a
single named constant shared with the reader of that file.

Worth recording *why this survived two releases*: 0.2.0 and 0.2.1 both ran a
disclosure scan and both recorded zero developer-path hits, because both searched
the **compressed** archive bytes, where a plaintext pattern cannot match. A clean
tree and a leaking one were indistinguishable — the scan could not fail. The
0.2.1 wheel in fact shipped this path in three modules and twelve times in
`METADATA`. `tests/test_hardening.py` now scans the packaged sources directly,
docstrings included, and is mutation-proven against the original line.

**The engine fingerprint could not see the parsers (#52).** `engine_identity`
digested `cache_version`, `schema_version`, `graphite.__version__` and the files
under `src/graphite/` — everything except the tree-sitter grammars that actually
produce the ASTs. Upgrading `tree-sitter-python` therefore changed every
extracted graph while the fingerprint stayed byte-identical.

Three mechanisms this repo deliberately built were defeated by that. The
extraction cache partitions on `{cache_version}-{engine[:16]}` (#21), so a
grammar upgrade served parses produced by the OLD parser — precisely the
staleness partitioning was introduced to prevent. The daemon queues a rebuild
when a project's recorded engine identity differs from the current one (#18), and
a grammar upgrade produced no difference, so supervised repos kept graphs that
disagreed with their source. And `metadata.engine.fingerprint` is documented as
the engine that produced the content, which across a parser change it was not.

Not hypothetical: every runtime dependency is lower-bound only, and the installed
`tree-sitter` was already 0.25.2 against a declared floor of `>=0.23`.

`engine_identity` now folds a `parsers` record into the digest, and
`metadata.engine` reports it, so a reader can see *which* grammars produced a
graph rather than only that something moved:

```
"parsers": "tree-sitter=0.25.2,tree-sitter-go=0.25.0,..."
```

An absent grammar is recorded as `absent` rather than omitted — "go was
installed" and "go was not" are different engines and must not share a
fingerprint — and a missing grammar degrades that one language without making
identity itself fail. `ENGINE_SCHEMA_VERSION` is now `2`: a fingerprint computed
under schema 1 covered graphite's own files only, so the two are not comparable.

**Expect a one-time rebuild everywhere.** Every fingerprint changes, so the first
run after upgrading re-extracts and re-partitions its cache. That is the correct
signal, not a side effect.

**A transport failure now says what it observed (#51).** The deep MCP probe's
diagnostic read every field off a `ProbeProcessResult`, and a transport failure
never produces one — `run_bounded_process` raises instead of returning. So on a
timeout, the one failure the diagnostic exists to explain, it printed `<none>`
for every field. A real CI sighting was consequently uninterpretable.

`ProbeProcessError` now carries `elapsed_seconds`, `budget_seconds`,
`stdout_bytes` and `stderr_bytes`. Counts and timings only: the error type is
contractually free of process data, and tests pin that a child's output cannot
reach `str(exc)`. Numbers are safe to carry there for the same reason
`os_error` already was.

Read `elapsed_s` against `budget_s`. At or below budget, the deadline fired on
time and the child did not answer within it — note a normal timeout lands
*below* budget, because the runner reserves up to 40% of it for cleanup and
enforces the earlier execution deadline. **Above** budget means our own deadline
was late, i.e. the process was starved of CPU — the load hypothesis, which
nothing in the log could previously express. `stdout_bytes` splits the first
case: zero means the child never produced a byte, non-zero means it was alive
and progressing.

Live output, same probe, two failures:

    deep_mcp output_limit: elapsed_s=0.170 | budget_s=0.35 | stdout_bytes=1048577
    deep_mcp timeout:      elapsed_s=0.325 | budget_s=0.35 | stdout_bytes=0

### Added

**`graphite debt` reports declared blind spots and their age.** A blind spot that
is DECLARED is working as designed; an undeclared one is the failure. The command
prints open and retired entries with time-to-retire, so the count is auditable
rather than a claim. Retirement is recorded against the fix that earned it.

**The public API surface is declared.** `docs/` now states what consumers may
depend on and what they may not. Six repos import this package from one install,
so "it happened to work" was the only contract they had; anything not listed is
explicitly not a promise.

**Release verification targets the BUILT DISTRIBUTION, not the source tree.**
Checks that passed against the working tree could not see what the wheel actually
contained — the two differ precisely where packaging bugs live.

**CommonJS is modelled (#49).** `require('<literal>')` now emits a real
`imports` edge — resolved in-repo, or `EXTERNAL_IMPORT` for a bare package,
exactly as the ESM equivalent does. Four call shapes that previously landed on
a same-file phantom now bind to their definition:

| shape | before | after |
|---|---|---|
| `const { f } = require('./x')` → `f()` | phantom | binds |
| `const m = require('./x')` → `m.f()` | phantom | binds |
| `module.exports.f = f` → `m.f()` | phantom | binds |
| `import * as ns from './x'` → `ns.f()` | phantom | binds |

`_ImportBindings` gained `namespaces`, mapping a whole-module local name to the
file it stands for, and `_resolve_call` turns `m.f()` into that file's `f`.
This is a mirror of what Python's `alias_map` has always done for `import x` +
`x.attr()`, not a new design. Detection of the `require()` shape lives in one
predicate used by both the binding collector and the edge-emitting walk, so the
two cannot drift.

Measured on a four-file fixture, JavaScript and TypeScript both: calls
**1/2 → 5/5**, imports **1/1 → 2/2**, placeholder share **0.143 → 0.077** as the
`m.f` phantoms stopped being invented, and the graph moved unhealthy → healthy.
Note the calls denominator *grew* while the ratio rose — newly bound sites are
sites that were previously never counted. `imported-by src/mod.js` now answers
`consumer.js` at `decision_grade` instead of answering nothing at that grade.

One interaction worth recording: `_resolve_method_dispatch` re-points any call
edge carrying `_member` by method NAME alone, so a namespace-resolved `m.f()`
would have been stolen back by any same-named class method elsewhere. The walk
now omits `_member` when the namespace map resolved the target — the post-pass
exists for edges that are "only a file-scoped phantom", which these no longer
are.

Guarded against a false positive the fix itself introduced. The binding maps are
file-level while calls are walked per scope, so an inner `const m = ...`, a
parameter named `m`, or a second destructure of the same name is
indistinguishable from the module binding at resolution time — and `m.real()`
would have claimed the module's definition, putting a caller in `callers real`
that does not exist. A wrong edge is worse than a missing one.
`_rebound_local_names` distrusts any require-bound name that is bound more than
once anywhere in the file: deliberately blunt rather than modelling JavaScript
scope, because it **fails closed**, giving up an edge instead of inventing one.
Measured cost — in a file that rebinds the name, every `m.x()` in it loses
binding, which is exactly the pre-#49 behaviour for that file and no worse.
Applied only to names CommonJS introduced; ESM binding forms are
statement-level and were never re-derived from a declarator.

Also measured and correct without change: a module-object call to a member the
module does not export (`m.notExported()`) produces an unbound placeholder and
LOWERS the ratio rather than fabricating a binding.

Three caveats retired on re-measured evidence — `ts-destructured-locals-unbound`
(declared 2026-07-27), `js-require-emits-no-import-edge` and
`js-module-object-calls-unbound` (both declared that morning). One added:
`js-dynamic-module-load-unmodelled`, because `require(expr)` and `import()`
expressions still emit nothing, measured the same day — note the second has a
string literal, so "non-literal argument" is not the test; `import()` is an
expression rather than an import statement. And one more,
`js-shadowed-module-local-unbound`, for the shadowing subset above. **Both
non-detection classes are narrowed, not gone**, so `imports` stays in
`NON_DETECTION_RELATIONS` for JavaScript and TypeScript. Retiring either
predecessor without its successor would have removed the honest grade from a
class of absence that is still not proof — the tidy-registry mistake, made once
and caught, then nearly made again one entry over.

### Fixed

**`imported-by` reported a confident false absence for CommonJS.** A
`require('./mod')` is a call expression, not an import statement, so the import
extractor never sees it and no candidate edge is emitted. A missing *site*
cannot lower a ratio computed over sites, so the metric graded its own blind
spot healthy. Measured on a two-file fixture where `consumer.js` requires
`./mod` **twice**: the graph held exactly one import edge (the unrelated ESM
one), the imports cell read `total 1, bound 1, ratio 1.0`, and `imported-by
src/mod.js` answered nothing at **`decision_grade`** — the grade whose contract
is "an empty result is a trustworthy absence".

This is round 55's defect in the relation that had been excused from it. That
round added `NON_DETECTION_RELATIONS` for `calls`, because a callback-registered
caller emits no edge, and recorded `imports` as exempt: "an import is a
syntactic construct that extraction either sees or does not… add a relation only
with a measured non-detection case, not on suspicion." CommonJS is that measured
case. **A resolution metric cannot underwrite a coverage claim**, in any
relation.

`imports` now joins the non-detection set, **scoped by language** — Rust `use`
and Go imports have no dynamic form graphite models, so their absences are still
evidence and are not downgraded to buy a fix for JavaScript. The empty-listing
line also names the construct that went undetected, since the reader's next
action is a grep and which one depends on whether the missing edge is a callback
registration or a `require()`.

Two blind spots declared the day they were measured, per the caveat process:
`js-require-emits-no-import-edge` (imports) and `js-module-object-calls-unbound`
(calls — `const m = require('./x'); m.f()` and `import * as ns; ns.f()`, neither
covered by the existing destructuring entry). Python already binds this shape
via `alias_map`; JavaScript has no equivalent. Extraction is unchanged — these
are declarations, not fixes.

**A failed Git version probe told the operator to upgrade a working Git.**
`GitUnsupportedVersionError` carries three unrelated conditions, and the one it
is named for is the rarest: the other two are a `--version` probe that timed out
or could not be launched. All three raised the literal "Git 2.38 or newer is
required" — a sanitized message, and a false statement in the two cases where no
version was ever read. It does not merely fail to help; it names a specific
remedy, and that remedy is wrong. `review` repeated the same literal one layer
up, `from None`, so the line a user actually sees on their terminal carried it
too.

Messages now come from a per-`reason` table of module constants
(`git_version_failure_message`). "Git 2.38 or newer is required" survives for
`too_old`, where it is exactly right; a timeout says it timed out; an
unrecognised reason says the version could not be verified rather than
inheriting a remedy. `review` looks up the same table instead of hardcoding a
literal — and deliberately does not pass `str(exc)` through, because that
message can carry Git's own output and keeping it off a terminal is what the
hardcoded literal was protecting. Mutation-proven: passing the exception's text
through fails three tests.

This is diagnosis, not a fix for #37 — the flake it makes readable has not
recurred in 40 CI runs since `b3ae61a`, and absence of a sighting is not
evidence of a fix.

**A package manager that printed anything was reported as not installed.**
`run_manager_version` gave `<manager> --version` a 64-byte output budget, and
`run_bounded_process` applies its budget **per stream** — so whatever the child
wrote to stderr competed with a limit sized for the version string on stdout.
Overflow raises `output_limit`, which the probe reported as
`manager_unavailable`: the same answer it gives when the manager is absent, so
TypeScript activation declined to proceed and named the wrong cause.
`_minimal_node_environment` forwards only locale variables and a PATH, so
nothing silences npm notices or Node deprecation warnings, and either clears 64
bytes on its own. The budget is now `MANAGER_VERSION_OUTPUT_LIMIT` (8 KiB) —
still a hard flood bound, three orders of magnitude under the install budget,
and the only pathological one: a sweep of every `max_output_bytes` call site
found the next smallest at 4 KiB.

Found from the other side, as #48: Python 3.14 added a `site.py` check that
warns when `sys.prefix` disagrees with the `pyvenv.cfg` layout, and the POSIX
activation fixture wrote that landmark beside the copied interpreter rather
than one level above its directory. 287 bytes on stderr, two red legs on ubuntu
and macOS, reported as a missing package manager. Reproduced locally under WSL
on CPython 3.14.7 — a different distribution from the runner's — and attributed
with a 2×2: **either fix alone clears it**, the budget because the probe stops
caring what the child says, the fixture because the child stops saying it. Both
shipped; the fixture was wrong on its own terms, and the budget defect was
never about 3.14.

**A version probe would not say what it had refused.** `run_manager_version`
flattened the provenance revalidation result into `manager_unavailable`, while
`run_install` had always returned it as-is — one test asserted both, side by
side, on a single command. "Your toolchain changed under us and graphite
refused to launch it" and "there is no package manager here" are opposite
operator situations, and they arrived as one string from a check whose whole
job is to be believed. The reason is now returned unflattened
(`executable_changed` / `command_changed`). The user-visible activation reason
is unchanged: the mapping in `typescript_activation` already defaults unknown
reasons to `manager_version_unavailable`.

**Every bounded subprocess reported a failed containment on macOS.**
`run_bounded_process` holds an exited child as an unreaped zombie on purpose, so
its pgid cannot be recycled under the signals that follow. On darwin that makes
the process group unsignalable and `killpg` answers **EPERM** — where Linux
answers success for the identical state — so cleanup called every successful
probe a failure. That is 46 of the 62 macOS test failures in #46, and in
ordinary use it made `doctor` and every routing probe unusable on macOS.

Measured on macos-latest 3.12.10 with ubuntu-latest as the control, four process
states each. A live descendant in the same group makes darwin answer OK, which
is what licenses reading EPERM as "nothing left to signal" rather than "not
allowed to signal"; the transport also creates the group itself via `setsid()`
from its own uid, so a member it may not signal is not reachable. The reading is
gated on the leader having exited — on the timeout path the leader is alive and
EPERM stays a failure. Linux behaviour is untouched.

**A failed cleanup overwrote the diagnosis it was called to follow.** Every
recheck after cleanup was guarded by "only if nothing failed yet"; the cleanup
assignment itself was not, so a run that had already determined `timeout`,
`output_limit` or `input_failed` reported `cleanup_failed` instead. Precedence
is now explicit — a transport failure, then the child's own non-zero exit, then
`cleanup_failed` only when there is nothing else to report — and a failed
containment rides on `ProbeProcessError.cleanup_failed` rather than replacing
the code. It is still raised, never returned as success.

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

**The distribution is now named `graphite-code`, and the name it is looked up by
is pinned.** PyPI's `graphite` belongs to another project. The import package is
still `graphite`; only the distribution name changed. A test pins the lookup name
because `importlib.metadata` fails silently on a mismatch — which is exactly how
the CLI came to report no version at all.

**The CLI reported nothing instead of an unresolvable distribution.** A failed
metadata lookup fell through to silence, so a broken install and a working one
were indistinguishable at the one command a consumer would use to tell them
apart. It now says which distribution it could not resolve.

**One machine's drive layout was welded into a published tool.** Default paths
and documentation carried this checkout's own absolute paths, which would have
shipped to every user of a release — and to PyPI's rendered README. Fixed in the
config defaults and the docs, with a check that keeps them gone.

**Six sqlite connections were left to the garbage collector.** `_connect`
orphaned its handle when configuration failed; the routing and lifecycle stores
left connections unclosed on several paths; and the test fixtures leaked the same
handle the source did, which hid the defect from the suite that should have
caught it. On Windows an unclosed handle blocks the file, so recovery depended on
GC timing. `conftest` now fails the suite when a handle is never closed — the
instrument that found the rest.

**A running interpreter is trusted by identity, not by its path.** Path-based
comparison misidentified the active interpreter when the same binary was reachable
by more than one path, which is normal under virtualenvs and symlinks.

**A test could pass while the thing it checked never happened (#50).** Sixteen
wall-clock assertions were demoted to named hang guards: a stopwatch bound beside
a structural assertion adds flake surface and no correctness. Two orphan checks
were worse than flaky — their reveal window was too short, so a surviving orphan
could go unseen and the test would pass. Each fix is mutation-proven.

**A failed Git step did not say which step failed (refs #37).** Git errors are now
attributed to the operation and carry the OS error text, so a CI sighting is
interpretable without a local repro.

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
