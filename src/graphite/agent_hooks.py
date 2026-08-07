"""Claude Code agent-hook handlers enforcing graphite-first on onboarded repos.

Every handler is fail-open by contract: any parse, IO, graph, or timing problem
yields ``None`` (no hook output) rather than an exception, so a hook bug can
never break a tool call or a session. The CLI wrapper adds a second catch-all.
"""
from __future__ import annotations

import json
import re
import shlex
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from . import savings as savings_model
from . import usage_ledger
from .activation import mark_active
from .config import Config
from .freshness import check_graph_freshness
from .graph_io import load_validated_graph_bundle
from .health import persisted_resolution
from .incident_ledger import record_incident, repo_ledger_dir

SESSION_CONTRACT = (
    "graphite-first: this repo uses a shared code graph. For cross-file questions "
    "(who-calls / who-reads / where-defined / data-flow / blast-radius / structure), run "
    'python -m graphite context <file> | impact <file> | query "..." | search "..." BEFORE '
    "grep/glob exploration; manual search is for literal text and filename lookups. Fall "
    "back only when a graph answer proved insufficient, and say so. See GRAPHITE.md."
)

_FRESHNESS_BUDGET_SECONDS = 2.0


def _payload_root(payload: dict[str, Any]) -> Path:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    return Path.cwd()


def _activate(root: Path) -> None:
    """Register this repo as open, so the daemon supervises it and leaves the
    rest of the machine alone.

    Guarded separately from the caller's own try/except: activation is
    bookkeeping, and this module is fail-open by contract -- a broken registry
    must never cost the agent its session context or its graph-first reminder.
    """
    try:
        mark_active(root, "claude")
    except Exception:
        return


def _freshness_within_budget(root: Path) -> tuple[str, str | None]:
    """Return ``(state, reason)`` within budget, never raising.

    ``state`` is 'fresh' | 'stale' | 'timeout' | 'unknown'.

    'timeout' and 'unknown' are kept apart deliberately (#24). Both used to
    collapse into a single "unknown" message, but they mean different things
    and the timeout case is the one most likely to be hiding a real STALE --
    a check that runs long suggests a large, cold, or unbuilt repo, which is
    exactly the population that tends to be stale. Reporting them identically
    made the reassuring reading the default in the least reassuring case.

    ``reason`` carries ``check_graph_freshness``'s own explanation
    (``engine_changed``, ``missing manifest``, ...) so a STALE message can say
    why without the agent running ``graphite check .`` a second time to find
    out. It is advisory: absent or malformed, the state still stands.
    """
    outcome: list[tuple[str, str | None]] = [("unknown", None)]

    def _worker() -> None:
        try:
            cfg = Config(output_dir=root / "graph-out", cache_dir=root / ".cache" / "graphite")
            status = check_graph_freshness(root, cfg)
            raw = status.get("reason")
            reason = raw if isinstance(raw, str) and raw else None
            outcome[0] = ("stale", reason) if status.get("stale") else ("fresh", reason)
        except Exception:
            outcome[0] = ("unknown", None)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    worker.join(_FRESHNESS_BUDGET_SECONDS)
    # The thread is a daemon and is never joined again, so a slow check is
    # abandoned rather than waited on. is_alive() is what separates "still
    # running" from "ran and gave up" -- outcome[0] alone cannot.
    if worker.is_alive():
        return ("timeout", None)
    return outcome[0]


def handle_session_start(payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        root = _payload_root(payload)
        _activate(root)
        if (root / "graph-out" / "graph.json").is_file():
            state, reason = _freshness_within_budget(root)
            if state == "stale":
                cause = f" ({reason})" if reason else ""
                status = f"Graph status: STALE{cause} - run `python -m graphite build .` before relying on graph answers."
            elif state == "fresh":
                status = "Graph status: fresh."
            elif state == "timeout":
                status = (
                    f"Graph status: unknown (freshness check did not finish in {_FRESHNESS_BUDGET_SECONDS}s) "
                    "- verify with `python -m graphite check .`."
                )
            else:
                status = (
                    "Graph status: unknown (check ran, could not determine) "
                    "- verify with `python -m graphite check .`."
                )
        else:
            status = "Graph status: missing - run `python -m graphite build .` to create it."
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"{SESSION_CONTRACT}\n{status}",
            }
        }
    except Exception:
        return None


