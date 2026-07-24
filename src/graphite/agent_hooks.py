"""Claude Code agent-hook handlers enforcing graphite-first on onboarded repos.

Every handler is fail-open by contract: any parse, IO, graph, or timing problem
yields ``None`` (no hook output) rather than an exception, so a hook bug can
never break a tool call or a session. The CLI wrapper adds a second catch-all.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .config import Config
from .freshness import check_graph_freshness

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


def handle_pre_tool_use(payload: dict[str, Any], mode: str) -> dict[str, Any] | None:
    try:
        if payload.get("tool_name") not in ("Grep", "Glob"):
            return None
        root = _payload_root(payload)
        if not (root / "graph-out" / "graph.json").is_file():
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": PRE_TOOL_REMINDER,
            }
        }
    except Exception:
        return None
