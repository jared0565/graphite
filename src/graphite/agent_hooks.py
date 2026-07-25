"""Claude Code agent-hook handlers enforcing graphite-first on onboarded repos.

Every handler is fail-open by contract: any parse, IO, graph, or timing problem
yields ``None`` (no hook output) rather than an exception, so a hook bug can
never break a tool call or a session. The CLI wrapper adds a second catch-all.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from . import savings as savings_model
from . import usage_ledger
from .config import Config
from .freshness import check_graph_freshness
from .graph_io import load_validated_graph_bundle
from .health import persisted_resolution

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


def _freshness_within_budget(root: Path) -> str | None:
    """Return 'fresh' | 'stale' | None (unknown), never raising, within budget."""
    outcome: list[str | None] = [None]

    def _worker() -> None:
        try:
            cfg = Config(output_dir=root / "graph-out", cache_dir=root / ".cache" / "graphite")
            status = check_graph_freshness(root, cfg)
            outcome[0] = "stale" if status.get("stale") else "fresh"
        except Exception:
            outcome[0] = None

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    worker.join(_FRESHNESS_BUDGET_SECONDS)
    return outcome[0]


def handle_session_start(payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        root = _payload_root(payload)
        if (root / "graph-out" / "graph.json").is_file():
            freshness = _freshness_within_budget(root)
            if freshness == "stale":
                status = "Graph status: STALE - run `python -m graphite build .` before relying on graph answers."
            elif freshness == "fresh":
                status = "Graph status: fresh."
            else:
                status = "Graph status: unknown - verify with `python -m graphite check .`."
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


def _strict_denial(payload: dict[str, Any], root: Path) -> str | None:
    try:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return None
        pattern = tool_input.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return None
        target = tool_input.get("path")
        if isinstance(target, str) and target:
            candidate = Path(target)
            if not candidate.is_absolute():
                candidate = root / candidate
            if candidate.is_file():
                return None  # single-file scoped searches are always allowed
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
        if payload.get("tool_name") not in ("Grep", "Glob"):
            return None
        root = _payload_root(payload)
        if not (root / "graph-out" / "graph.json").is_file():
            return None
        if mode == "strict" and payload.get("tool_name") == "Grep":
            denial = _strict_denial(payload, root)
            if denial is not None:
                health = persisted_resolution(root)
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
