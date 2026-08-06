"""Broker for the shared agent channel.

Some agents are sandboxed to their own workspace and cannot write to the channel
at all, and that restriction is deliberate. So access is *mediated* here rather
than *granted* as a path: agents call graphite, graphite writes.

Two invariants carry the whole design.

**Identity is derived, never declared.** Every agent commits under the operator's
git identity, so the `Co-Authored-By` trailer is the only answer to "who wrote
this". A caller-supplied author would let any agent forge any trailer, so the
author comes from the repository the broker is running in, resolved through a
registry committed in the channel. An unregistered repository is refused --
never defaulted to "unknown", because a permissive fallback is exactly the hole
the derivation exists to close.

**Nothing on disk is ever rewritten.** Rounds are created once. Status is an
append-only event log (`status/NN/SEQ-*.json`) folded to a current value at read
time. A status field inside a round would mean editing the round, and a log you
can rewrite answers "who said what" only as well as its last edit.

See docs/superpowers/specs/2026-08-01-agent-channel-broker-design.md.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

CHANNEL_DIRNAME = ".agent-channel"
REGISTRY_FILENAME = "agents.json"
ROUNDS_DIRNAME = "rounds"
STATUS_DIRNAME = "status"

#: Closed on purpose. An open-ended status string would make the audit report
#: ungradeable -- it could not tell a state it understands from a typo.
STATUSES = (
    "open",
    "delivered",
    "acknowledged",
    "blocked",
    "done",
    "withdrawn",
    "superseded",
)

#: Only the broker may assert these; an agent claiming them would defeat the
#: point of recording them separately from what the agent says it did.
BROKER_ONLY_STATUSES = frozenset({"open", "delivered", "superseded"})
RECIPIENT_STATUSES = frozenset({"acknowledged", "blocked", "done"})
AUTHOR_STATUSES = frozenset({"withdrawn"})

_ROUND_IN_NAME = re.compile(r"round-(\d+)")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_LOCK_TIMEOUT_SECONDS = 30.0
# Deliberately under the lock timeout: a git call that outlives the lock wait
# would strand every other agent behind a holder that is already doomed.
#
# Note this holds per CALL, not per critical section. `register_agent` runs
# `ensure_channel_hook`'s config call plus a three-call `_commit`, so a
# perfectly healthy holder can legitimately sit for ~80s -- well over the 30s a
# waiter is willing to wait. A `lock_timeout` is therefore not by itself
# evidence of a wedge.
_GIT_TIMEOUT_SECONDS = 20.0

#: How long before an unreleased lock is assumed abandoned.
#:
#: Staleness is TTL-only, following `buildlock.py` -- read its module docstring
#: before "improving" this. `os.kill(pid, 0)` is the obvious liveness idiom and
#: is WRONG here: on Windows, Python's `os.kill` ignores signal 0 and calls
#: TerminateProcess, so the portable-looking probe kills the very process it is
#: checking. The `pid` and `host` in the record are DIAGNOSTIC ONLY -- they tell
#: an operator who to go look at; they are never probed.
#:
#: 300s is ~3.7x the ~80s worst-case LEGITIMATE hold derived above. The margin
#: is lopsided on purpose: breaking a live lock puts two writers in the critical
#: section, which is the exact failure this lock exists to prevent, and a torn
#: status directory is worse than a wedge that clears itself five minutes later.
_LOCK_STALE_SECONDS = 300.0

#: Channel content whose orphaned presence changes behaviour. Everything else a
#: dead holder might leave behind is inert.
_RESIDUE_PREFIXES = (f"{STATUS_DIRNAME}/", f"{ROUNDS_DIRNAME}/", REGISTRY_FILENAME)


class ChannelError(Exception):
    """A refusal with a machine-readable code, so callers can branch on cause."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass
class Round:
    number: int | None
    path: Path
    title: str
    body: str = ""
    author: str | None = None
    posted: str | None = None
    to: list[str] = field(default_factory=list)
    supersedes: int | None = None

    @property
    def legacy(self) -> bool:
        """True when the round predates the broker.

        These carry no stamped author, and the channel's own history cannot
        supply one either: the 37 relocated rounds were moved in a single commit
        that did not carry history, so git reports graphite as the author of all
        of them regardless of who wrote them. Reporting that as fact would be a
        lie, so the reader marks them and the audit report grades them `legacy`.
        """
        return self.author is None


# --- channel location -------------------------------------------------------