PRE_TOOL_REMINDER = (
    "graph-first: this repo's graphite graph answers relationship questions. For who-calls / "
    "who-reads / data-flow / blast-radius / where-defined questions, use python -m graphite "
    'context <file> | impact <file> | query "..." instead of grepping or globbing across '
    "files. Literal text and filename searches are fine. Falling back after an insufficient "
    "graph answer is allowed - say so when you do."
)

STRICT_SUSPENSION_NOTE = (
    " (strict denial suspended: graph resolution health is low or unknown — "
    "grep fallback allowed.)"
)

MAX_HOOK_GRAPH_BYTES = 32 * 1024 * 1024
_MAX_TOKENS_CHECKED = 5
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _pattern_tokens(pattern: str) -> list[str]:
    """Identifier-like tokens from a grep pattern, deduplicated, capped."""
    seen: set[str] = set()
    tokens: list[str] = []
    for token in _IDENTIFIER_RE.findall(pattern):
        low = token.lower()
        if low in seen:
            continue
        seen.add(low)
        tokens.append(token)
        if len(tokens) >= _MAX_TOKENS_CHECKED:
            break
    return tokens


def _graph_symbol(root: Path, tokens: list[str]) -> tuple[str, str] | None:
    """First token that IS a graph node (exact id or name match), else None."""
    graph_path = root / "graph-out" / "graph.json"
    if not graph_path.is_file() or graph_path.stat().st_size > MAX_HOOK_GRAPH_BYTES:
        return None
    _, graph = load_validated_graph_bundle(graph_path, root=root, max_bytes=MAX_HOOK_GRAPH_BYTES)
    lowered = {token.lower(): token for token in tokens}
    for node_id, data in graph.nodes(data=True):
        node_lower = node_id.lower()
        name = str(data.get("name", "")).lower()
        if node_lower in lowered:
            return node_id, lowered[node_lower]
        if name and name in lowered:
            return node_id, lowered[name]
    return None


# Search binaries reachable from the Bash tool. The PreToolUse matcher was
# "Grep|Glob" -- tool NAMES -- so `grep` run through Bash bypassed this hook
# entirely, and consumer agents reported using exactly that route for
# cross-file work. Enforcing on the Grep tool alone enforces a naming
# convention, not a rule.
_SEARCH_COMMANDS = frozenset({
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "findstr",
    # PowerShell's grep, plus its alias. The PowerShell tool is a separate
    # tool name from Bash, so it needs its own matcher entry as well.
    "select-string", "sls",
})
# `find -name` is deliberately absent: a filename lookup is not a cross-file
# content search, and the graph-first contract explicitly permits it.
_SHELL_OPERATORS = frozenset({"|", "||", "&&", "&", ";", "|&"})
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Flags whose VALUE is the pattern. Without these, `grep -e PAT path` and
# PowerShell's `-Path . -Pattern PAT` put a non-pattern token first and the
# positional guess silently reads the wrong one -- which fails open, i.e. the
# search is allowed through. Honouring them explicitly is what closes that.
_PATTERN_FLAGS = frozenset({"-e", "--regexp", "-pattern", "--pattern"})
# Matched case-SENSITIVELY for the POSIX short forms. The old lookup lowercased
# every token, so `-E` (extended-regex mode, takes no value) collided with `-e`
# (whose value IS the pattern) and `grep -E -i PAT file` read `-i` as the
# pattern. That direction fails open, so it denied nothing -- but it also
# matched nothing it should have.
_PATTERN_FLAGS_EXACT = frozenset({"-e", "--regexp"})
_PATTERN_FLAGS_FOLDED = frozenset({"-pattern", "--pattern"})

