"""Interactive project initialization for Graphite-aware AI coding agents."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, TextIO

from .bootstrap import ensure_gitignore, daemon_visibility
from .io import atomic_write_text

GRAPHITE_DOC_HEADER = "# Graphite Development Context"
GRAPHITE_REQUIRED_WORKFLOW = "## Required Workflow"
GRAPHITE_DOC = """# Graphite Development Context

Graphite is the shared local code graph for this project. Codex, Claude Code, Gemini CLI, Antigravity, Visual Studio, and other coding agents should use the same graph instead of rebuilding separate mental maps.

All commands below use `python -m graphite`, which works in every shell and for every agent as long as the Python environment has Graphite installed. A bare `graphite` command is equivalent where the console script is on PATH.

## Required Workflow

Before non-trivial code changes:

1. Run `python -m graphite check .`
2. Run `python -m graphite context <target-file>` before editing important files.
3. Run `python -m graphite impact <target-file>` before changing shared logic, APIs, data flow, auth, persistence, deployment behavior, or other high-risk paths.
4. Use `python -m graphite query "stats"` when project structure is unclear.

After edits:

1. Run `python -m graphite build .` (skip if a Graphite daemon/watcher keeps this repo fresh; verify with `python -m graphite check .`)
2. Run relevant tests, typechecks, or validation commands.
3. Do not edit `graph-out/` manually.

## Optional LLM Enrichment

Graphite is zero-LLM by default. Use LLM enrichment only when a human explicitly wants AI-generated graph summaries in `graph-out/GRAPH_REPORT.md`.

Local examples:

```bash
python -m graphite --llm local --llm-provider ollama --llm-model qwen2.5-coder report .
python -m graphite --llm local --llm-provider lmstudio --llm-model local-model report .
```

Cloud or remote OpenAI-compatible example:

```bash
set GRAPHITE_LLM_API_KEY=<provider-key>
python -m graphite --llm cloud --llm-provider openai-compatible --llm-base-url https://example.com/v1 --llm-model my-model report .
```

Intelligent auto mode:

```bash
python -m graphite --llm auto --llm-provider openrouter report .
```

Auto mode keeps builds zero-LLM for small/simple graphs, skips cloud calls when credentials are missing, and uses LLM enrichment only when graph complexity/risk signals justify the extra cost. For OpenRouter, it defaults to `moonshotai/kimi-k2.7-code` when `--llm-model` is omitted.
OpenRouter examples:

```bash
set GRAPHITE_LLM_API_KEY=<openrouter-api-key>
python -m graphite --llm cloud --llm-provider openrouter report .
python -m graphite --llm cloud --llm-provider openrouter --llm-model "moonshotai/kimi-k2.7-code" report .
python -m graphite --llm cloud --llm-provider openrouter --llm-model "~openai/gpt-latest" report .
```

Graphite automatically uses OpenRouter's OpenAI-compatible base URL. To use a specific OpenRouter model, replace `--llm-model` with a model slug from the OpenRouter model catalog.
Rules:

- Prefer local LLMs for sensitive or private codebases.
- Do not put API keys in committed files or shell history; prefer `GRAPHITE_LLM_API_KEY`.
- Keep daemon/watch builds zero-LLM unless explicitly requested.
- LLM enrichment sends bounded graph metadata and analysis summaries, not raw source code, but still treat external providers as third-party data processors.

## Operating Rules

- Treat Graphite as a project map, not as proof of correctness.
- Always read the source files and tests that Graphite identifies before changing behavior.
- If `python -m graphite check .` reports stale output, rebuild before relying on context or impact data.
- Graphite runs locally and should not use LLM or network calls unless explicitly configured.
- For TypeScript resolver issues, use `python -m graphite --typescript-resolver disabled build .` only as a fallback.
"""

SHARED_POINTER_HEADER = "## Shared Graphite Instructions"
SHARED_POINTER = """## Shared Graphite Instructions

Follow `GRAPHITE.md` before making non-trivial code changes. Use the existing `graph-out/graph.json` as the shared project graph, and do not edit `graph-out/` manually.
"""

CURSOR_POINTER = """---
description: Use Graphite project context before non-trivial code changes
alwaysApply: true
---

# Graphite Instructions

Follow `GRAPHITE.md` before making non-trivial code changes. Use the existing `graph-out/graph.json` as the shared project graph, and do not edit `graph-out/` manually.
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_root": str(self.project_root),
            "platforms": list(self.platforms),
            "graphite_doc": self.graphite_doc,
            "gitignore": self.gitignore,
            "platform_files": self.platform_files,
            "allowlist": self.allowlist,
            "daemon": self.daemon,
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


def init_project(project_root: Path, *, platforms: Iterable[str], daemon_base: Path | None = None) -> InitResult:
    root = project_root.resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    selected = resolve_platform_selection(platforms)
    graphite_doc = ensure_graphite_doc(root / "GRAPHITE.md")
    gitignore = ensure_gitignore(root / ".gitignore")
    platform_files: list[dict[str, Any]] = []
    instruction_paths = [Path("GRAPHITE.md")]

    for key in selected:
        spec = PLATFORMS[key]
        for rel in spec.files:
            rel_path = Path(rel)
            platform_files.append(ensure_platform_file(root / rel_path, spec=spec))
            instruction_paths.append(rel_path)

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
    )


def ensure_graphite_doc(path: Path) -> dict[str, Any]:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if GRAPHITE_DOC_HEADER in original and GRAPHITE_REQUIRED_WORKFLOW in original:
        return {"path": str(path), "changed": False, "action": "already current"}

    if not original.strip():
        new_text = GRAPHITE_DOC
    else:
        new_text = _append_section(original, GRAPHITE_DOC)
    atomic_write_text(path, new_text)
    return {"path": str(path), "changed": True, "action": "created" if not original else "updated"}


def ensure_platform_file(path: Path, *, spec: PlatformSpec) -> dict[str, Any]:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    marker_present = "GRAPHITE.md" in original and "graph-out/graph.json" in original
    if marker_present:
        return {"platform": spec.key, "path": str(path), "changed": False, "action": "already current"}

    if not original.strip():
        heading = f"# {spec.label} Project Instructions\n\n"
        new_text = heading + spec.content
    else:
        new_text = _append_section(original, spec.content)
    atomic_write_text(path, new_text)
    return {"platform": spec.key, "path": str(path), "changed": True, "action": "created" if not original else "updated"}


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
    patterns: list[str] = []
    if len(parts) > 1:
        current: list[str] = []
        for directory in parts[:-1]:
            current.append(directory)
            patterns.append("!/" + "/".join(current) + "/")
    patterns.append("!/" + "/".join(parts))
    return patterns