def channel_root() -> Path:
    """Resolve the channel from machine-local config, never from a stored path.

    An absolute path must not be written into managed instruction files: those
    are committed and pushed in consumer repos, so the operator's directory
    layout would land on their remotes.
    """
    from .config import default_projects_root

    return (default_projects_root() / CHANNEL_DIRNAME).resolve()


def require_channel(root: Path | None = None) -> Path:
    root = root or channel_root()
    if not root.is_dir():
        raise ChannelError("channel_missing", f"channel not found at {root}")
    if not (root / ".git").exists():
        raise ChannelError(
            "channel_not_git",
            f"channel at {root} is not a git repository, so changes there are unauditable",
        )
    return root


# --- identity ---------------------------------------------------------------


def _normalize(path: Path) -> str:
    """Canonical key for registry lookup.

    `resolve()` collapses `..`, `.` and trailing separators, and casefold makes
    the lookup survive Windows' case-insensitive paths. Without this the hard
    refusal below would fire on a correct-but-differently-spelled path, which
    would train operators to widen the registry.
    """
    return os.path.normcase(str(Path(path).resolve()))


def read_registry(root: Path) -> dict[str, str]:
    path = root / REGISTRY_FILENAME
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ChannelError("registry_unreadable", str(exc)) from exc
    if not isinstance(raw, dict):
        raise ChannelError("registry_unreadable", "agents.json is not an object")
    return {_normalize(Path(k)): str(v) for k, v in raw.items()}


