"""Interactive project initialization for Graphite-aware AI coding agents."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, TextIO

from .agent_settings import ensure_claude_settings
from .bootstrap import ensure_gitignore, daemon_visibility
from .io import atomic_write_text

# Bump whenever GRAPHITE_DOC, SHARED_POINTER, or CURSOR_POINTER changes;
# test_template_change_requires_doc_version_bump pins the pairing. Files
# written before versioning existed count as version 1 ("legacy unversioned")
# and are never rewritten automatically.
DOC_VERSION = 9

MANAGED_BEGIN = f"<!-- graphite:managed version={DOC_VERSION} -->"
MANAGED_END = "<!-- graphite:managed-end -->"
_MANAGED_BEGIN_RE = re.compile(r"<!-- graphite:managed version=(\d{1,9}) -->")

GRAPHITE_DOC_HEADER = "# Graphite Development Context"
GRAPHITE_REQUIRED_WORKFLOW = "## Required Workflow"
GRAPHITE_DOC = """# Graphite Development Context

Graphite is the shared local code graph for this project. Codex, Claude Code, Gemini CLI, Antigravity, Visual Studio, and other coding agents should use the same graph instead of rebuilding separate mental maps.

All commands below use `python -m graphite`, which works in every shell and for every agent as long as the Python environment has Graphite installed. A bare `graphite` command is equivalent where the console script is on PATH.

## Required Workflow

Graphite-first is required, not advisory. Before any cross-file exploration, consult the graph first. Manual search (grep, glob, directory walking) is the fallback, not the default: use it for literal text and filename lookups, or after a Graphite answer proved insufficient — and say so when you fall back.

| Question shape | Run first |
| --- | --- |
| Who calls / reads / imports this symbol? | `python -m graphite query "callers <symbol>"` |
| What does this symbol call? | `python -m graphite query "calls <symbol>"` |
| Where is this symbol defined? | `python -m graphite search "<symbol>"` |
| What breaks if this file changes? | `python -m graphite impact <file>` |
| What surrounds this file (callers, tests, neighbors)? | `python -m graphite context <file>` |
| How is the project structured? | `python -m graphite query "stats"` |
| Literal string or filename lookup | grep/glob — Graphite not required |

Before non-trivial code changes:

1. Run `python -m graphite check .`
2. Run `python -m graphite context <target-file>` before editing important files.
3. Run `python -m graphite impact <target-file>` before changing shared logic, APIs, data flow, auth, persistence, deployment behavior, or other high-risk paths.
4. Use `python -m graphite search "<symbol, path, or concept>"` to locate nodes; use `python -m graphite query "stats"` when project structure is unclear.
5. Discover supported commands, query verbs, and limits with `python -m graphite capabilities --json` — do not guess query verbs. `query` takes structured verbs; `query --natural "<question>"` accepts only the fixed deterministic grammar listed by capabilities (no inference — unmatched questions fall back to ranked search).
6. Graph answers carry an `answer` block: `grade: decision_grade` means this answer's own relations/languages are healthy (an empty result is a trustworthy absence, subject to `caveats`); `advisory` means verify with grep and say so; `inconclusive` (also the legacy `"inconclusive": true`) means unknown, not safe. Check `known limits`/`caveats` before trusting empties.
7. `python -m graphite incidents list` shows recorded failures (build errors, malformed artifacts, inconclusive queries). Check it when a graph answer looks wrong; recurring incidents belong in a governed round.

After edits:

1. Run `python -m graphite build .` (skip if a Graphite daemon/watcher keeps this repo fresh; verify with `python -m graphite check .`)
2. Run relevant tests, typechecks, or validation commands.
3. Do not edit `graph-out/` manually.

## Canonical Graph Isolation

`scan`, `build`, `report`, `check`, `validate`, `query`, `context`, `impact`,
`watch`, and `daemon` are inference-free canonical operations. They do not read
provider credentials, ignore ambient `GRAPHITE_LLM*` configuration, and reject
legacy non-`none` LLM flags. Model-generated annotations belong only in the
explicit, non-authoritative overlay boundary and must never replace or modify
canonical `graph-out` artifacts.

## Operating Rules

