"""Resolve an external program to an absolute path, never from the current directory.

Windows looks a bare program name up in the CALLER's current directory before it
reads PATH, unless ``NoDefaultCurrentDirectoryInExePath`` is set. Three routes
do it, each measured with a planted executable (channel round 304; Windows 11,
CPython 3.14.5):

- CreateProcess, behind a list-form ``subprocess.run(["git", ...])``. It
  searches the PARENT process's own current directory, after the interpreter's
  directory and before PATH. The ``cwd=`` argument does not move that search.
- cmd.exe, for a shell command.
- ``shutil.which``, which answered ``.\\git.EXE``.

PowerShell, Git-for-Windows ``sh`` and node's ``child_process`` read PATH only.

graphite's builds, hooks and broker run with a repository root as their current
directory, and they start outside any agent session (the daemon at login, a
commit from a terminal), where that variable is not set. So a ``node.exe``
committed to a TypeScript repo's root ran on every build in place of node.
Proven end to end against the released 1.1.0 wheel.

Launching by absolute path takes the search out of the picture. This module is
the one place that turns a name into that path:

- only absolute PATH entries are read, so an empty entry (POSIX's spelling of
  the current directory), ``.`` and any relative entry are skipped;
- a candidate inside the current directory or inside any ``exclude`` root is
  refused, however PATH came to point at it;
- on Windows a bare name tries ``.exe`` only, as CreateProcess would. A caller
  that launches a ``.cmd`` shim on purpose passes ``extensions``.

Windows' own tools are not looked up on PATH at all: ``system_program`` anchors
them in the directory the OS reports, because a current directory comes before
System32 in the search, and ``SystemRoot`` is the caller's to set.
"""
from __future__ import annotations

import errno
import os
import sys
from collections.abc import Iterable
from pathlib import Path


def resolve_program(
    name: str,
    *,
    exclude: Iterable[Path] = (),
    extensions: Iterable[str] | None = None,
    platform_name: str | None = None,
) -> Path | None:
    """Return the absolute path of ``name`` from PATH, or None.

    ``exclude`` names roots whose contents must never be launched -- typically
    the repository the caller is working on. The current directory is always
    excluded as well.
    """
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError(f"expected a bare program name, got {name!r}")
    selected_platform = os.name if platform_name is None else platform_name
    candidates = _candidate_names(name, selected_platform, extensions)
    refused = _refused_roots(exclude)
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(raw_directory)
        if not directory.is_absolute():
            continue
        for candidate_name in candidates:
            resolved = _usable(directory / candidate_name, selected_platform)
            if resolved is None or any(_inside(resolved, root) for root in refused):
                continue
            return resolved
    return None


def require_program(
    name: str,
    *,
    exclude: Iterable[Path] = (),
    extensions: Iterable[str] | None = None,
) -> Path:
    """`resolve_program`, raising what `subprocess` raises for a missing program.

    Callers written against ``subprocess.run(["git", ...])`` already handle
    FileNotFoundError, so a program that cannot be found safely fails the way
    it always did.
    """
    resolved = resolve_program(name, exclude=exclude, extensions=extensions)
    if resolved is None:
        raise FileNotFoundError(errno.ENOENT, f"{name} was not found on PATH", name)
    return resolved


def windows_system_directory() -> Path:
    """The System32 directory, as the OS reports it."""
    if sys.platform == "win32":
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(32768)
            length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
            if 0 < length < len(buffer):
                return Path(buffer.value)
        except (AttributeError, OSError):
            pass
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return Path(root) / "System32"


def system_program(*parts: str) -> Path:
    """An absolute path to a tool that ships with Windows, e.g.
    ``system_program("schtasks.exe")`` or
    ``system_program("WindowsPowerShell", "v1.0", "powershell.exe")``."""
    return windows_system_directory().joinpath(*parts)


def _candidate_names(
    name: str, platform_name: str, extensions: Iterable[str] | None
) -> tuple[str, ...]:
    if platform_name != "nt":
        return (name,)
    suffixes = (".exe",) if extensions is None else tuple(e.lower() for e in extensions)
    if Path(name).suffix.lower() in suffixes:
        return (name,)
    return tuple(name + suffix for suffix in suffixes)


def _refused_roots(exclude: Iterable[Path]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for root in exclude:
        try:
            roots.append(Path(root).resolve())
        except OSError:
            continue
    try:
        roots.append(Path.cwd().resolve())
    except OSError:
        pass
    return tuple(roots)


def _usable(candidate: Path, platform_name: str) -> Path | None:
    try:
        if not candidate.is_file():
            return None
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            return None
        if platform_name != "nt" and not os.access(resolved, os.X_OK):
            return None
    except OSError:
        return None
    return resolved


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