def write_registry(root: Path, mapping: dict[str, str]) -> None:
    (root / REGISTRY_FILENAME).write_text(
        json.dumps(mapping, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


_AGENT_ID = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*-agent$")

#: The channel's `commit-msg` gate, derived from `agents.json` rather than a
#: hardcoded list.
#:
#: The original carried its own allowlist of agent names, which meant
#: registering a newcomer produced a repo that resolved an identity and was then
#: rejected on every commit -- registration that lies. Two records that drift is
#: the failure; there is one record on purpose.
#:
#: It also requires the NAME and ADDRESS to be the same agent. The old
#: alternation `(codex|aramid|graphite)-agent <(codex|aramid|graphite)@...>`
#: matched the two halves independently, so `codex-agent <aramid@agents.local>`
#: passed.
#:
#: Fails closed: a missing or unparseable registry rejects rather than waving
#: commits through.
COMMIT_MSG_HOOK = '''#!/bin/sh
# Agent channel audit gate. GRAPHITE-MANAGED -- edit via `graphite channel`.
#
# Every change here must say WHICH AGENT made it. The committer identity is the
# operator's for all agents, so without a trailer the history cannot answer
# "who wrote this" -- which is the whole audit requirement.
#
# Rejects rather than warns: a warning on a commit is a warning nobody reads.
# There is deliberately no --no-verify guidance; if a commit cannot name an
# agent, it does not belong in the channel.

MSG_FILE="$1"
ROOT="$(git rev-parse --show-toplevel)"

# `python3` FIRST, and `python` only as a fallback. Ubuntu ships no `python` at
# all and macOS removed it in 12.3, so hardcoding it made this gate reject every
# commit on any non-Windows machine -- an outage, not a rejection. Windows is the
# single platform where the old spelling worked.
#
# `-I` rather than `-P`: both drop the CWD from `sys.path`, but `-P` is 3.11+ and
# this hook is COMMITTED, so it runs under whatever `python3` a machine happens to
# have. `-I` has existed since 3.4 and cannot brick the gate on an older one.
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    # `echo`, not `cat`: this branch fires when the environment is already
    # threadbare, and a builtin needs nothing on PATH to report it.
    echo "[agent-channel] BLOCKED: no Python interpreter found (tried python3, python)." >&2
    echo "" >&2
    echo "The audit gate cannot verify this commit, so it is refused rather than" >&2
    echo "waved through. This is NOT a problem with your commit message. Install" >&2
    echo "Python or put it on PATH, then commit again." >&2
    exit 1
fi

if "$PY" -I - "$MSG_FILE" "$ROOT/agents.json" <<'PYEOF'
import json, re, sys

try:
    message = open(sys.argv[1], encoding="utf-8", errors="replace").read()
    agents = set(json.load(open(sys.argv[2], encoding="utf-8")).values())
except Exception:
    sys.exit(1)

for agent in agents:
    local = agent[: -len("-agent")] if agent.endswith("-agent") else agent
    pattern = r"^Co-Authored-By: %s <%s@agents\\.local>$" % (
        re.escape(agent),
        re.escape(local),
    )
    if re.search(pattern, message, re.MULTILINE):
        sys.exit(0)
sys.exit(1)
PYEOF
then
    exit 0
fi

cat >&2 <<EOF
[agent-channel] REJECTED: this commit names no agent.

Every commit in the channel must carry the trailer of the agent that wrote it,
so history can answer "who changed this, and why":

    Co-Authored-By: <name>-agent <name@agents.local>

Only your own, and only an agent registered in agents.json. Register one with:

    $PY -P -m graphite channel register <repo-path> <name>-agent

The commit message must also state the reason for the change.
See PROTOCOL.md, "Audit requirements".
EOF
exit 1
'''


def ensure_channel_hook(root: Path) -> dict:
    """Install/refresh the audit gate and point `core.hooksPath` at it."""
    hooks = root / ".githooks"
    hooks.mkdir(parents=True, exist_ok=True)
    path = hooks / "commit-msg"
    path.write_text(COMMIT_MSG_HOOK, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    _git(root, "config", "core.hooksPath", ".githooks", check=False)
    return {"path": str(path), "changed": True}


def register_agent(root: Path, project_root: Path, agent: str) -> dict:
    """Bind a repository to an agent identity, and commit the change.

    This is the operator-side half the broker shipped without. `init` writes
    `.mcp.json` and doctrine telling an agent to call `channel_inbox`, but an
    unregistered repo is refused -- so without a registration command a newly
    onboarded agent is told to use tools that reject it, and the only remedy is
    hand-editing JSON in a repo it cannot reach. "Ask the operator" is not a
    mechanism; this is.
    """
    root = require_channel(root)
    if not isinstance(agent, str) or not _AGENT_ID.match(agent):
        raise ChannelError(
            "invalid_agent_id",
            f"{agent!r} must look like `name-agent` (lowercase, hyphen-separated)",
        )

    with _Lock(root) as lock:
        registry_path = root / REGISTRY_FILENAME
        raw: dict[str, str] = {}
        if registry_path.is_file():
            try:
                loaded = json.loads(registry_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise ChannelError("registry_unreadable", str(exc)) from exc
            if isinstance(loaded, dict):
                raw = {str(k): str(v) for k, v in loaded.items()}

        key = Path(project_root).resolve().as_posix()
        previous = None
        for existing in list(raw):
            if _normalize(Path(existing)) == _normalize(Path(project_root)):
                previous = raw.pop(existing)
        raw[key] = agent
        write_registry(root, raw)

        # The gate must be able to accept the newcomer, or this registration is
        # a lie. Refreshing it here keeps the two in step by construction.
        ensure_channel_hook(root)

        commit = _commit(
            root,
            [REGISTRY_FILENAME, ".githooks/commit-msg"],
            f"register {agent} for {Path(project_root).name}",
            # Graphite made this change, so graphite is credited -- attributing
            # it to the agent being registered would read as "codex registered
            # itself", which is false and is exactly the kind of claim the
            # trailer exists to keep honest.
            #
            # The exception is bootstrapping: in a channel where graphite-agent
            # is not yet registered, the hook would reject a graphite trailer,
            # so the first registration is signed by whoever it registers. That
            # is the one commit whose author is structurally unverifiable, and
            # it is unavoidable -- someone has to write the first row.
            "graphite-agent" if "graphite-agent" in raw.values() else agent,
        )

    result = {"ok": True, "agent": agent, "path": key, "previous": previous, "commit": commit}
    if lock.recovered is not None:
        result["lock_recovered"] = lock.recovered
    return result


def derive_identity(root: Path, project_root: Path) -> str:
    """Map the repo the broker runs in to an agent identity, or refuse."""
    registry = read_registry(root)
    key = _normalize(project_root)
    agent = registry.get(key)
    if agent is None:
        # Name the command, not the file. "Add it to agents.json" is advice a
        # sandboxed agent cannot act on -- the channel is the one place it
        # cannot reach, which is the whole reason the broker exists.
        raise ChannelError(
            "unregistered_project",
            f"{project_root} is not a registered agent repository. "
            f"Ask the operator to run: graphite channel register {project_root} <name>-agent",
        )
    return agent


def agent_email(agent: str) -> str:
    """`aramid-agent` -> `aramid@agents.local`, matching the channel's hook."""
    return f"{agent.removesuffix('-agent')}@agents.local"


def trailer(agent: str) -> str:
    return f"Co-Authored-By: {agent} <{agent_email(agent)}>"


# --- round documents --------------------------------------------------------


def _slug(text: str) -> str:
    slug = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    return slug[:60] or "round"


def parse_round(text: str) -> tuple[dict, str]:
    """Parse the flat front matter block.

    Hand-rolled rather than YAML on purpose: the schema is closed and flat, so a
    parser with no dependency and no surprises (`to: [a]` vs `to: a`) is worth
    more here than generality.
    """
    meta: dict = {}
    if not text.startswith("---\n"):
        return meta, text
    _, _, rest = text.partition("---\n")
    block, sep, body = rest.partition("\n---\n")
    if not sep:
        return {}, text
    for line in block.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if key in {"round", "supersedes"}:
            meta[key] = int(value) if value.isdigit() else None
        elif key == "to":
            meta[key] = [part.strip() for part in value.split(",") if part.strip()]
        else:
            meta[key] = value
    return meta, body.lstrip("\n")


def render_round(meta: dict, body: str) -> str:
    lines = ["---"]
    for key in ("round", "author", "posted", "title", "to", "supersedes"):
        value = meta.get(key)
        if value is None or value == [] or value == "":
            continue
        if isinstance(value, list):
            value = ", ".join(value)
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.rstrip("\n") + "\n"


def _load_round(path: Path) -> Round:
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = parse_round(text)
    number = meta.get("round")
    if number is None:
        match = _ROUND_IN_NAME.search(path.name)
        number = int(match.group(1)) if match else None
    title = meta.get("title") or _title_from_body(body or text) or path.stem
    return Round(
        number=number,
        path=path,
        title=title,
        body=body or text,
        author=meta.get("author"),
        posted=meta.get("posted"),
        to=list(meta.get("to") or []),
        supersedes=meta.get("supersedes"),
    )


def _title_from_body(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip() or None
    return None


def list_rounds(root: Path) -> list[Round]:
    rounds_dir = root / ROUNDS_DIRNAME
    if not rounds_dir.is_dir():
        return []
    loaded = [_load_round(p) for p in sorted(rounds_dir.glob("*.md"))]
    return sorted(loaded, key=lambda r: (r.number is None, r.number or 0, r.path.name))


def read_round(root: Path, number: int) -> Round:
    for entry in list_rounds(root):
        if entry.number == number:
            return entry
    raise ChannelError("round_not_found", f"no round {number}")


def next_round_number(root: Path) -> int:
    numbers = [r.number for r in list_rounds(root) if r.number is not None]
    return (max(numbers) + 1) if numbers else 1


# --- git --------------------------------------------------------------------


def _git(root: Path, *args: str, check: bool = True) -> str:
    """Run git for the channel, bounded and detached from the caller's stdin.

    Both guards exist because this runs inside `_Lock`, where a stall is not one
    caller's problem but a channel-wide outage that no timeout elsewhere can
    clear:

    - `stdin=DEVNULL`: the broker is a stdio MCP server, so its stdin is the
      JSON-RPC pipe. A child that inherits it can block on it, and anything it
      consumes is protocol the server never sees.
    - `timeout`: a hung git otherwise holds the lock until someone kills it by
      hand. Observed live -- `git add` blocked 45 minutes at 0.03s CPU, having
      never reached `.git/index.lock`, with every other agent locked out behind
      it.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise ChannelError(
            "git_timeout",
            f"git {' '.join(args)} exceeded {_GIT_TIMEOUT_SECONDS:g}s in {root}",
        ) from None
    if check and result.returncode != 0:
        raise ChannelError("git_failed", (result.stderr or result.stdout).strip())
    return result.stdout


def _commit(root: Path, paths: list[str], subject: str, agent: str) -> str:
    message = f"{subject}\n\n{trailer(agent)}\n"
    _git(root, "add", "--", *paths)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "--short", "HEAD").strip()


def _uncommitted_residue(root: Path) -> list[str]:
    """Channel files an abandoned holder left behind uncommitted.

    Automatic recovery makes this MORE important, not less. `status_events`
    reads from DISK, so an orphaned `status/NNN/*.json` written by a holder that
    died before committing silently suppresses redelivery of that round. While
    clearing the lock was a manual act it came with an instruction to go and
    look; once the lock clears itself, nobody is looking unless we say so.

    Best-effort by construction: this runs while we hold nothing, and a failure
    to scan must never block a recovery that is otherwise safe.
    """
    try:
        # `--untracked-files=all`: bare `--porcelain` collapses a wholly
        # untracked directory to `status/`, which is precisely the filename an
        # operator needs in order to go and look at it.
        porcelain = _git(root, "status", "--porcelain", "--untracked-files=all", check=False)
    except ChannelError:
        return []
    residue = set()
    for line in porcelain.splitlines():
        path = line[3:].strip().strip('"')
        if path.startswith(_RESIDUE_PREFIXES):
            residue.add(path)
    return sorted(residue)


class _Lock:
    """Exclusive lock so concurrent allocations cannot collide on a number.

    Create-only writes keep the critical section tiny -- allocate, write, commit
    -- because nothing is ever read-modify-written.

    A holder that is *killed* never runs `__exit__`, and the lock it leaves has
    no expiry of its own: bounding `_git` covers a holder that stalls, not one
    that dies. So the file carries `{pid, started_at, host}` and is broken once
    it passes `_LOCK_STALE_SECONDS`.
    """

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], float] = time.time,
        stale_seconds: float = _LOCK_STALE_SECONDS,
    ) -> None:
        self.root = root
        self.path = root / ".channel.lock"
        # `time.time`, never `time.monotonic`: the deadline below is monotonic
        # and it is right there to copy, but monotonic values are meaningless
        # across processes, which is the only comparison staleness makes.
        self._clock = clock
        self._stale_seconds = stale_seconds
        self._record: dict | None = None
        #: Set when this acquisition had to break an abandoned lock. Callers
        #: surface it, because a silent recovery would hide both the crash and
        #: whatever the dead holder left half-written.
        self.recovered: dict | None = None

    def _read_record(self) -> dict | None:
        """The holder's record, or None if absent, unreadable, or not a dict."""
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return loaded if isinstance(loaded, dict) else None

    @staticmethod
    def _identity(record: dict | None) -> tuple | None:
        return None if record is None else (record.get("pid"), record.get("started_at"))

    def _is_stale(self, record: dict | None) -> bool:
        started = None if record is None else record.get("started_at")
        if not isinstance(started, (int, float)) or isinstance(started, bool):
            # No usable timestamp: absent, garbage, or a record still being
            # written. Fall back to the file's own mtime rather than declaring
            # it stale outright. `_acquire` creates the file with O_EXCL and
            # writes the record immediately after, so "created, not yet written"
            # is a normal state on the ORDINARY path -- calling unparseable
            # content instantly stale would let a competing waiter break a lock
            # that had just been legitimately taken, on every contended
            # acquisition rather than only in some rare recovery. Garbage still
            # ages out; it just ages out by the clock rather than at sight.
            try:
                started = self.path.stat().st_mtime
            except OSError:
                return True  # already gone: there is nothing left to protect
        return (self._clock() - float(started)) > self._stale_seconds

    def _acquire(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        record = {"pid": os.getpid(), "started_at": float(self._clock()), "host": socket.gethostname()}
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        self._record = record
        return True

    def _break_if_abandoned(self) -> bool:
        """Remove an abandoned lock. True when this call removed one.

        The record is read TWICE and both reads must agree. Several agents
        wedged behind one dead holder all judge it stale at the same moment; the
        first breaker unlinks and immediately re-creates the lock for itself,
        and without the confirming read every other breaker would then unlink
        that FRESH lock in turn and all of them would enter the critical section
        together. Two writers here is the failure the lock exists to prevent.

        What survives is a single-syscall window between the confirming read and
        the unlink -- the same residual `buildlock` already ships with, and it
        is reachable only if the holder we just judged dead releases within it.
        """
        first = self._read_record()
        if not self._is_stale(first):
            return False
        if self._identity(self._read_record()) != self._identity(first):
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        held_for = None
        started = (first or {}).get("started_at")
        if isinstance(started, (int, float)) and not isinstance(started, bool):
            held_for = round(self._clock() - float(started), 1)
        self.recovered = {
            "pid": (first or {}).get("pid"),
            "host": (first or {}).get("host"),
            "held_seconds": held_for,
            "residue": _uncommitted_residue(self.root),
        }
        return True

    def __enter__(self) -> _Lock:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            if self._acquire():
                return self
            if self._break_if_abandoned() and self._acquire():
                return self
            if time.monotonic() >= deadline:
                held = self._identity(self._read_record())
                raise ChannelError(
                    "lock_timeout",
                    f"channel lock held at {self.path} by {held}",
                ) from None
            time.sleep(0.05)

    def __exit__(self, *_exc: object) -> None:
        # Release only what we still hold. If a break ever does overlap two
        # holders, unlinking someone else's lock on the way out would turn that
        # single overlap into a cascade.
        if self._identity(self._read_record()) != self._identity(self._record):
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- posting ----------------------------------------------------------------


def post_round(
    root: Path,
    project_root: Path,
    *,
    title: str,
    body: str,
    to: list[str] | tuple[str, ...] = (),
    supersedes: int | None = None,
    _force_number: int | None = None,
) -> dict:
    """Create a round. There is deliberately no `author` parameter.

    The caller supplies content, never identity and never a path: the author is
    derived and the filename is generated, which removes both forgery and path
    traversal as classes rather than filtering for them.
    """
    root = require_channel(root)
    agent = derive_identity(root, project_root)
    if not title.strip():
        raise ChannelError("empty_title", "a round needs a title")

    with _Lock(root) as lock:
        number = _force_number if _force_number is not None else next_round_number(root)
        stamped = _now()
        name = f"{stamped[:10]}-{agent.removesuffix('-agent')}-round-{number}-{_slug(title)}.md"
        target = root / ROUNDS_DIRNAME / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or any(
            r.number == number for r in list_rounds(root) if _force_number is not None
        ):
            raise ChannelError("round_exists", f"round {number} already exists")
        meta = {
            "round": number,
            "author": agent,
            "posted": stamped,
            "title": title.strip(),
            "to": list(to),
            "supersedes": supersedes,
        }
        target.write_text(render_round(meta, body), encoding="utf-8")
        commit = _commit(
            root,
            [f"{ROUNDS_DIRNAME}/{name}"],
            f"round {number}: {title.strip()}",
            agent,
        )

    result = {
        "ok": True,
        "round": number,
        "path": str(target),
        "author": agent,
        "commit": commit,
        "posted": stamped,
    }
    # Only present when it happened. A key on every response would be noise,
    # and this one has to read as an event.
    if lock.recovered is not None:
        result["lock_recovered"] = lock.recovered
    return result


# --- status: an append-only event log ---------------------------------------


def _status_dir(root: Path, number: int) -> Path:
    return root / STATUS_DIRNAME / f"{number:03d}"


def status_events(root: Path, number: int) -> list[dict]:
    """Every event ever recorded for a round, oldest first.

    A malformed event is surfaced rather than skipped: silently dropping it
    would let a hand-written file remove a status from the audit trail, which is
    precisely the tampering the report exists to catch.
    """
    directory = _status_dir(root, number)
    if not directory.is_dir():
        return []
    events: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {"status": None, "malformed": True}
        payload["file"] = path.name
        events.append(payload)
    return events


def current_status(root: Path, number: int) -> dict | None:
    """Fold the log: the last event wins. `None` means nothing has happened."""
    events = status_events(root, number)
    return events[-1] if events else None


def _write_status_event(
    root: Path,
    number: int,
    status: str,
    actor: str,
    *,
    broker: bool,
    reason: str | None = None,
) -> dict:
    with _Lock(root) as lock:
        directory = _status_dir(root, number)
        directory.mkdir(parents=True, exist_ok=True)
        seq = len(list(directory.glob("*.json"))) + 1
        name = f"{seq:04d}-{status}.json"
        target = directory / name
        if target.exists():
            raise ChannelError("status_exists", f"{name} already exists")
        event = {
            "round": number,
            "seq": seq,
            "status": status,
            "actor": actor,
            # True marks an event the BROKER wrote, not one the agent asserted.
            # The audit report leans on this to tell "we handed it over" apart
            # from "the agent says it acted".
            "broker": broker,
            "at": _now(),
            "reason": reason,
        }
        target.write_text(json.dumps(event, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        # Committed under the actor's trailer so the report can compare the two
        # and catch an event whose recorded actor is not who committed it.
        event["commit"] = _commit(
            root,
            [f"{STATUS_DIRNAME}/{number:03d}/{name}"],
            f"round {number}: {status}",
            actor,
        )
    # Added after the write, like `commit` above, so it stays out of the on-disk
    # event: this describes the acquisition, not what was recorded.
    if lock.recovered is not None:
        event["lock_recovered"] = lock.recovered
    return event


def record_status(
    root: Path,
    project_root: Path,
    number: int,
    status: str,
    *,
    reason: str | None = None,
) -> dict:
    """Record an agent-asserted status.

    Authorization is strict; transitions are not. `done` with no preceding
    `delivered` is allowed through and flagged by the report -- refusing it
    would lose the evidence that it happened, and real workflows outrun any
    state machine written in advance.
    """
    root = require_channel(root)
    agent = derive_identity(root, project_root)

    if status not in STATUSES:
        raise ChannelError("unknown_status", f"{status!r} is not one of {', '.join(STATUSES)}")
    if status in BROKER_ONLY_STATUSES:
        raise ChannelError(
            "broker_only_status",
            f"{status!r} is recorded by the broker, not asserted by an agent",
        )

    entry = read_round(root, number)
    if status in RECIPIENT_STATUSES and agent not in entry.to:
        raise ChannelError("not_recipient", f"round {number} is not addressed to {agent}")
    if status in AUTHOR_STATUSES and agent != entry.author:
        raise ChannelError("not_author", f"round {number} was not written by {agent}")

    return _write_status_event(root, number, status, agent, broker=False, reason=reason)


# --- inbox: notification and handover ---------------------------------------


def inbox(root: Path, project_root: Path) -> list[Round]:
    """Hand over rounds addressed to the caller that it has not been given yet.

    The `delivered` event is written HERE, by the broker, as the message goes
    out. The agent cannot assert it and cannot decline it, so an agent that
    ignores its inbox leaves a visible `delivered` with no follow-up rather than
    an absence indistinguishable from never having been told.
    """
    root = require_channel(root)
    agent = derive_identity(root, project_root)

    pending: list[tuple[int, Round]] = []
    for entry in list_rounds(root):
        if entry.number is None or agent not in entry.to:
            continue
        already = any(
            event.get("status") == "delivered" and event.get("actor") == agent
            for event in status_events(root, entry.number)
        )
        if not already:
            pending.append((entry.number, entry))

    for number, _entry in pending:
        _write_status_event(root, number, "delivered", agent, broker=True)
    return [entry for _number, entry in pending]


# --- audit report -----------------------------------------------------------

STALE_DAYS_DEFAULT = 3

#: Grades that mean the broker cannot vouch for the row. `legacy` is absent on
#: purpose: 37 rounds predate the broker and always will, so counting them would
#: leave the check permanently red -- and a check that is always red is one
#: nobody reads.
FAILING_VERIFICATIONS = frozenset({"uncommitted", "modified", "discrepancy"})

_TRAILER = re.compile(r"^Co-Authored-By:\s*(\S+-agent)\s*<", re.MULTILINE)


def _tracked(root: Path) -> set[str]:
    return {line.strip() for line in _git(root, "ls-files").splitlines() if line.strip()}


def _commits_for(root: Path, rel: str) -> list[str]:
    out = _git(root, "log", "--format=%H", "--", rel)
    return [line.strip() for line in out.splitlines() if line.strip()]


def _commit_agent(root: Path, sha: str) -> str | None:
    match = _TRAILER.search(_git(root, "show", "-s", "--format=%B", sha))
    return match.group(1) if match else None


def _dirty(root: Path) -> set[str]:
    """Tracked files with uncommitted working-tree changes.

    Without this, editing a tracked round and simply not committing would grade
    `verified`: the file is tracked and still has exactly one commit.
    """
    out = _git(root, "status", "--porcelain", "--untracked-files=no")
    return {line[3:].strip().strip('"') for line in out.splitlines() if line.strip()}


def _verify(root: Path, rel: str, tracked: set[str], dirty: set[str]) -> str:
    """Grade one file against its ORIGINAL committed content, not its current text.

    Grading the working-tree copy was a hole I shipped into the first draft and
    the tests caught: overwriting a brokered round erases its front matter, so
    the author reads as `None`, and the row would grade `legacy` -- which is the
    one degraded grade that deliberately does not fail the check. Tampering
    would have laundered itself into "predates the broker".

    Reading the author out of the file's first commit removes that move: what
    the round was when it was created is not something a later edit can change.
    """
    if rel not in tracked:
        return "uncommitted"
    if rel in dirty:
        return "modified"
    commits = _commits_for(root, rel)
    if not commits:
        return "uncommitted"
    created = commits[-1]
    original, _ = parse_round(_git(root, "show", f"{created}:{rel}", check=False))
    origin_author = original.get("author")
    if origin_author is None:
        # No stamped author at creation: this predates the broker. Its extra
        # commits are ordinary history, and nothing here can tell that apart
        # from tampering -- so say `legacy` rather than guess.
        return "legacy"
    if len(commits) > 1:
        return "modified"
    if _commit_agent(root, created) != origin_author:
        return "discrepancy"
    return "verified"


def _parse_at(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def build_report(
    root: Path,
    *,
    stale_days: int = STALE_DAYS_DEFAULT,
    now: datetime | None = None,
) -> dict:
    """Assemble the audit view. Every row states what the broker can vouch for."""
    root = require_channel(root)
    now = now or datetime.now(timezone.utc)
    tracked = _tracked(root)
    dirty = _dirty(root)
    entries = list_rounds(root)
    numbers = {e.number for e in entries if e.number is not None}

    rows: list[dict] = []
    anomalies: list[dict] = []
    for entry in entries:
        rel = entry.path.relative_to(root).as_posix()
        verification = _verify(root, rel, tracked, dirty)
        events = status_events(root, entry.number) if entry.number is not None else []
        current = events[-1] if events else None

        stalled = False
        if current and current.get("status") in {"delivered", "acknowledged"}:
            at = _parse_at(current.get("at"))
            stalled = at is not None and (now - at) > timedelta(days=stale_days)

        participants = {entry.author, *entry.to} - {None}
        for event in events:
            number = entry.number
            if event.get("malformed"):
                anomalies.append({"round": number, "kind": "malformed_status", "detail": event.get("file")})
                continue
            actor = event.get("actor")
            if actor not in participants:
                anomalies.append(
                    {"round": number, "kind": "status_actor_not_participant", "detail": actor}
                )
            event_rel = f"{STATUS_DIRNAME}/{number:03d}/{event['file']}"
            if event_rel not in tracked:
                anomalies.append({"round": number, "kind": "status_uncommitted", "detail": event["file"]})
        seen = [e.get("status") for e in events]
        if "done" in seen and "delivered" not in seen[: seen.index("done")]:
            anomalies.append({"round": entry.number, "kind": "done_without_delivery", "detail": None})
        if entry.supersedes is not None and entry.supersedes not in numbers:
            anomalies.append(
                {"round": entry.number, "kind": "supersedes_missing", "detail": entry.supersedes}
            )

        rows.append(
            {
                "round": entry.number,
                "path": rel,
                "title": entry.title,
                "author": entry.author,
                "to": entry.to,
                "posted": entry.posted,
                "supersedes": entry.supersedes,
                "verification": verification,
                "status": current.get("status") if current else None,
                "status_actor": current.get("actor") if current else None,
                "status_at": current.get("at") if current else None,
                "reason": current.get("reason") if current else None,
                "stalled": stalled,
            }
        )

    by_agent: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_agent.setdefault(row["author"] or "(legacy)", {"posted": 0, "open": 0})
        bucket["posted"] += 1
        if row["status"] not in {"done", "withdrawn", "superseded"}:
            bucket["open"] += 1

    degraded = [r for r in rows if r["verification"] in FAILING_VERIFICATIONS]
    return {
        "schema_version": 1,
        "ok": not degraded and not anomalies,
        "channel": str(root),
        "stale_days": stale_days,
        "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rounds": rows,
        "stalled": [r for r in rows if r["stalled"]],
        "anomalies": anomalies,
        "by_agent": by_agent,
        # Who may write at all. Without this the operator can see what was said
        # but not who is able to say anything -- and an agent that was onboarded
        # but never registered is silently mute rather than visibly absent.
        "registry": dict(sorted(read_registry(root).items())),
        "counts": {
            "total": len(rows),
            "legacy": sum(1 for r in rows if r["verification"] == "legacy"),
            "degraded": len(degraded),
        },
    }


def render_report(data: dict) -> str:
    """Human default. Every non-`verified` row must be visible without --json."""
    lines = [
        f"Agent channel audit — {data['channel']}",
        f"generated {data['generated']}   stale threshold {data['stale_days']}d",
        "",
    ]
    for row in data["rounds"]:
        number = row["round"]
        label = f"round {number}" if number is not None else "round ?"
        recipients = ", ".join(row["to"]) or "-"
        status = (row["status"] or "-").upper()
        lines.append(
            f"{label:<10} {row['author'] or '(legacy)':<16} -> {recipients:<16} "
            f"{status:<13} {row['verification'].upper():<12} {row['title']}"
        )
        if row["stalled"]:
            lines.append(f"{'':<10}   ^ STALLED — {status.lower()} since {row['status_at']}, no follow-up")
        if row["reason"]:
            lines.append(f"{'':<10}   \"{row['reason']}\"")

    registry = data.get("registry") or {}
    lines += ["", f"Registered agents ({len(registry)}) — only these can write:"]
    for path, agent in registry.items():
        lines.append(f"  {agent:<16} {path}")
    if not registry:
        lines.append("  (none — no agent can post; graphite channel register <repo> <name>-agent)")

    legacy = data["counts"]["legacy"]
    if legacy:
        lines += [
            "",
            f"{legacy} legacy round(s): authorship is NOT verifiable in this repo. The "
            "relocation did not carry git history, so every one of them reads as "
            "graphite's regardless of who wrote it — operation-firewall's log is "
            "authoritative for those.",
        ]
    if data["anomalies"]:
        lines += ["", "Anomalies:"]
        lines += [
            f"  round {a['round']}: {a['kind']}" + (f" ({a['detail']})" if a["detail"] else "")
            for a in data["anomalies"]
        ]
    lines += ["", "OK" if data["ok"] else "NOT OK — degraded or anomalous rows above"]
    return "\n".join(lines)