# Redirection operators as `shlex(punctuation_chars=True)` emits them. It splits
# `2>/dev/null` into `2`, `>` and `/dev/null`, all three of which then looked
# like path ARGUMENTS to the search.
_REDIRECTION_TOKENS = frozenset({">", ">>", "<", "<<", "<<<", ">&", "<&", ">|", "&>", "&>>"})

# Flags whose value is a NUMBER, not a path. `grep -A 5 file` left `5` in the
# path list. Case-sensitive on purpose: lowercasing `-A` yields `-a`, which is a
# real grep flag that takes no value at all.
_VALUE_FLAGS = frozenset({
    "-A", "-B", "-C", "-m", "-d", "-j",
    "--after-context", "--before-context", "--context",
    "--max-count", "--max-depth", "--threads",
})


def _strip_redirections(argv: list[str]) -> list[str]:
    """Drop redirection operators, their targets, and any file-descriptor digit.

    Without this a single `2>/dev/null` contributed `2`, `>` and `/dev/null` to
    the path list, and `_is_single_file_scope` requires EVERY named path to be an
    existing file -- so one redirection turned a legitimate single-file search
    into a denial whose own message promised that such searches are allowed.
    """
    kept: list[str] = []
    skip_target = False
    for token in argv:
        if skip_target:
            skip_target = False
            continue
        if token in _REDIRECTION_TOKENS:
            # `2>file`: the descriptor was lexed as its own token just before.
            if kept and kept[-1].isdigit():
                kept.pop()
            skip_target = True
            continue
        kept.append(token)
    return kept


def _strip_flag_values(rest: list[str]) -> list[str]:
    """Drop the VALUE that follows a numeric-valued flag, so it is not a path."""
    kept: list[str] = []
    skip_value = False
    for token in rest:
        if skip_value:
            skip_value = False
            continue
        if token in _VALUE_FLAGS:
            skip_value = True
            kept.append(token)
            continue
        kept.append(token)
    return kept


def _is_outside_repository(root: Path, paths: list[str]) -> bool:
    """Every named path lies outside this repository.

    The graph describes THIS repo. A search rooted anywhere else is not a
    question it can answer, so denying it points the reader at a tool with
    nothing to say -- and names symbols from a repository the search never
    touched. Observed exactly that way: a scratchpad file was refused because
    the PATTERN happened to contain `START`, matching `daemon_start` here.
    """
    if not paths:
        return False
    try:
        root_resolved = root.resolve()
    except OSError:  # pragma: no cover - a repo root that cannot resolve
        return False
    for target in paths:
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            candidate.resolve().relative_to(root_resolved)
        except (ValueError, OSError):
            continue  # outside, keep looking
        return False  # at least one path is inside the repo
    return True


