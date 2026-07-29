"""Pure rendering of graphite's git-hook trampolines.

Deliberately has NO filesystem or git side effects -- installation lives in
`hookinstall`. Every Windows correctness rule below was taken from aramid's
`src/aramid/hooks.py`, which had already paid for them; keeping rendering pure
is what makes them cheaply testable.
"""
from __future__ import annotations

from pathlib import Path

MARKER_START = "# >>> graphite managed >>>"
MARKER_END = "# <<< graphite managed <<<"

# The events that change committed code. `post-checkout` is excluded on
# purpose: branch switching is frequent and transient, and hooking it makes
# `git bisect` expensive.
TRIGGERS: tuple[str, ...] = ("post-commit", "post-merge", "post-rewrite")

# The relocated original hook, invoked before graphite's own body. `.local`
# rather than a graphite-specific suffix so a human reading `.githooks/` can
# tell at a glance which file is theirs.
CHAINED_SUFFIX = ".local"


def sh_interpreter_path(interpreter: Path) -> str:
    """`C:\\Python314\\python.exe` -> `/c/Python314/python.exe`.

    Git-for-Windows' `sh` cannot exec a drive-letter path. A POSIX path is
    returned unchanged.
    """
    resolved = interpreter.resolve()
    drive = resolved.drive.rstrip(":").lower()
    if not drive:
        return resolved.as_posix()
    rest = resolved.as_posix()[len(resolved.drive):].lstrip("/")
    return f"/{drive}/{rest}"


def render_trigger_shim(hook: str, interpreter: Path) -> bytes:
    """The trampoline graphite installs for one of its triggers.

    Three rules, each load-bearing:

    * **`if`/`fi`, never `[ -f "$C" ] && { ...; }`.** As a script's *final*
      command the latter exits 1 when `$C` is absent -- the test fails, `&&`
      short-circuits, and that status becomes the script's -- so a trampoline
      with nothing to chain to would block every commit on a fresh clone.
      Measured, and independently reproduced by aramid's agent.
    * **The chain-check is always emitted**, whether or not a `.local` exists.
      Baking that state into the bytes is what breaks idempotent regeneration.
    * **Never a bare `python`.** This machine exposes several interpreters to
      hook `sh`, including the WindowsApps store stub, so the absolute path is
      baked in with `py -3` as the only fallback.

    All three triggers are `post-*`, where git ignores the exit code, so the
    chained hook's failure is swallowed with `|| true` and the shim ends with
    an explicit `exit 0`: a graph refresh must never fail a developer's commit.
    """
    interp = sh_interpreter_path(interpreter)
    lines = [
        "#!/bin/sh",
        MARKER_START,
        'DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)',
        f'CHAINED="$DIR/{hook}{CHAINED_SUFFIX}"',
        'if [ -f "$CHAINED" ]; then',
        '    "$CHAINED" "$@" || true',
        "fi",
        f'INTERP="{interp}"',
        'if [ -x "$INTERP" ]; then',
        '    "$INTERP" -m graphite.hook_entry >/dev/null 2>&1 || true',
        "elif command -v py >/dev/null 2>&1; then",
        "    py -3 -m graphite.hook_entry >/dev/null 2>&1 || true",
        "fi",
        MARKER_END,
        "exit 0",
        "",
    ]
    return "\n".join(lines).encode("utf-8")
