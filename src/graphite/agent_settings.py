"""Idempotent Claude Code settings wiring for graphite agent hooks.

Graphite owns exactly the hook commands whose command string starts with
``HOOK_COMMAND_PREFIX``; those are stripped and re-written on every install.
Everything else in ``.claude/settings.json`` is preserved untouched, and a file
that fails to parse is never modified.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .io import atomic_write_text

# `-P` omits the working directory from `sys.path` (3.11+). Without it,
# `python -m graphite` puts the CWD at `sys.path[0]`, so a `graphite.py` -- or a
# `graphite/` directory -- in a managed repo's root wins over the installed
# package and the hook executes it. The pre-tool-use hook fires on EVERY tool
# use, so such a file never has to be invoked by anyone: it runs on the next
# agent action after it lands. `.claude/settings.json` is tracked in consumer
# repos, so it would arrive through an ordinary reviewed commit, and `init`
# ships this identical command line to every managed repo.
#
# Reported by aramid-agent (channel round 49) and reproduced here: with a
# `graphite.py` in the CWD, bare `python -m graphite --version` ran the shadow
# file; `python -P -m graphite --version` ran graphite.
#
# `-P` over PYTHONSAFEPATH=1 because it is visible in the command string itself,
# which matters for a generated file an operator audits by reading.
HOOK_COMMAND_PREFIX = "python -P -m graphite agent-hook"
# Every prefix graphite has ever written. Load-bearing, not history: this string
# is both what we WRITE and what we MATCH to find our own hooks. A matcher that
# knew only the current spelling would fail to strip the legacy hook from an
# already-onboarded repo (leaving two, the vulnerable one still firing) and
# would make `existing_mode` return None, silently downgrading every strict repo
# to DEFAULT_MODE on its next `init`. Never drop an entry; only ever append.
_OWNED_COMMAND_PREFIXES = (HOOK_COMMAND_PREFIX, "python -m graphite agent-hook")
DEFAULT_MODE = "remind"
_MODES = ("remind", "strict")
_SESSION_START_COMMAND = f"{HOOK_COMMAND_PREFIX} session-start"
_STOP_COMMAND = f"{HOOK_COMMAND_PREFIX} stop"


def _pre_tool_use_command(mode: str) -> str:
    return f"{HOOK_COMMAND_PREFIX} pre-tool-use --mode {mode}"


def _is_graphite_command(hook: Any) -> bool:
    return isinstance(hook, dict) and str(hook.get("command", "")).startswith(_OWNED_COMMAND_PREFIXES)


def _strip_graphite(groups: Any) -> list[Any]:
    """Drop graphite-owned commands; keep foreign groups byte-identical."""
    if not isinstance(groups, list):
        return []
    kept: list[Any] = []
    for entry in groups:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            kept.append(entry)
            continue
        remaining = [hook for hook in entry["hooks"] if not _is_graphite_command(hook)]
        if remaining == entry["hooks"]:
            kept.append(entry)
        elif remaining:
            kept.append({**entry, "hooks": remaining})
    return kept


def _load_settings(path: Path) -> dict[str, Any] | None:
    """Parsed settings dict, {} when absent, None when malformed."""
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def existing_mode(root: Path) -> str | None:
    settings = _load_settings(root / ".claude" / "settings.json")
    if not settings:
        return None
    hooks = settings.get("hooks")
    groups = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    for entry in groups if isinstance(groups, list) else []:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else []:
            command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
            if command.startswith(_OWNED_COMMAND_PREFIXES) and "pre-tool-use" in command:
                return "strict" if "--mode strict" in command else "remind"
    return None


def ensure_claude_settings(root: Path, *, mode: str | None = None) -> dict[str, Any]:
    path = root / ".claude" / "settings.json"
    resolved = mode or existing_mode(root) or DEFAULT_MODE
    if resolved not in _MODES:
        raise ValueError(f"unknown agent-hook mode: {resolved}")
    existed = path.exists()
    original = _load_settings(path)
    if original is None:
        return {"path": str(path), "changed": False, "action": "malformed settings", "mode": resolved}

    hooks_value = original.get("hooks")
    if "hooks" in original and not isinstance(hooks_value, dict):
        # A non-dict "hooks" value is foreign malformed content we must not
        # silently overwrite with {} — treat it like unparseable JSON: no-op.
        return {"path": str(path), "changed": False, "action": "malformed settings", "mode": resolved}
    hooks = dict(hooks_value) if isinstance(hooks_value, dict) else {}
    desired = {
        "PreToolUse": {
            # The shells are matched because the previous matcher, "Grep|Glob",
            # named TOOLS rather than behaviour: `grep -rn ...` through the Bash
            # tool and `Select-String` through the PowerShell tool never reached
            # the hook at all, and consumer agents reported taking exactly those
            # routes for cross-file work. Enforcing on the Grep tool alone
            # enforces a naming convention, not a rule.
            #
            # `agent_hooks.handle_pre_tool_use` returns None for every shell
            # command that is not a repo-wide search, so matching the shells
            # here costs one fail-open hook invocation per command and never
            # interferes with ordinary work.
            "matcher": "Grep|Glob|Bash|PowerShell",
            "hooks": [{"type": "command", "command": _pre_tool_use_command(resolved)}],
        },
        "SessionStart": {
            "hooks": [{"type": "command", "command": _SESSION_START_COMMAND}],
        },
        "Stop": {
            "hooks": [{"type": "command", "command": _STOP_COMMAND}],
        },
    }
    changed = False
    for event, entry in desired.items():
        groups = _strip_graphite(hooks.get(event))
        groups.append(entry)
        if hooks.get(event) != groups:
            changed = True
        hooks[event] = groups

    if not changed:
        return {"path": str(path), "changed": False, "action": "already current", "mode": resolved}
    updated = {**original, "hooks": hooks}
    atomic_write_text(path, json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
    action = "updated" if existed else "created"
    return {"path": str(path), "changed": True, "action": action, "mode": resolved}
