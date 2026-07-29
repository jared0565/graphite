"""Installing, migrating and removing graphite's git hooks.

All filesystem and git side effects live here; `hookshim` stays pure.

**The interop rule this module exists to honour:** a hook graphite does not
trigger on is *relocated byte-identically* -- moved, unchanged, with no
graphite marker and no `.local` sibling. It is never wrapped in a pass-through
trampoline.

An earlier design did wrap them. aramid's `install()` treats any hook carrying
another tool's `# >>> <tool> managed >>>` marker as foreign-managed and refuses
it outright (aramid `0f24609`), skipping its own gate for that hook. Since the
old design stamped graphite's marker onto *every* migrated hook including
`pre-commit`/`pre-push`, `graphite init` would have left aramid's gates
un-refreshable in every repo it touched. Relocation avoids this entirely:
aramid's shim keeps its own marker, so `_is_aramid_shim` is true, its
foreign-hook branch never runs, and it regenerates in place. aramid's agent
independently confirmed this against `hooks.py:333,338-342` and
`init.py:198-206` before it shipped.
"""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from .hookshim import CHAINED_SUFFIX, MARKER_START, TRIGGERS, render_trigger_shim

DEFAULT_HOOKS_DIRNAME = ".githooks"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )


def hooks_dir(root: Path) -> Path:
    """Where hooks live for this repo.

    Honours an existing `core.hooksPath` rather than taking it over: if husky
    (or anything else) already owns hook policy here, graphite installs into
    *their* directory. Relative values resolve against the repo root, matching
    git's own rule and aramid's `hooks_dir`.
    """
    configured = _git(root, "config", "--get", "core.hooksPath").stdout.strip()
    if not configured:
        return root / DEFAULT_HOOKS_DIRNAME
    candidate = Path(configured)
    return candidate if candidate.is_absolute() else root / candidate


def _make_executable(path: Path) -> None:
    """Best-effort on a bare Windows filesystem, but it matters the moment the
    repo is cloned onto WSL or a Linux CI runner."""
    try:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _is_graphite_shim(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return MARKER_START.encode() in path.read_bytes()
    except OSError:
        return False


def _legacy_hooks(root: Path) -> list[Path]:
    legacy = root / ".git" / "hooks"
    if not legacy.is_dir():
        return []
    return [p for p in sorted(legacy.iterdir()) if p.is_file() and p.suffix != ".sample"]


def install_hooks(root: Path, interpreter: Path) -> list[str]:
    """Install graphite's trampolines, migrating whatever was already there.

    Returns the names of hooks that were relocated (not chained), so callers
    can report them -- a silent migration of someone else's hook is exactly the
    kind of surprise this design is trying to avoid.

    Ordering is load-bearing: `core.hooksPath` is written **last**, so no
    window exists in which it redirects to a directory that has no hooks yet.
    """
    hdir = hooks_dir(root)
    hdir.mkdir(parents=True, exist_ok=True)
    already_configured = bool(
        _git(root, "config", "--get", "core.hooksPath").stdout.strip()
    )

    relocated: list[str] = []
    for legacy in _legacy_hooks(root):
        name = legacy.name
        target = hdir / (f"{name}{CHAINED_SUFFIX}" if name in TRIGGERS else name)
        if target.exists() or (name not in TRIGGERS and (hdir / name).exists()):
            # Already migrated by a previous run; leave both copies alone
            # rather than clobbering a live hook.
            continue
        target.write_bytes(legacy.read_bytes())
        _make_executable(target)
        legacy.unlink()
        if name not in TRIGGERS:
            relocated.append(name)

    for hook in TRIGGERS:
        slot = hdir / hook
        # A pre-existing NON-graphite file in a trigger slot (e.g. one written
        # straight into .githooks/) becomes the chained original. A graphite
        # shim is simply regenerated -- never chained to itself.
        if slot.exists() and not _is_graphite_shim(slot):
            chained = hdir / f"{hook}{CHAINED_SUFFIX}"
            if not chained.exists():
                slot.replace(chained)
                _make_executable(chained)
            else:
                slot.unlink()
        slot.write_bytes(render_trigger_shim(hook, interpreter))
        _make_executable(slot)

    if not already_configured:
        _git(root, "config", "core.hooksPath", DEFAULT_HOOKS_DIRNAME)
    return relocated


def uninstall_hooks(root: Path) -> list[str]:
    """Remove graphite's trampolines, restoring anything they chained.

    Only graphite's own shims are touched. A relocated foreign hook is left
    exactly where it is: graphite moved it but never owned it, and deleting it
    would take out another tool's live gate.
    """
    hdir = hooks_dir(root)
    removed: list[str] = []
    for hook in TRIGGERS:
        slot = hdir / hook
        if not _is_graphite_shim(slot):
            continue
        chained = hdir / f"{hook}{CHAINED_SUFFIX}"
        slot.unlink()
        if chained.exists():
            chained.replace(slot)
            _make_executable(slot)
        removed.append(hook)
    return removed