- Treat Graphite as a project map, not as proof of correctness.
- Always read the source files and tests that Graphite identifies before changing behavior.
- Graphite-first: prefer graph commands over manual cross-file search; fall back only when the graph answer is insufficient, and say so.
- If `python -m graphite check .` reports stale output, rebuild before relying on context or impact data.
- Canonical Graphite operations run locally and never use LLM or network inference.
- For TypeScript resolver issues, use `python -m graphite --typescript-resolver disabled build .` only as a fallback.
"""

SHARED_POINTER_HEADER = "## Shared Graphite Instructions"
SHARED_POINTER = """## Shared Graphite Instructions

Graphite-first is required in this repo. Follow `GRAPHITE.md` before making non-trivial code changes: for cross-file questions (who-calls, where-defined, impact, data flow, structure) run the Graphite commands first; grep/glob are for literal text and filename lookups only. Fall back to manual search only after a Graphite answer proved insufficient, and say so. Use the existing `graph-out/graph.json` as the shared project graph, and do not edit `graph-out/` manually.
"""

CURSOR_POINTER = """---
description: Graphite-first project context is required before non-trivial code changes
alwaysApply: true
---

# Graphite Instructions

Graphite-first is required in this repo. Follow `GRAPHITE.md` before making non-trivial code changes: for cross-file questions (who-calls, where-defined, impact, data flow, structure) run the Graphite commands first; grep/glob are for literal text and filename lookups only. Fall back to manual search only after a Graphite answer proved insufficient, and say so. Use the existing `graph-out/graph.json` as the shared project graph, and do not edit `graph-out/` manually.
"""

PLATFORM_ORDER: tuple[str, ...] = (
    "codex",
    "claude",
    "gemini",
    "antigravity",
    "visual-studio",
    "cursor",
    "windsurf",
)

DEFAULT_PLATFORMS: tuple[str, ...] = (
    "codex",
    "claude",
    "antigravity",
    "visual-studio",
)


@dataclass(frozen=True)
class PlatformSpec:
    key: str
    label: str
    files: tuple[str, ...]
    content: str = SHARED_POINTER


PLATFORMS: dict[str, PlatformSpec] = {
    "codex": PlatformSpec("codex", "Codex CLI / Codex Desktop", ("AGENTS.md",)),
    "claude": PlatformSpec("claude", "Claude Code", ("CLAUDE.md",)),
    "gemini": PlatformSpec("gemini", "Gemini CLI", ("GEMINI.md",)),
    "antigravity": PlatformSpec("antigravity", "Antigravity IDE", ("ANTIGRAVITY.md",)),
    "visual-studio": PlatformSpec("visual-studio", "Visual Studio / GitHub Copilot", (".github/copilot-instructions.md",)),
    "cursor": PlatformSpec("cursor", "Cursor", (".cursor/rules/graphite.mdc",), CURSOR_POINTER),
    "windsurf": PlatformSpec("windsurf", "Windsurf", (".windsurfrules",)),
}

ALIASES: dict[str, str] = {
    "agent": "codex",
    "agents": "codex",
    "codex-cli": "codex",
    "codex-desktop": "codex",
    "claude-code": "claude",
    "gemini-cli": "gemini",
    "google-gemini": "gemini",
    "google-antigravity": "antigravity",
    "vs": "visual-studio",
    "vscode": "visual-studio",
    "visualstudio": "visual-studio",
    "visual-studio-code": "visual-studio",
    "copilot": "visual-studio",
    "github-copilot": "visual-studio",
}


@dataclass(frozen=True)
class InitResult:
    project_root: Path
    platforms: tuple[str, ...]
    graphite_doc: dict[str, Any]
    gitignore: dict[str, Any]
    platform_files: list[dict[str, Any]]
    allowlist: dict[str, Any]
    daemon: dict[str, Any]
    agent_hooks: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_root": str(self.project_root),
            "platforms": list(self.platforms),
            "graphite_doc": self.graphite_doc,
            "gitignore": self.gitignore,
            "platform_files": self.platform_files,
            "allowlist": self.allowlist,
            "daemon": self.daemon,
            "agent_hooks": self.agent_hooks,
        }


def platform_choices() -> list[dict[str, str]]:
    return [{"key": key, "label": PLATFORMS[key].label} for key in PLATFORM_ORDER]


def resolve_platform_selection(
    requested: Iterable[str] | None,
    *,
    interactive: bool = False,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> tuple[str, ...]:
    tokens = [token for value in requested or [] for token in _split_platform_tokens(value)]
    if not tokens and interactive:
        tokens = list(_prompt_for_platforms(stdin=stdin, stdout=stdout))
    if not tokens:
        tokens = list(DEFAULT_PLATFORMS)

    resolved: list[str] = []
    for token in tokens:
        normalized = _normalize_platform(token)
        if normalized == "all":
            for key in PLATFORM_ORDER:
                if key not in resolved:
                    resolved.append(key)
            continue
        if normalized not in PLATFORMS:
            valid = ", ".join([*PLATFORM_ORDER, "all"])
            raise ValueError(f"unknown platform '{token}'. Valid platforms: {valid}")
        if normalized not in resolved:
            resolved.append(normalized)
    return tuple(resolved)


def init_project(
    project_root: Path,
    *,
    platforms: Iterable[str],
    daemon_base: Path | None = None,
    agent_hooks_mode: str | None = None,
    install_agent_hooks: bool = True,
    adopt: bool = False,
) -> InitResult:
    root = project_root.resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    selected = resolve_platform_selection(platforms)
    graphite_doc = ensure_graphite_doc(root / "GRAPHITE.md", adopt=adopt)
    gitignore = ensure_gitignore(root / ".gitignore")
    platform_files: list[dict[str, Any]] = []
    instruction_paths = [Path("GRAPHITE.md")]

    for key in selected:
        spec = PLATFORMS[key]
        for rel in spec.files:
            rel_path = Path(rel)
            platform_files.append(ensure_platform_file(root / rel_path, spec=spec, adopt=adopt))
            instruction_paths.append(rel_path)

    if install_agent_hooks:
        agent_hooks = ensure_claude_settings(root, mode=agent_hooks_mode)
        instruction_paths.append(Path(".claude/settings.json"))
    else:
        agent_hooks = {
            "path": str(root / ".claude" / "settings.json"),
            "changed": False,
            "action": "skipped",
            "mode": None,
        }

    allowlist = ensure_gitignore_allowlist(root / ".gitignore", instruction_paths)
    daemon = daemon_visibility(root, daemon_base=daemon_base)
    return InitResult(
        project_root=root,
        platforms=selected,
        graphite_doc=graphite_doc,
        gitignore=gitignore,
        platform_files=platform_files,
        allowlist=allowlist,
        daemon=daemon,
        agent_hooks=agent_hooks,
    )


def _managed_block(body: str) -> str:
    if not body.endswith("\n"):
        body += "\n"
    return f"{MANAGED_BEGIN}\n{body}{MANAGED_END}"


def _ensure_managed_text(
    original: str, body: str, *, is_legacy: bool, heading: str = "", adopt: bool = False
) -> tuple[str | None, str]:
    """Return (new_text_or_None, action) for a versioned managed region."""
    begin = _MANAGED_BEGIN_RE.search(original)
    if begin is not None:
        end_index = original.find(MANAGED_END, begin.end())
        if end_index < 0:
            return None, "managed markers damaged"
        version = int(begin.group(1))
        if version == DOC_VERSION:
            return None, "already current"
        if version > DOC_VERSION:
            return None, "newer than tool"
        replaced = (
            original[: begin.start()]
            + _managed_block(body)
            + original[end_index + len(MANAGED_END):]
        )
        return replaced, "refreshed"

    if is_legacy:
        if not adopt:
            # Pre-versioning content, possibly hand-curated; never rewrite it
            # automatically. Reported so the operator can reconcile and opt in.
            return None, "legacy unversioned"
        # --adopt is that opt-in. It APPENDS the managed block and leaves the
        # legacy text untouched above it, so hand-curated owner policy survives
        # (#13). Nothing is overwritten: this is the same non-destructive path
        # an unmarked non-legacy doc already takes, and once the markers exist
        # later runs refresh normally.
        return _append_section(original, _managed_block(body)), "adopted"

    if not original.strip():
        return heading + _managed_block(body) + "\n", "created"
    return _append_section(original, _managed_block(body)), "updated"


def ensure_graphite_doc(path: Path, *, adopt: bool = False) -> dict[str, Any]:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    new_text, action = _ensure_managed_text(
        original,
        GRAPHITE_DOC,
        is_legacy=GRAPHITE_DOC_HEADER in original and GRAPHITE_REQUIRED_WORKFLOW in original,
        adopt=adopt,
    )
    if new_text is not None:
        atomic_write_text(path, new_text)
    return {"path": str(path), "changed": new_text is not None, "action": action}


def ensure_platform_file(path: Path, *, spec: PlatformSpec, adopt: bool = False) -> dict[str, Any]:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    new_text, action = _ensure_managed_text(
        original,
        spec.content,
        is_legacy="GRAPHITE.md" in original and "graph-out/graph.json" in original,
        heading=f"# {spec.label} Project Instructions\n\n",
        adopt=adopt,
    )
    if new_text is not None:
        atomic_write_text(path, new_text)
    return {"platform": spec.key, "path": str(path), "changed": new_text is not None, "action": action}


def ensure_gitignore_allowlist(path: Path, rel_paths: Iterable[Path]) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "changed": False, "added": [], "reason": "missing gitignore"}
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    default_deny = any(line.strip() == "/*" for line in lines)
    if not default_deny:
        return {"path": str(path), "changed": False, "added": [], "reason": "not default-deny"}

    existing = {line.strip() for line in lines}
    added: list[str] = []
    for rel in rel_paths:
        for pattern in _allowlist_patterns(rel):
            if pattern not in existing and pattern not in added:
                added.append(pattern)

    if added:
        new_text = original
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        if new_text and not new_text.endswith("\n\n"):
            new_text += "\n"
        new_text += "\n".join(added) + "\n"
        atomic_write_text(path, new_text)
    return {"path": str(path), "changed": bool(added), "added": added, "reason": "default-deny"}


def _prompt_for_platforms(*, stdin: TextIO | None, stdout: TextIO | None) -> tuple[str, ...]:
    stdin = stdin or __import__("sys").stdin
    stdout = stdout or __import__("sys").stdout
    print("Select AI platforms to configure for Graphite:", file=stdout)
    for index, key in enumerate(PLATFORM_ORDER, start=1):
        print(f"  {index}. {PLATFORMS[key].label} [{key}]", file=stdout)
    default = ", ".join(DEFAULT_PLATFORMS)
    print("  all. All supported platforms", file=stdout)
    print(f"Enter numbers/names separated by commas, or press Enter for: {default}", file=stdout)
    stdout.flush()
    answer = stdin.readline().strip()
    if not answer:
        return DEFAULT_PLATFORMS
    return tuple(_split_platform_tokens(answer))


def _split_platform_tokens(value: str) -> list[str]:
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


def _normalize_platform(value: str) -> str:
    token = value.strip().lower().replace("_", "-")
    if token.isdigit():
        index = int(token) - 1
        if 0 <= index < len(PLATFORM_ORDER):
            return PLATFORM_ORDER[index]
    return ALIASES.get(token, token)


def _append_section(original: str, section: str) -> str:
    new_text = original
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    if new_text and not new_text.endswith("\n\n"):
        new_text += "\n"
    new_text += section
    if not new_text.endswith("\n"):
        new_text += "\n"
    return new_text


def _allowlist_patterns(rel: Path) -> list[str]:
    rel = Path(*[part for part in rel.parts if part not in ("", ".")])
    parts = rel.parts
    if not parts:
        return []
    if parts[0] == ".claude":
        # .claude/ can hold settings.local.json (machine-local permissions,
        # possibly secrets). A bare "!/.claude/" un-ignore in a default-deny
        # gitignore would expose everything under the directory, not just
        # settings.json. Sandwich: un-ignore the dir, re-ignore its contents,
        # then un-ignore only the committed settings file.
        return ["!/.claude/", "/.claude/*", "!/.claude/settings.json"]
    patterns: list[str] = []
    if len(parts) > 1:
        current: list[str] = []
        for directory in parts[:-1]:
            current.append(directory)
            patterns.append("!/" + "/".join(current) + "/")
    patterns.append("!/" + "/".join(parts))
    return patterns
