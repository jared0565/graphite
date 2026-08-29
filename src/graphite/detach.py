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
    # One `Popen` call per platform rather than a kwargs dict: the isolation
    # flag is the only difference, and spelled out it is what mypy narrows on.
    if sys.platform == "win32":
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    return proc.pid
