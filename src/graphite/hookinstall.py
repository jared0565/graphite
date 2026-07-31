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

from .config import default_projects_root
from .hookshim import CHAINED_SUFFIX, MARKER_START, TRIGGERS, render_trigger_shim

DEFAULT_HOOKS_DIRNAME = ".githooks"

# Distinct from the unrelated "template" used for GRAPHITE.md/instruction-doc
# versioning (`init.py`'s DOC_VERSION) -- "hooks" is spelled out so the two
# concepts never look like the same thing on disk.
DEFAULT_TEMPLATE_DIRNAME = ".graphite-hooks-template"


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


def hook_shim_present(path: Path) -> bool:
    """Did graphite write this hook file? Decided by the in-file marker.

    The single marker predicate for the whole codebase. It lived in `doctor`
    until 2026-07-31; two copies in two modules is how the relocated-hook rule
    below drifts apart from the code that enforces it.
    """
    if not path.is_file():
        return False
    try:
        return MARKER_START.encode() in path.read_bytes()
    except OSError:
        return False


def managed_hook_paths(root: Path) -> tuple[str, ...]:
    """Hook files graphite *authored*, as repo-relative posix paths.

    Marker-based rather than a directory glob, and that is load-bearing in two
    directions rather than a convenience:

    * A `.local` sibling is the pre-existing hook graphite chained to. It is
      machine-local by construction and carries no marker; reporting it as
      graphite's would tell an operator to commit someone else's private hook.
    * A hook graphite does not trigger on is relocated *byte-identically*, with
      no marker, per this module's stated interop rule. Graphite moved it but
      did not write it -- in graphite's own repo those are aramid's
      `pre-commit` and `pre-push`. Claiming them would have doctor reporting
      another tool's gate as graphite's file to commit.

    Returns nothing when hooks live anywhere but the default directory. A
    custom `core.hooksPath` means another tool (husky et al.) owns hook policy
    here: graphite installs into their directory by design, but it does not get
    to speak for its contents, and `ensure_gitignore_allowlist` must never
    rewrite ignore rules for a directory it does not own. An absolute path
    outside the repo additionally cannot be committed to it, so reporting it
    would be advice nobody can act on.
    """
    hdir = hooks_dir(root)
    if hdir != root / DEFAULT_HOOKS_DIRNAME:
        return ()
    return tuple(f"{DEFAULT_HOOKS_DIRNAME}/{hook}" for hook in TRIGGERS if hook_shim_present(hdir / hook))


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


def default_template_root() -> Path:
    """Where `graphite hooks --install-template` writes by default.

    Mirrors the machine-state convention already used for daemon state --
    `<default_projects_root>/.graphite-daemon` (see `config.default_projects_root`,
    `cli._incidents_ledger_dir`) -- rather than inventing a new location.
    Never reads or writes real global git config; that stays the human's step.
    """
    return default_projects_root() / DEFAULT_TEMPLATE_DIRNAME


def install_template(template_root: Path, interpreter: Path) -> list[Path]:
    """Write graphite's trigger shims into a git `init.templateDir` layout.

    Git copies `<templateDir>/hooks/<name>` into `.git/hooks/<name>` on every
    future `git init`/`git clone` on this machine once a human points
    `init.templateDir` at `template_root` -- the `hooks/` subdirectory is
    required, since that is what git actually copies from. This function
    itself never touches git config, real or otherwise, and only ever writes
    under `template_root`.

    Unlike `install_hooks`, there is no relocation, no `.local` chaining and
    no `core.hooksPath` write: there is nothing pre-existing to migrate in a
    template directory, and git's own template-copy mechanism is what wires a
    fresh repo up. The written bytes are exactly `render_trigger_shim`'s --
    no separate rendering path for the template case.

    This shim runs in *every* new clone on the machine, onboarded with
    `GRAPHITE.md` or not, so it must fail open. `hook_entry.main()` already
    does: it returns 0 silently when no `GRAPHITE.md` is found walking up
    from cwd. That existing behaviour is what makes reusing the same shim
    here safe -- a machine-wide template hook that errors would break every
    unrelated repo on the machine.

    Regeneration is idempotent for the same `(template_root, interpreter)`:
    `render_trigger_shim` is pure and there is no chaining state to disturb.
    """
    hooks_subdir = template_root / "hooks"
    hooks_subdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for hook in TRIGGERS:
        path = hooks_subdir / hook
        path.write_bytes(render_trigger_shim(hook, interpreter))
        _make_executable(path)
        written.append(path)
    return written
