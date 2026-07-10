"""Repository ingestion: discover, classify, and hash files safely."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .cache import file_hash
from .config import Config


@dataclass(frozen=True)
class FileEntry:
    rel_path: str  # posix-style relative path
    abs_path: Path
    language: str | None
    size: int
    content_hash: str


# Extension → language classification.
LANGUAGE_BY_EXT: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".json": "json",
    ".md": "markdown",
    ".mdx": "markdown",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".css": "css",
    ".scss": "scss",
    ".html": "html",
    ".sql": "sql",
}

# Directories and file patterns to skip entirely.
SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", ".next", ".wrangler", ".open-next", "dist", "build",
    "out", "coverage", ".cache", ".claude", ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__", "graph-out", "graphify-out", "tools", "vendor", ".venv", "venv",
    "target",  # Rust/Maven build output
})

SKIP_SUFFIXES: frozenset[str] = frozenset({
    ".lock", ".log", ".min.js", ".min.css", ".map", ".svg", ".png", ".jpg",
    ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".mp4",
    ".webm", ".ogg", ".mp3", ".wav", ".pdf", ".zip", ".tar", ".gz",
})

# Binary file detection threshold: if a chunk contains this ratio of null bytes, treat as binary.
_BINARY_NULL_RATIO = 0.001


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
    except OSError:
        return True
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    return chunk.count(b"\x00") / len(chunk) > _BINARY_NULL_RATIO


def _classify_language(rel_path: str) -> str | None:
    lower = rel_path.lower()
    # package.json is json, tsconfig.json is json, etc.
    ext = Path(lower).suffix
    if ext in LANGUAGE_BY_EXT:
        return LANGUAGE_BY_EXT[ext]
    # Shebang detection for extensionless scripts.
    if ext == "":
        return None
    return None


def _should_skip(rel_path: str, cfg: Config) -> bool:
    parts = Path(rel_path).parts
    if any(part in SKIP_DIRS for part in parts):
        return True
    lower = rel_path.lower()
    if any(lower.endswith(s) for s in SKIP_SUFFIXES):
        return True
    if not cfg.include_dotfiles:
        if any(part.startswith(".") for part in parts):
            return True
    return False


def _git_ls_files(root: Path) -> list[str] | None:
    """Return tracked and untracked non-ignored files, or None if git is unavailable."""
    git_dir = root / ".git"
    if not git_dir.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=False,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    files = result.stdout.decode("utf-8", errors="ignore").split("\x00")
    return [f for f in files if f]


def _walk_files(root: Path, cfg: Config) -> Iterable[str]:
    """Fallback filesystem walk respecting SKIP_DIRS."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            full = Path(dirpath) / filename
            rel = full.relative_to(root).as_posix()
            if _should_skip(rel, cfg):
                continue
            yield rel


def collect_files(root: Path, cfg: Config) -> list[FileEntry]:
    """Collect all ingestible files under root."""
    root = root.resolve()
    tracked = _git_ls_files(root)
    if tracked is not None:
        rel_paths = [p for p in tracked if not _should_skip(p, cfg)]
    else:
        rel_paths = list(_walk_files(root, cfg))

    if cfg.max_files:
        rel_paths = rel_paths[: cfg.max_files]

    entries: list[FileEntry] = []
    for rel in rel_paths:
        abs_path = root / rel
        try:
            size = abs_path.stat().st_size
        except OSError:
            continue
        if size > cfg.max_file_size:
            continue
        if _is_binary(abs_path):
            continue
        entries.append(
            FileEntry(
                rel_path=rel,
                abs_path=abs_path,
                language=_classify_language(rel),
                size=size,
                content_hash=file_hash(abs_path),
            )
        )

    return sorted(entries, key=lambda e: e.rel_path)

