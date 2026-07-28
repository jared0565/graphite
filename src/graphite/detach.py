"""Spawn a fully detached child process, portably.

This lives in Python rather than in the shell hook on purpose: git hooks on
Windows run under Git Bash, where backgrounding is unreliable and untestable.
The hook stays a trampoline; the platform logic lives here where the test suite
can reach it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def spawn_detached(cmd: list[str], cwd: Path) -> int:
    """Start `cmd` detached from this process and return its pid."""
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
    return proc.pid
