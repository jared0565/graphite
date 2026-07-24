"""Claude Code agent-hook handlers enforcing graphite-first on onboarded repos.

Every handler is fail-open by contract: any parse, IO, graph, or timing problem
yields ``None`` (no hook output) rather than an exception, so a hook bug can
never break a tool call or a session. The CLI wrapper adds a second catch-all.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from .config import Config
from .freshness import check_graph_freshness
from .graph_io import load_validated_graph_bundle

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
    _, graph = load_validated_graph_bundle(graph_path, root=root)
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
                "additionalContext": PRE_TOOL_REMINDER,
            }
        }
    except Exception:
        return None
