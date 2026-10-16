"""Repository ingestion: discover, classify, and hash files safely."""
from __future__ import annotations

import os
from itertools import islice
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from . import ownership
from .cache import file_hash
from .config import Config
from .git import GitError, GitRunner
from .hook_entry import MARKER as _SETUP_MARKER


MAX_GIT_FILE_RECORDS = 100_000


class IngestError(RuntimeError):
    """Raised when repository files cannot be enumerated safely."""


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


def _has_component_prefix(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    if len(parts) < len(prefix):
        return False
    return all(
        os.path.normcase(part) == os.path.normcase(expected)
        for part, expected in zip(parts, prefix)
    )


def _dynamic_exclusions(root: Path, cfg: Config) -> tuple[tuple[str, ...], ...]:
    exclusions: set[tuple[str, ...]] = set()
    for configured in (cfg.output_dir, cfg.cache_dir):
        try:
            relative = configured.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        if relative != Path("."):
            exclusions.add(relative.parts)
    return tuple(sorted(exclusions))


def _should_skip(
    rel_path: str,
    cfg: Config,
    dynamic_exclusions: tuple[tuple[str, ...], ...] = (),
) -> bool:
    parts = Path(rel_path).parts
    if any(part in SKIP_DIRS for part in parts):
        return True
    if any(_has_component_prefix(parts, prefix) for prefix in dynamic_exclusions):
        return True
    lower = rel_path.lower()
    if any(lower.endswith(s) for s in SKIP_SUFFIXES):
        return True
    if not cfg.include_dotfiles:
        if any(part.startswith(".") for part in parts):
            return True
    return False


def _resolve_contained_path(root: Path, candidate: Path) -> Path | None:
    """Resolve *candidate* and return it only when strictly below *root*."""
    try:
        resolved = candidate.resolve()
        relative = resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if relative == Path("."):
        return None
    return resolved


def _normalize_git_path(root: Path, value: str) -> str | None:
    posix_path = PurePosixPath(value)
    if not value or value == "." or posix_path.is_absolute() or ".." in posix_path.parts:
        return None
    native_path = Path(value)
    if native_path.is_absolute():
        return None
    resolved = _resolve_contained_path(root, root / native_path)
    if resolved is None:
        return None
    return native_path.as_posix()


def enclosing_git_root(path: Path) -> Path | None:
    """The nearest directory, from `path` upward, that holds `.git`.

    `.git` is a directory in a clone and a file in a linked worktree or a
    submodule, so it is tested for existence only. None means `path` is in no
    repository at all.
    """
    for directory in (path, *path.parents):
        if os.path.lexists(directory / ".git"):
            return directory
    return None


def _is_set_up(top: Path) -> bool:
    """Whether graphite is set up at `top`: it has a graph, or the
    `GRAPHITE.md` that `init` commits."""
    return (top / "graph-out" / "graph.json").is_file() or (top / _SETUP_MARKER).is_file()


def _resolved(path: Path) -> Path:
    """Where `path` leads, every link on the way followed."""
    return Path(os.path.realpath(path))


def trusted_root(top: Path) -> bool:
    """Whether `top`, a directory holding `.git`, may be taken for the
    repository from below it: the user running graphite owns it and its
    `.git`, and it is not a drive or filesystem root.

    A directory and a `.git` in it can be made by whoever may write to its
    parent. Taken from below, the root is where graphite reads a graph,
    writes its ledger and points the daemon, so one made by someone else is
    not taken (`ownership`).

    Each of the two is asked about as named and as resolved. The owner of a
    link is whoever made the link, but what graphite reads, writes and
    registers is where it leads; the hooks name the directory as the
    session does, links and all. Never raises: a root that cannot be
    resolved is not taken.
    """
    try:
        real = _resolved(top)
        if top.parent == top or real.parent == real:
            return False
        git = top / ".git"
        named_and_resolved = (top, real, git, _resolved(git))
    except OSError:
        return False
    return all(ownership.owned_by_current_user(path) for path in named_and_resolved)


def repository_root(directory: Path) -> Path:
    """The repository a session or a command in `directory` is about.

    Taking the directory itself for the root made the graph "missing" one
    `cd` below it: for the agent hooks (#80) and for every read command of
    the CLI (#81). The repository is the nearest directory upward that holds
    `.git`, the one place a build of it can run (`enclosing_git_root`).

    That directory is used only where graphite is already set up there (it
    has a graph, or the `GRAPHITE.md` that `init` commits) and it is the
    user's own (`trusted_root`). Anywhere else `directory` stands, as it did
    before, so a session that wanders into a subfolder of some other
    checkout does not hand the daemon that whole project to build. Ownership
    is asked last: the hooks ask for the root at every tool call.

    Never raises: a directory that cannot be read stands for itself.
    """
    try:
        top = enclosing_git_root(directory)
        if top is None or top == directory:
            return directory
        if _is_set_up(top) and trusted_root(top):
            return top
    except OSError:
        pass
    return directory


def _git_ls_files(
    root: Path,
    *,
    max_files: int | None = None,
    is_eligible: Callable[[str], bool] | None = None,
) -> list[str] | None:
    """Return bounded Git files, or None only when *root* is not a Git repository."""
    enclosing = enclosing_git_root(root)
    if enclosing != root:
        if enclosing is not None:
            raise IngestError("nested Git roots are unsupported")
        return None
    try:
        result = GitRunner(root).run(
            ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            timeout_seconds=30,
        )
    except GitError as exc:
        raise IngestError("unable to enumerate Git repository safely") from exc
    if result.returncode != 0:
        raise IngestError("unable to enumerate Git repository safely")
    try:
        output = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IngestError("unable to enumerate Git repository safely") from exc
    if not output:
        return []
    if not output.endswith("\x00"):
        raise IngestError("unable to enumerate Git repository safely")

    selected: list[str] = []
    start = 0
    record_count = 0
    while start < len(output) and (
        max_files is None or len(selected) < max_files
    ):
        end = output.find("\x00", start)
        if end < 0:
            raise IngestError("unable to enumerate Git repository safely")
        record_count += 1
        if record_count > MAX_GIT_FILE_RECORDS:
            raise IngestError("unable to enumerate Git repository safely")
        normalized = _normalize_git_path(root, output[start:end])
        if normalized is None:
            raise IngestError("unable to enumerate Git repository safely")
        if is_eligible is None or is_eligible(normalized):
            selected.append(normalized)
        start = end + 1
    return selected


def _walk_files(
    root: Path,
    cfg: Config,
    dynamic_exclusions: tuple[tuple[str, ...], ...],
) -> Iterable[str]:
    """Fallback filesystem walk respecting SKIP_DIRS."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place.
        current_parts = Path(dirpath).relative_to(root).parts
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIRS
            and not any(
                _has_component_prefix((*current_parts, name), prefix)
                for prefix in dynamic_exclusions
            )
        ]
        for filename in filenames:
            full = Path(dirpath) / filename
            rel = full.relative_to(root).as_posix()
            if _should_skip(rel, cfg, dynamic_exclusions):
                continue
            yield rel


def collect_files(root: Path, cfg: Config) -> list[FileEntry]:
    """Collect files under root, bounded by cfg.max_files as an ingestion cap."""
    root = root.resolve()
    dynamic_exclusions = _dynamic_exclusions(root, cfg)
    tracked = _git_ls_files(
        root,
        max_files=cfg.max_files,
        is_eligible=lambda path: not _should_skip(path, cfg, dynamic_exclusions),
    )
    if tracked is not None:
        rel_paths = tracked
    else:
        walked = _walk_files(root, cfg, dynamic_exclusions)
        rel_paths = list(
            islice(walked, cfg.max_files)
            if cfg.max_files is not None
            else walked
        )

    entries: list[FileEntry] = []
    for rel in rel_paths:
        abs_path = _resolve_contained_path(root, root / Path(rel))
        if abs_path is None:
            continue
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
