# Graphite knowledge base

Symptoms, causes and remedies collected from operating graphite across
several machines and repositories. Each entry names the release or issue
that settled it, so you can tell whether the version you run has the fix —
and remember that a version is a coarse label: fixes have shipped without a
version change, so survey for the fix's marker or compare
`graphite --version` fingerprints rather than trusting the number.

Format: **symptom** → cause → what to do.

## Launching and installation

**`python -m graphite` runs something unexpected, or a hook silently does
nothing.** → `-m` puts the current directory first on `sys.path`; a
`graphite.py` or a `graphite/__init__.py` at the repository root is imported
instead of the installed package. A module-shaped shadow *executes* before
the failed submodule lookup errors, so "it would just error" is not a
mitigation, and a hook's `|| true` hides it completely. → Always launch with
`python -P -m graphite`, or use the `graphite` console script, which is
immune by construction. Everything `init` generates carries `-P`; a
launcher, hook or `.mcp.json` entry without it is a defect — report it. A
directory named `graphite/` with no `__init__.py` is only a namespace
portion and cannot shadow.

**`graphite --version` prints a version but not the fingerprint you
expected on another machine.** → The engine fingerprint folds in the parser
packages present in *that* environment, so the same wheel has a different
fingerprint in different environments. → Compare a machine against its own
recorded fingerprint, never across machines. Ship the version and the
fingerprint together in bug reports.

**`pip install graphite` installed something else.** → PyPI's `graphite` is
an unrelated project. → The distribution is `graphite-code`; the import
package stays `graphite`.

**Two versions seem installed; `--version` reports a `stale-install`.** →
An editable checkout and a wheel, or two dist-infos, resolve to different
code. → `python -c "import graphite; print(graphite.__file__)"` tells you
which one runs; keep one. Contributors: the dev environment belongs
*outside* the clone (see CONTRIBUTING), and its interpreter — not the
machine's — is what the release gates test with.

## Building

