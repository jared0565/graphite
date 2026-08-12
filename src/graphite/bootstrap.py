"""Project bootstrap helpers for Graphite-aware development workflows."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .io import atomic_write_text

GRAPHITE_GITIGNORE_MARKER = "# Graphite"
# `**/.cache/graphite/` instead of `.cache/graphite/`: a gitignore pattern with
# an internal slash is anchored to the gitignore's directory, so the anchored
# form misses caches created by manual builds inside monorepo workspaces.
GRAPHITE_GITIGNORE_LINES: tuple[str, ...] = (
    "# Graphite",
    "graph-out/",
    "**/.cache/graphite/",
    "**/.graphite/",
    ".graphite-daemon/",
    # Hook trampolines are MACHINE-LOCAL, not repository content: the shim
    # `hookshim.render_trigger_shim` writes embeds an absolute interpreter path
    # (`INTERP="/c/Python314/python.exe"` on the machine that ran `init`).
    # Committing one bakes another machine's Python location into the repo, and
    # every `init` on a different box produces a spurious diff.
    #
    # They are distributed per machine instead -- `graphite hooks template`
    # (a16b00f) installs a git template so fresh clones get hooks without the
    # repo carrying them. Evidence this was always the intent: no repo on this
    # machine has ever tracked a file under `.githooks/`.
    #
    # Ignored rather than left merely untracked so the intent is stated. On
    # 2026-07-31 `doctor` briefly reported these as "generated but never
    # committed" and `init` allowlisted them out of a default-deny gitignore --
    # advice that would have committed a machine-specific path.
    ".githooks/",
)

GRAPHITE_AGENTS_HEADER = "## Automatic Graphite Consult"
GRAPHITE_AGENTS_SECTION = """## Automatic Graphite Consult

For any non-trivial code change, run Graphite before broad file reads or edits:

```bash
python -m graphite check .
python -m graphite context <likely-changed-file>
python -m graphite impact <likely-changed-file>
python -m graphite query "stats"
```

Use `python -m graphite context` first when the likely target file is known. Treat its output as a dependency map, not as proof of correctness: still read the relevant source files and tests before editing. If `python -m graphite check .` reports stale output, rebuild before relying on context or impact data.

If the graph is missing or stale, rebuild it from the repository root with:

```bash
python -m graphite -v build .
```

During active multi-file development, keep the graph current with:

```bash
python -m graphite watch . --impact
```

Graphite is a centrally installed Python package (importable from any repository). Canonical graph commands run locally, never read provider credentials, and never use LLM or network inference; model output belongs only in an explicit non-authoritative overlay. `python -m graphite` works in every shell; a bare `graphite` command is equivalent where the console script is on PATH.

For TypeScript, Graphite uses the local TypeScript compiler resolver automatically when available. If a project has a broken TypeScript setup, fall back with `python -m graphite --typescript-resolver disabled build .`.
"""


@dataclass(frozen=True)
class BootstrapResult:
    project_root: Path
    gitignore: dict[str, Any]
    agents: dict[str, Any]
    daemon: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_root": str(self.project_root),
            "gitignore": self.gitignore,
            "agents": self.agents,
            "daemon": self.daemon,
        }


def bootstrap_project(project_root: Path, *, daemon_base: Path | None = None) -> BootstrapResult:
    """Make a project Graphite-ready without disturbing unrelated user content."""
    root = project_root.resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    gitignore = ensure_gitignore(root / ".gitignore")
    agents = ensure_agents(root / "AGENTS.md", root.name)
    daemon = daemon_visibility(root, daemon_base=daemon_base)
    return BootstrapResult(project_root=root, gitignore=gitignore, agents=agents, daemon=daemon)


def ensure_gitignore(path: Path) -> dict[str, Any]:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = original.splitlines()
    existing = {line.strip() for line in lines}
    has_graphite_header = any(line.strip().lower().startswith("# graphite") for line in lines)
    missing: list[str] = []
    for line in GRAPHITE_GITIGNORE_LINES:
        stripped = line.strip()
        if stripped == GRAPHITE_GITIGNORE_MARKER and has_graphite_header:
            continue
        if stripped not in existing:
            missing.append(line)
    changed = bool(missing)
    if changed:
        new_text = original
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        if new_text and not new_text.endswith("\n\n"):
            new_text += "\n"
        new_text += "\n".join(missing) + "\n"
        atomic_write_text(path, new_text)
    return {
        "path": str(path),
        "changed": changed,
        "added": missing,
    }


def ensure_agents(path: Path, project_name: str) -> dict[str, Any]:
    if path.exists():
        original = path.read_text(encoding="utf-8")
    else:
        original = f"# {project_name} Agent Notes\n\n"
    changed = GRAPHITE_AGENTS_HEADER not in original
    if changed:
        new_text = original
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        if new_text and not new_text.endswith("\n\n"):
            new_text += "\n"
        new_text += GRAPHITE_AGENTS_SECTION
        if not new_text.endswith("\n"):
            new_text += "\n"
        atomic_write_text(path, new_text)
    return {
        "path": str(path),
        "changed": changed,
        "section": GRAPHITE_AGENTS_HEADER,
    }


#: The directory the daemon writes its state into. Named once because
#: `_default_daemon_base` now FINDS the base by looking for it, so a drifted
#: literal there would silently stop discovering the very thing it points at.
DAEMON_STATE_DIRNAME = ".graphite-daemon"


def daemon_visibility(project_root: Path, *, daemon_base: Path | None = None) -> dict[str, Any]:
    base = (daemon_base or _default_daemon_base(project_root)).resolve()
    status_path = base / DAEMON_STATE_DIRNAME / "status.json"
    payload: dict[str, Any] = {
        "base": str(base),
        "status_path": str(status_path),
        "status_found": status_path.exists(),
        "project_listed": False,
    }
    if not status_path.exists():
        return payload
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        payload["error"] = str(exc)
        return payload
    project = str(project_root.resolve()).lower()
    projects = data.get("projects", [])
    for item in projects:
        if str(item.get("root", "")).lower() == project:
            payload["project_listed"] = True
            payload["project_status"] = {
                "build_count": item.get("build_count"),
                "failure_count": item.get("failure_count"),
                "last_error": item.get("last_error"),
                "file_count": item.get("file_count"),
            }
            break
    payload["daemon_status"] = data.get("status")
    payload["project_count"] = data.get("project_count")
    return payload


def _default_daemon_base(project_root: Path) -> Path:
    """Find the directory the daemon writes its state into.

    Discovered by the marker rather than by name. From the first commit onward
    this compared each parent against one hardcoded absolute path -- the
    maintainer's own layout compiled into shipped code -- so discovery worked on
    exactly one machine, and the wheel carried an absolute developer path, which
    RELEASING.md forbids in a release archive. The value is only ever used to
    reach `base/DAEMON_STATE_DIRNAME/status.json`, so look for that. Do not name
    a concrete path here: this docstring ships inside the wheel.
    """
    env = os.environ.get("GRAPHITE_PROJECTS_ROOT")
    if env:
        return Path(env)
    resolved = project_root.resolve()
    for parent in (resolved, *resolved.parents):
        if (parent / DAEMON_STATE_DIRNAME).is_dir():
            return parent
    return resolved
