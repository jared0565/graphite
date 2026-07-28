# Autonomous repo: git-hook-driven graph maintenance

**Date:** 2026-07-28
**Status:** design, approved for planning

## Problem

Graphite's automation is entirely agent-gated. Every mechanism that keeps a
graph fresh depends on an LLM agent having opened the repo:

```
SessionStart  → agent-hook session-start   (writes activation marker)
Stop          → agent-hook stop            (refreshes marker)
PreToolUse    → agent-hook pre-tool-use    (strict grep denial)
```

Supervision follows those markers, which carry a 3600s TTL. So with no agent
running, every marker expires within the hour, the daemon supervises a set of
zero, and graphs freeze.

That is tolerable while nothing changes. It stops being tolerable the moment a
**human** edits code with no agent running: nothing rebuilds, the graph drifts,
and `query` output has **no staleness field of any kind** — verified, the keys
are `schema_version, node_count, edge_count, density, community_count,
nodes_by_kind, edges_by_relation, top_incoming, top_outgoing,
resolution_health, resolution`. A months-old graph will answer `decision_grade`
about code that has since changed, because `resolution_health` measures
*binding quality, not freshness*.

## Goal

A repo maintains its own graph, driven by git events, with no agent and no
required background process — while the daemon, when present, still accelerates
freshness between commits.

## Non-goals

- Continuous freshness without any process. Git events are the granularity.
- CI integration. Git hooks do not run in CI; that is a separate concern.
- Replacing the daemon. It remains the low-latency path.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Independence from | Agent **and** daemon | Hooks are the baseline; daemon accelerates |
| Hook behaviour | Build in background, detached | Commit stays instant; graph genuinely fresh ~15s later with nothing else installed |
| Distribution | Committed `.githooks/` **and** `init` wiring | Versioned and reviewable; `init` sets `core.hooksPath` and migrates |
| Triggers | `post-commit`, `post-merge`, `post-rewrite` | Every event that changes committed code. `post-checkout` excluded — branch switching is frequent and transient, and hooking it makes `git bisect` expensive |
| Concurrency | Repo-level build lock | First real concurrency in the system; see below |

Measured build times informing the "detached" choice (from the daemon log,
2026-07-28): 3s for 22 files, **15s for 277 files**, 7s for 285 files. A
synchronous `post-commit` would put 15s on every commit in `app`.

## Architecture

| Component | Status | Role |
|---|---|---|
| `.githooks/` | new, committed | Trampolines for the three triggers, plus pass-throughs |
| `graphite init` | extend | Writes hooks, sets `core.hooksPath`, migrates existing hooks |
| `graphite build --detach` | new flag | Re-spawns detached, returns immediately |
| `buildlock.py` | new module | Repo build lock, used by hook builds **and** the daemon |
| `graphite hooks install` | new | Installs the guarded template into git's `init.templateDir` so future clones self-arm |
| `graphite doctor` | extend | "Configured but not enforced" fresh-clone probe |

### Flow

```
git commit
 └─ .githooks/post-commit              (trampoline, ~5 lines)
     ├─ chained user hooks first       (app's deploy push still fires)
     └─ python -m graphite build . --detach     → returns in milliseconds
          └─ detached child: acquire lock ─┬─ held?  → exit 0, silently
                                           └─ free?  → build → release
```

The daemon is unchanged except that it acquires the same lock. That is what
makes "hooks baseline, daemon accelerates" true rather than two systems
fighting: whoever arrives first builds, the other no-ops.

### Shim rendering: constraints taken from aramid

`aramid/hooks.py` is the reference implementation on this machine and encodes
Windows correctness lessons that are not obvious. Graphite follows all of them:

- **Render shim bytes with `\n` only, and write via `Path.write_bytes`.** Never
  a text-mode write on Windows. Git-for-Windows' bundled `sh` chokes on a bare
  CR in the exec line, and a text-mode write silently reintroduces CRLF.