def _bash_command_heads(command: str) -> list[list[str]]:
    """argv of each command in `command` that is NOT downstream of a pipe.

    A search downstream of a pipe filters another command's output rather than
    searching the repository -- `graphite query ... | grep name` is the obvious
    case, and denying it would break the very commands the denial message
    recommends. Everything after a `|` is therefore dropped, while `&&`, `||`,
    `;` and `&` start a genuinely new command that is checked on its own.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []  # unbalanced quotes: fail open, never break the user's shell
    heads: list[list[str]] = []
    current: list[str] = []
    piped = False
    for token in tokens:
        if token in _SHELL_OPERATORS:
            if current and not piped:
                heads.append(current)
            # `||` is or-else, not a pipe -- only `|` and `|&` feed stdout on.
            piped = token in ("|", "|&")
            current = []
            continue
        current.append(token)
    if current and not piped:
        heads.append(current)
    return heads


def _bash_search_pattern(command: str) -> tuple[str, list[str]] | None:
    """`(pattern, path arguments)` when `command` searches the repo, else None.

    Deliberately conservative: the first non-flag argument is taken as the
    pattern, which is the positional order of every tool in
    `_BASH_SEARCH_TOOLS`. A flag that takes a separate value (`grep -e X`,
    `rg --glob X`) can mis-identify the pattern, and that is the safe
    direction -- a mis-read pattern simply fails to match a graph symbol and
    nothing is denied.
    """
    for argv in _bash_command_heads(command):
        while argv and _ENV_ASSIGNMENT_RE.match(argv[0]):
            argv = argv[1:]
        argv = _strip_redirections(argv)
        if not argv:
            continue
        name = PurePosixPath(argv[0].replace("\\", "/")).name.lower()
        if name.endswith(".exe"):
            name = name[:-4]
        rest = argv[1:]
        if name == "git" and rest and rest[0] == "grep":
            name, rest = "grep", rest[1:]
        if name not in _SEARCH_COMMANDS:
            continue
        rest = _strip_flag_values(rest)
        for index, token in enumerate(rest):
            if (
                token in _PATTERN_FLAGS_EXACT or token.lower() in _PATTERN_FLAGS_FOLDED
            ) and index + 1 < len(rest):
                paths = [
                    value
                    for position, value in enumerate(rest)
                    if position not in (index, index + 1) and not value.startswith("-")
                ]
                return rest[index + 1], paths
        positional = [token for token in rest if not token.startswith("-")]
        if not positional:
            continue
        return positional[0], positional[1:]
    return None


def _is_single_file_scope(root: Path, paths: list[str]) -> bool:
    """Every named path exists and is a file. Literal-text searches scoped to
    one file are always allowed -- the graph answers relationships, not what a
    specific file literally contains."""
    if not paths:
        return False
    for target in paths:
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.is_file():
            return False
    return True


def _denial_for(root: Path, pattern: str, paths: list[str]) -> str | None:
    """Shared by the Grep and Bash routes so both enforce the same rule."""
    if not pattern:
        return None
    if _is_single_file_scope(root, paths):
        return None
    if _is_outside_repository(root, paths):
        return None
    tokens = _pattern_tokens(pattern)
    if not tokens:
        return None
    match = _graph_symbol(root, tokens)
    if match is None:
        return None
    node_id, token = match
    return (
        f"graphite-first (strict): '{token}' is a symbol in this repo's code graph "
        f"({node_id}). Use the graph instead of cross-file grep: "
        f'python -m graphite query "callers {token}" | python -m graphite query "calls {token}" | '
        f'python -m graphite search "{token}" | python -m graphite context <file>. '
        "Literal-text searches scoped to a single file path are always allowed."
    )


def _strict_denial(payload: dict[str, Any], root: Path) -> str | None:
    try:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return None
        pattern = tool_input.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return None
        target = tool_input.get("path")
        paths = [target] if isinstance(target, str) and target else []
        return _denial_for(root, pattern, paths)
    except Exception:
        return None


MAX_CURSOR_SESSIONS = 20


def _entries_between(path: Path, start: int, end: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with open(path, "rb") as handle:
        handle.seek(start)
        blob = handle.read(max(0, end - start))
    for line in blob.decode("utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _cursor_number(value: Any, cast: type) -> Any:
    """Coerce a possibly-corrupted cursor field; non-numeric (or bool) -> 0.

    A raw ``int(...)``/``float(...)`` on a corrupted (e.g. hand-edited or
    concurrently-truncated) cursor field would raise before ``write_cursor``
    runs, permanently stalling the session (offset never advances, same
    failure repeats every turn). Guarding here lets the cursor self-heal.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return cast(0)
    return cast(value)


