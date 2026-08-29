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

**A node id from an older graph no longer resolves.** → Node ids are stable
only within an engine version; 0.5.0 deliberately changed id construction
(`index.ts` and `index.js` were one node before). → Never persist node ids
across upgrades; re-read them from the new graph, or key on paths and
symbol names.

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
same hook.** → They chain: graphite writes trampolines that call into the
other manager's hook when one is present. → See `docs/interop/` for the
measured chaining behaviour; do not hand-edit either side's hook.

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