**`build` runs for minutes and memory climbs into the gigabytes on a large
repository.** → Before 1.0.0 the cycle report enumerated every simple cycle
of the import graph; on a repository whose packages import each other
densely (Django's `tests/` ↔ `django/`) that is exponential (#64). → 1.0.0
bounds the search (per strongly connected component, length ≤ 8, 10 000-cycle
budget) and reports `analysis.cycle_search` with whether the enumeration was
exact. Upgrade.

**`check` says the graph is stale right after you upgraded, though no
source changed.** → `engine_changed`: the extraction engine's identity moved
with the upgrade, so cached extraction is not trusted. → Rebuild. A running
daemon does this itself on its next discovery cycle (#18); `check
--ignore-engine` reports source drift only.

**The graph is missing a whole nested repository, or shows it stale.** →
The daemon supervises project roots it discovered; a repository nested inside
another is reported as `unsupervised_nested_repos` in `daemon-health` rather
than built silently. → Either let it be discovered as its own project or
exclude the subtree with a `.graphite-ignore` file.

**A build fails on Windows with `WinError 5` during `os.replace`.** →
Something held the artifact open for the length of a read — graphite's own
readers can do it — and Windows refuses the rename (#59, 3 of 1 585 daemon
builds). → Fixed in 0.5.1: the rename retries with a bounded Windows-only
backoff and the real error is no longer masked by cleanup. On older
versions the daemon's next cycle heals it; hook- or agent-driven builds fail
that turn.

**A file is missing from the graph, or the graph names a path the file has
been moved from, and nothing about the file is wrong.** → The file has the
same bytes as another file, or it was moved without being edited. The
extraction cache keyed an entry on a file's bytes while the entry names the
file's path, so identical files answered for each other: one kept its node
and its definitions and the others had none, on every build after the first
and by a race on the first (#74). Empty `__init__.py` files are the usual
case; copied modules and identical templates are the others. → Fixed in the
release that follows 1.2.1: the path is part of the key. On older versions no
setting avoids it, and deleting `.cache/graphite` before a build brings back
the race, not the nodes.

**Git-backed enumeration reports `git_unavailable` on a machine where Git
works.** → The `git --version` probe had its own fixed two-second budget
while the command it gated was allowed far longer; on a slow first launch the
probe timed out (#37). → Fixed in 0.5.3: the probe runs under its caller's
budget. Symptom on older versions: a filesystem-fallback scan and a
different file set.

**The cache directory keeps growing after upgrades.** → Extraction cache
partitions are keyed by engine identity, so every engine build creates one;
until #23 (fixed 2026-07-29) nothing removed the old ones — one machine
measured eleven partitions with exactly one reachable. → A build now
reclaims partitions no current engine can reach. If the directory still
grows, more than one engine is building the same repository (a development
checkout and an installed wheel, say), and each keeps its own reachable
partition; `rm -r .cache/graphite` is always safe — the next build
re-extracts.

**From a subfolder every query says `graph unavailable: graph_missing`,
`check .` says the graph is stale, and `build .` says "nested Git roots are
unsupported".** → The CLI took the directory it was typed in for the
repository (#81; the agent hooks had the same defect, #80, fixed in 1.2.1).
Each such run also left a `.graphite/` in the subfolder, and the refused
build a `.cache/graphite/`. → Fixed in the release that follows 1.2.1: reads
follow the repository root, and a build or a check of a path below it stops
before writing anything and names the root. On older versions run graphite
from the root. The leftover `.graphite/` and `.cache/` in subfolders are no
longer written or read by any of those commands, and can be deleted.

**`graphite build <path>` exits with "`<path>` is below the root of its Git
repository".** → A repository is built and checked from its root, and the
path given lies inside one. → Run the command from the directory the message
names. A folder that should be a project of its own needs its own `.git`.

**`build` or `check` exits with "`<path>` is below `<dir>`, which holds .git but
is not a repository root graphite takes", and queries from a subfolder say
`graph_missing`.** → The nearest `.git` above the folder is in a directory
the user running graphite does not own, or at a drive or filesystem root,
either as named or where a link to it or to its `.git` leads.
graphite takes a repository root from below it only where that directory
and its `.git` are the user's own. → If `<dir>` is not a repository you
made, find out who did: nothing there should be built or trusted on your
behalf. A folder that should be a project of its own needs its own `.git`,
which then stands nearer. If `<dir>` is your repository but another account
owns it, as after a copy or a checkout made by an elevated or a different
user, give it back to your account.

## Reading answers

**`callers X` returns nothing, and the aggregate health says `healthy:
true`.** → The aggregate can be healthy while the language your question
used is degraded; empty-and-degraded is *unknown*, not *none*. → Gate on the
answer-scoped `answer.grade`: `decision_grade` means an empty result is a
trustworthy absence; `inconclusive` means look elsewhere. The caveats list
says what is known to be missing.

**Hundreds of calls into the standard library appear bound to a test
double, or an `EXTERNAL_CALL` edge points inside the repository.** → Two
resolver defects, both fixed (#54 in 0.4.0, #56 in 0.5.0): in-repo names
shadowed the standard library at the wrong precedence, and a call could be
tagged external while its target was resolved locally. → Upgrade; `calls`
counts drop when a release removes false edges — honest accounting, not
regression.

**TypeScript resolution ratios look low even though the code is fine.** →
Calls into npm packages, injected test globals (`expect`, `describe`) and
language builtins used to be counted as resolver failures. → Since the
external-call classification fix they are tagged `EXTERNAL_CALL` and
excluded from the ratio; a residual low ratio now means real unresolved
calls (dynamic dispatch, decorator rebinding, `getattr`), which stay honestly
unbound.

**A `path` or `reaches` query says `no_path` with `truncated: true`.** →
Traversal is bounded (default depth 32) and the bound was hit. → That is
"not proven absent", not "absent". Narrow the endpoints or raise the limit
through the plan.

**A `path` or `reaches` query says `no_path` with `truncated: false`.** →
The walk finished and the graph holds no such path. That is still not proof:
a call the graph did not bind, or a caller registered as a callback, leaves a
real path out. → Read `answer.grade` on the result. `inconclusive` means the
call edges around those two nodes are mostly unbound; `advisory` means they
are fine and only the absence is unverified. It is never `decision_grade`.
Confirm with a text search before treating the two as unconnected.

**A node id from an older graph no longer resolves.** → Node ids are stable
only within an engine version; 0.5.0 deliberately changed id construction
(`index.ts` and `index.js` were one node before), and the Python
scope-identity release changed it again: a Python method or nested
definition's id is now `<file>_<qualname>_<hex>` (`m_py_worker_run_7c6b1a`),
while module-level ids are unchanged. → Never persist node ids across
upgrades; re-read them from the new graph, or key on paths and qualified
names (`graphite query "callers Worker.run"` resolves a qualname).

**`callers` or `impact` on a Python method misses a caller that calls it as
`obj.m()`, and the answer still says `decision_grade`.** → A member call is
dispatched by method name unless the source states the class. When more than
three methods of that name are defined in the caller's file and the files it
imports, the call gets no edge, and the dropped site also leaves the ratio.
Since Python methods got their own nodes, common names (`__init__`,
`get_queryset`, `to_dict`) hit this more often. The answer's caveats list
`python-member-call-over-dispatch-cap`. → Confirm with grep for `.m(`. File
input was unaffected where measured: on graphite and on Django, no file's
`impact <file>` result changed. Up to 1.2.1 this was every `self.m()` and
`super().m()` as well. From the release that follows, those two are bound by
class where the classes say which method runs (#73, the entry after next),
and come here only where they do not.

**`callers` on a Python method lists a function that calls another class's
method of the same name, and the answer says `decision_grade`.** → A member
call is bound by the method's NAME unless the source states what it is called
on: to every method of that name, up to three, in the caller's file and the
files it imports. Each such edge counts as bound, so the ratio cannot see
it. The answer's caveats list `python-member-call-bound-by-name`. → Read the
caller's line. Still bound by name: a parameter, an attribute, what another
call returns, a method the class inherits. For `self` and `super()` see the
next entry. Bound by
what the receiver is, from the release that follows 1.2.1: a class called to
build the object (`Beta().m()`), the class itself (`Beta.m()`), a local name
bound only from such a call; and a literal, `open(...)` and other builtins
that return a builtin type, or a local bound only from those, which get no
edge. Before that release `f.read()` on an opened file was a call to the
repository's `read` method wherever only one was in reach (#79).

**`callers` on a Python method misses a `self.m()` or `super().m()` written
in a base class, and the answer says `decision_grade`.** → From the release
that follows 1.2.1 a call on `self` or on a bare `super()` is bound to the
method Python runs for an object of the class the call is WRITTEN in: the
first `m` in that class's linearisation. For an object of a subclass,
`self.m()` runs the subclass's override, and `super().m()` can reach a class
the subclass added to its bases. Those edges are not in the graph. The
answer's caveats list `python-self-call-bound-for-enclosing-class`. → For an
override `Sub.m`, ask `callers` of the `m` it overrides as well: a `self.m()`
bound to `Base.m` is a call that runs `Sub.m` for a `Sub`. Before that
release every such call was bound by name: `super().__init__()` was a call
to the `__init__` it is written in, and to nothing once more than three
classes in reach had one (#73).

**A `self.m()` or `super().m()` is still bound by name, to several methods
or to none.** → The binding is by class only where the classes say which
method runs. It falls back to the name (`python-member-call-bound-by-name`,
and `python-member-call-over-dispatch-cap` over three methods) where a base
on the way is not a class graphite reads (a builtin, a
dotted base such as `models.Model`, a name imported through a re-export);
where a class on the way is decorated, is made with a keyword
(`metaclass=...`), has its name bound a second time (`C = deco(C)`, or the
class written twice under an `if`), or sits under an `__init_subclass__`;
where `m` is set on a class from outside its body (`K.m = ...`,
`cls.m = ...` in a class method) or assigned on the instance
(`self.m = ...`); and where the class that would provide `m` binds it some
other way than one `def` written straight in its body. → Nothing to work
around: these are the cases where the `def`s do not decide. Read the line.

**A `self.m()` bound by class points at a method that does not run.** → The
binding reads class statements and the assignments written on `self`, `cls`
and a class's own name. It does not see `setattr`, an alias (`me = self`,
then `me.m = f`), an assignment made in another file, a decorator on a method
that replaces it or hands it another object, a class decorator that returns a
class with other bases, or anything a base that was not read does to its
subclasses (a metaclass or an `__init_subclass__` behind `models.Model` can
replace a class's own `def`). → Where a class is patched at run time, read
the patching code; the graph holds what the class statements say (#73).

**`callers Verdict` lists the callers of a function `verdict`.** → Matching
ignores case, and among the matches a shallower path wins before exact case
is consulted. The class `Verdict` lives a directory deeper, so the function
was chosen. → The result says so: `resolution[].case_ambiguous` is `true`, the
grade is `advisory` at most, the caveat `target-resolved-to-another-spelling`
leads the list, and the class is in `alternates`. Ask again with its node id.
A name with one spelling is never marked, however many definitions share it.

## The daemon

**`daemon-health` shows warnings right after a restart.** → Pending initial
builds and "not built recently" while the successor rebuilds each activated
project. → Read the log for `build_succeeded` events; the warnings clear
within a cycle or two. Warnings that persist name a failing project.

**After `pip install --upgrade`, supervised graphs still carry the old
engine fingerprint.** → A long-running daemon keeps executing the code it
loaded at start. → Restart it: stop, install, start, in that order, so the
old process cannot begin a build under the new code halfway through the
install. `daemon-service-status` shows what the platform supervisor thinks;
`daemon-health` shows what is actually running.

**The daemon idles on `build_skipped_locked` for ten minutes after a crash
or a forced stop.** → Before 0.5.2 the daemon held a project's build lock on
its child's behalf, so a dead daemon left a lock the successor waited out
(#60). → Fixed: the lock follows the writer, and a successor releases a
killed child's lock by pid match. If you still see it on ≥ 0.5.2 with a
virtual-environment interpreter on Windows, note that the venv `python.exe`
is a launcher whose pid is not the writer's — the lock then expires by TTL.

**`daemon-status` says `ok` but a project's graph is stale.** → `ok`
summarises the status file; a `build_failed` event lives in the log. → Read
`<base>/.graphite-daemon/graphite-daemon.log`, then `graphite build` in the
project to see the error interactively.

**On Windows, `daemon-health` reports
`daemon_process_check_unavailable`.** → Process enumeration needed CIM
access the OS denied. → It is a warning, not a claim the daemon is stopped;
run the same command from an elevated shell for a definitive answer.

**The scheduled task cannot be created (policy).** → Task Scheduler is
blocked for the user. → `daemon-install-startup-windows` installs a
non-admin Startup-folder launcher with the same argument vector.

## Agents, hooks and instruction files

**An agent keeps grepping instead of querying the graph.** → The
repository's instruction files predate the graph-first workflow, or the
agent hook is missing. → Rerun `graphite init` (idempotent) and check
`GRAPHITE.md` carries the "Required Workflow" section; the hook that
reminds an agent on cross-file searches is written under `-P` and reported
by `graphite doctor`.

**`init` exited 0 but a consumer repository still has the old template.** →
Template versions drift per repository; an exit code says the run completed,
not that every file changed, and a repository's git history does not
reflect its working tree. → Survey for the literal marker the fix
introduced (for example `-P` in a hook command), not for a version number.

**A git hook works on one machine and not on a clone.** → Hook trampolines
under `.githooks/` embed an absolute interpreter path; they are machine-local
and gitignored on purpose. → Run `graphite init` on the new machine.

**A vendored hook manager (aramid, pre-commit) and graphite both want the
same hook.** → They chain: each graphite trampoline (`post-commit`,
`post-merge`, `post-rewrite`) first runs `<hook>.local` beside it in the
hooks directory. A hook already in the slot when graphite installs is moved
there; a tool installing after graphite writes its hook there itself.
`graphite doctor --json` names the file per trigger under `chain_slots` and
says whether one is present. Hooks graphite does not trigger on
(`pre-commit`, `pre-push`) are moved unchanged and never wrapped. → See
`docs/interop/` for the measured chaining behaviour; do not hand-edit either
side's hook.

## Releases and verification

**Minutes after a release, the index verifier's "listing" arm fails while
the digest arm passes.** → PyPI's simple index lags the upload. → Wait and
re-run; a listing failure alone, with the served digest already equal to the
approved one, is propagation, not a failed publication.

**The verifier reports "no provenance" for a release that shows
attestations on PyPI.** → Verifier versions before the 1.0.0 follow-up read
provenance from PyPI's legacy `/pypi/<project>/<version>/json` endpoint,
which never carries it. → Provenance lives at
`https://pypi.org/integrity/<project>/<version>/<file>/provenance` and in
the PEP 691 Simple JSON index; the current verifier reads the latter. A
tagged 1.0.0 checkout still carries the old arm and reports 5 of 6 on an
attested release for that reason alone.

**Two builds of the same commit produce different archive digests on
different operating systems.** → The zip `create_system` byte and the
platform's zlib differ; the *contents* are identical. → Compare with
`scripts/compare_dist.py`; the approved digests are CI's, and CI builds the
published artifact from the tag.

**A full-repository security scan on a tag push finds things a branch push
did not.** → A tag push scans wider than a branch push. → Run the whole-repo
scan before tagging (RELEASING.md lists the gates).

## Getting more help

Search the closed issues first — most of the entries above link to one.
Then open an issue with the template, `graphite --version`, the platform,
and the smallest reproduction. Security-relevant findings go through the
private path in [SECURITY.md](../SECURITY.md).