def handle_stop(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Turn-end savings summary; silent unless graphite was used this turn."""
    try:
        root = _payload_root(payload)
        # Stop fires once per assistant turn, which makes it the heartbeat that
        # keeps a working session inside the activation TTL. It must run before
        # the session_id guard below, which returns early on most turns.
        _activate(root)
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            return None
        ledger = usage_ledger.ledger_path(root)
        ledger_stat = ledger.stat() if ledger.is_file() else None
        size = ledger_stat.st_size if ledger_stat is not None else 0
        # Rotation (usage_ledger.record_usage) replaces the file at the same path with a
        # brand-new one; the new generation can coincidentally match the old byte offset
        # (e.g. identical repeated entries), so size alone can't detect it. st_ino changes
        # across the replace even when size doesn't, so it's used as the rotation signal.
        current_ino = ledger_stat.st_ino if ledger_stat is not None else None
        cursor = usage_ledger.read_cursor(root)
        sessions = cursor.get("sessions")
        if not isinstance(sessions, dict):
            sessions = {}
        state = sessions.pop(session_id, None)
        if not isinstance(state, dict):
            state = {"offset": 0, "tokens": 0, "seconds": 0.0, "ino": current_ino}
        offset = state.get("offset", 0)
        stored_ino = state.get("ino")
        # A stored `ino` that isn't an int (missing key on the older 3-key cursor
        # schema, or a corrupted value) never counts as rotation, nor does a current
        # ino of 0/None (filesystem doesn't report inodes) -- both fall back to the
        # offset-only heuristic below. That's fail-safe: no false resync/overcount
        # from an absent or unsupported inode, at the cost of not detecting a
        # same-size rotation on such filesystems (same limitation the size-only
        # check always had).
        rotated = (
            isinstance(stored_ino, int)
            and not isinstance(stored_ino, bool)
            and bool(current_ino)
            and stored_ino != current_ino
        )
        if not isinstance(offset, int) or offset > size or rotated:
            offset = 0  # ledger rotated or cursor damaged; resync
        new_entries = _entries_between(ledger, offset, size) if size > offset else []
        turn_tokens = 0
        turn_seconds = 0.0
        for entry in new_entries:
            est = savings_model.estimate_entry(entry)
            turn_tokens += est["tokens_saved"]
            turn_seconds += est["seconds_saved"]
        state["offset"] = size
        state["ino"] = current_ino
        state["tokens"] = _cursor_number(state.get("tokens", 0), int) + turn_tokens
        state["seconds"] = _cursor_number(state.get("seconds", 0.0), float) + turn_seconds
        sessions[session_id] = state  # re-insert -> newest position
        while len(sessions) > MAX_CURSOR_SESSIONS:
            sessions.pop(next(iter(sessions)))
        usage_ledger.write_cursor(root, {"sessions": sessions})
        if not new_entries or not usage_ledger.savings_display_enabled(root):
            return None
        turn_text = savings_model.format_compact(turn_tokens, turn_seconds)
        session_text = savings_model.format_compact(state["tokens"], state["seconds"])
        return {
            "systemMessage": (
                f"graphite: est. {turn_text} saved this turn "
                f"(session: {session_text}) [estimates]"
            )
        }
    except Exception:
        return None


def handle_pre_tool_use(payload: dict[str, Any], mode: str) -> dict[str, Any] | None:
    try:
        tool_name = payload.get("tool_name")
        if tool_name not in ("Grep", "Glob", "Bash", "PowerShell"):
            return None
        root = _payload_root(payload)
        if not (root / "graph-out" / "graph.json").is_file():
            return None
        denial: str | None = None
        if tool_name in ("Bash", "PowerShell"):
            # The matcher now routes EVERY Bash call here, so anything that is
            # not a repo-wide search must leave silently -- not even a
            # reminder. Nagging `git status` is how a real warning gets
            # ignored, which is the same rule check_hooks applies to repos
            # that never onboarded.
            tool_input = payload.get("tool_input")
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            found = _bash_search_pattern(command) if isinstance(command, str) else None
            if found is None:
                return None
            if mode == "strict":
                denial = _denial_for(root, found[0], found[1])
        elif mode == "strict" and tool_name == "Grep":
            denial = _strict_denial(payload, root)
        if denial is not None:
            health = persisted_resolution(
                root,
                on_error=lambda exc: record_incident(
                    repo_ledger_dir(root),
                    klass="build",
                    code="artifact_malformed",
                    subject=".graphite_analysis.json",
                    detail=str(exc),
                ),
            )
            if isinstance(health, dict) and health.get("healthy") is True:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": denial,
                    }
                }
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": PRE_TOOL_REMINDER + STRICT_SUSPENSION_NOTE,
                }
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": PRE_TOOL_REMINDER,
            }
        }
    except Exception:
        return None
