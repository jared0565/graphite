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