- **Never invoke a bare `python`.** Bake the blessed interpreter's absolute
  path, converted to Git-for-Windows `/c/...` form (aramid's `win_sh_path`) and
  double-quoted, with a `command -v py` / `py -3` fallback for when that path
  goes stale. This machine has up to five different `python`s visible to hook
  `sh`, including the WindowsApps store stub.
- **Bake no chaining state into the rendered bytes.** The shim always includes
  the chain-check block; `install()` alone decides whether the
  `<hook>.graphite-chained` sibling exists, via rename-on-chain. This is what
  makes regeneration idempotent.
- **Resolve the hook directory through `core.hooksPath`**, with a relative value
  resolved against the repo root (aramid's `hooks_dir`).

Marker pair, mirroring the existing managed-doc convention:
`# >>> graphite managed >>>` / `# <<< graphite managed <<<`.

### Detach belongs in Python, not the shell

Windows git hooks run under Git Bash, where backgrounding is unreliable, and
shell-level detaching is untestable. The hook is a trampoline; `--detach` does
the platform work (`DETACHED_PROCESS` on Windows, `start_new_session` on POSIX)
where the test suite can reach it.

## Migration

`core.hooksPath` redirects **every** hook, so anything left in `.git/hooks`
silently stops firing. The migration must carry across hooks graphite has no
interest in.

1. Read existing `core.hooksPath`. **If already set** (husky's `.husky`, etc.),
   do not hijack — install into that directory using chaining. Someone else
   owns hook policy there.
2. If unset:
   - Create `.githooks/`.
   - Move each non-sample `.git/hooks/*` → `.githooks/<name>.local`, preserving
     shebang and executable bit.
   - Write graphite trampolines for the three triggers.
   - **Write pass-through trampolines for every other migrated hook.** Without
     this, `pre-commit` / `pre-push` gates die the moment `hooksPath` is set.
   - Set `core.hooksPath .githooks` **last**, so no window exists where nothing
     fires.

### Exit-code semantics differ by hook class

```sh
# post-* : git ignores exit codes — never block
[ -f "$CHAINED" ] && "$CHAINED" "$@" || true

# pre-*  : exit code GATES the operation — must propagate
[ -f "$CHAINED" ] && { "$CHAINED" "$@" || exit $?; }
```

### Coexistence with aramid — verified safe

`core.hooksPath` was expected to orphan aramid's managed hooks. It does not:
`aramid/hooks.py:79-82` resolves its hook directory *through* `core.hooksPath`
("Respects `git config core.hooksPath` (husky et al.)"). After graphite sets it,
aramid's next `init` writes into `.githooks/` and both tools coexist.

Graphite must still migrate aramid's existing `.git/hooks` entries, because
those files do not move themselves and would be dead until `aramid init` reran.

### The hook that must not break

`BytesAI Learning/app/.git/hooks/post-commit` auto-pushes to `origin/master`
and triggers a Cloudflare deploy. It becomes `.githooks/post-commit.local`,
invoked first. Since git already ignores `post-commit` exit codes, behaviour is
bit-identical. **Verify live after migration by confirming the push still
fires — not by reading the file.**

## Lock protocol

Path: `<repo>/.cache/graphite/.build.lock` — already gitignored, and unlike
`graph-out/` it is not rewritten wholesale by builds.

Acquire by atomic `O_EXCL` create, contents `{pid, started_at, host}`. If held,
read it: if `started_at` exceeds the TTL, steal; otherwise report *held* and the
caller exits 0. Release in a `finally`.

TTL is **600s**, i.e. 2× the daemon's own `build_timeout_seconds` default of
240s (`DaemonOptions`, `daemon.py:116`). Any build still holding the lock past
double the daemon's own give-up point is dead by definition.

**TTL-only, deliberately no PID liveness check.** `os.kill(pid, 0)` is the
obvious idiom, but on Windows Python's `os.kill` ignores signal 0 and calls
`TerminateProcess` — the portable-looking probe would kill the process it was
checking. TTL expiry recovers more slowly from a crashed build but cannot do
that.

Graphite has no cross-process locking today; the daemon holds only in-process
`threading.Lock`, because it has always been the sole builder per repo. Hook
builds create the first genuine concurrency, so the lock ships in the same
change.

## Failure modes

| Failure | Behaviour |
|---|---|
| Python not found in hook | Interpreter discovery, then `exit 0` — never breaks git |
| Detached child dies | No graph update; next commit or daemon cycle covers it |
| Lock held | Silent no-op, exit 0 |
| Crashed build leaves lock | TTL expiry |
| `hooksPath` set, hooks missing or not executable | `doctor` probe reports not-enforced |

No feedback loop: builds write only to `graph-out/`, which is gitignored, so a
build cannot trigger a commit that triggers a build.

## The fresh-clone hole

`.git/hooks` is not version-controlled, so cloning an onboarded repo yields the
committed config with no hooks: configured, but silently not enforced.

**An earlier draft of this spec claimed "zero steps is unachievable by any
design." That was wrong.** aramid closes the hole with git's
`init.templateDir` (`aramid/hooks.py:render_template_shim`, `template_dir`):
git copies template hooks into every **new** `git init` / `git clone`, so
future clones self-arm with no per-clone step at all.

Graphite adopts the same three-layer approach:

| Layer | Covers | Cost |
|---|---|---|
| Committed `.githooks/` | Reviewable hook source, travels with the repo | none |
| `graphite init` sets `core.hooksPath` | **Existing** clones | one command per existing clone |
| `init.templateDir` (`graphite hooks install`) | **Future** clones on this machine | one command per machine, ever |

The template variant needs an **opt-in guard**, because git copies it into every
new repo including ones nobody onboarded. `GRAPHITE.md` at the repo root is
graphite's proof-of-onboarding — it is committed, so a fresh clone of an
onboarded repo has it, which is precisely what closes the hole:

```sh
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -n "$ROOT" ] || exit 0
[ -f "$ROOT/GRAPHITE.md" ] || exit 0
```

The guard **fails open** on every ambiguity — no git, not a repo, no
`GRAPHITE.md` — because a machine-wide template hook that errors would break
unrelated repos.

The `doctor` probe below remains necessary regardless: it covers clones made
before the template was installed.

That step is exactly the thing people forget, so it needs a diagnostic. aramid
solves this with `probe_enforcement` (`commands/doctor.py:488`), which reports
"configured but NOT enforced" when its config is present but the shims are
absent. Graphite gains the equivalent: `GRAPHITE.md` present but hooks missing
from the resolved hook directory → a doctor finding, with the remedy.

Deliberately mirroring aramid's severity rule: a repo with no `GRAPHITE.md` is
*not* onboarded and is not a finding. Nagging every un-onboarded repo is how a
real warning gets ignored.

## Testing

**Unit**
- Lock: acquire, held, stale-TTL steal, release-in-finally.
- Migration: pre-existing hook preserved, chained, exec bit kept; `pre-*`
  propagates exit code, `post-*` does not.
- `hooksPath` already set → no hijack.
- Doctor probe: onboarded repo with hooks removed → reports not-enforced;
  un-onboarded repo → silent.

**Integration**
- Real temp git repo, `git commit`, assert the graph updates.
- The detach path is tested by asserting the spawn call and its flags, plus one
  end-to-end run with `--detach` off. Spawning detached processes inside a test
  suite is how CI acquires orphans.

## Rollout

Six managed repos, all currently at DOC_VERSION 10. `aramid` and
`BytesAI Learning/app` carry pre-existing hooks and are the migration test
cases. Roll to one first, verify the chained hook still fires, then the rest.

## Open question deferred

Surfacing graph age / file-set drift in `query` output, so a stale graph
downgrades from `decision_grade` automatically. This design reduces staleness
but cannot eliminate it — a repo edited with no agent, no daemon, and no commit
is still silently stale. Tracked separately; not in scope here.
