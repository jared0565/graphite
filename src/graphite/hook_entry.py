"""The fast path a git-hook trampoline execs.

**This module MUST NOT import `graphite.cli`.** Measured on Windows, best of 3:

    bare python                              79 ms
    import graphite                          86 ms   (__init__ is nearly free)
    import graphite.detach                  137 ms
    import graphite.cli                    1281 ms

A trampoline that reached the CLI would put ~1.2s on *every commit* purely to
spawn a background build. The cost is specifically `cli.py`, not "importing
graphite at all", which is what makes a Python hook entry viable in the first
place. `tests/test_hook_entry.py` enforces this from a subprocess -- an
in-process check would false-pass, since pytest imports the CLI elsewhere.

Deliberately does NOT take the build lock. `cmd_build` already acquires it in
the child; holding it here would mean the detached child finds the lock held by
its own parent and exits with "build skipped", so no build would ever run.
"""
import sys
from pathlib import Path

from .detach import spawn_detached

# The committed marker that means "this repo is onboarded to graphite". Chosen
# over `.git` on purpose: the machine-wide `init.templateDir` shim runs in every
# new clone on the machine, so the guard must be something a repo opts into.
MARKER = "GRAPHITE.md"


def find_repo_root(start: Path) -> Path | None:
    """Nearest ancestor of `start` (inclusive) carrying the marker."""
    for directory in (start, *start.parents):
        if (directory / MARKER).exists():
            return directory
    return None


def main(argv: list[str] | None = None) -> int:
    """Spawn a detached graph build for the repo containing `argv[0]`.

    Always returns 0. Git ignores `post-*` exit codes anyway, but the shim also
    ends with an explicit `exit 0`, and a graph refresh must never be able to
    fail a developer's commit.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    start = Path(args[0]) if args else Path.cwd()
    try:
        start = start.resolve()
    except OSError:
        return 0

    root = find_repo_root(start)
    if root is None:
        return 0  # not an onboarded repo -- fail open, silently

    # The root is passed explicitly AND used as cwd. Both are load-bearing:
    # `Config.output_dir` / `cache_dir` default to RELATIVE paths that
    # `cmd_build` resolves against the process CWD, so a child launched from
    # elsewhere would write the graph into the wrong directory.
    spawn_detached(
        [sys.executable, "-B", "-m", "graphite", "build", str(root)], root
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
