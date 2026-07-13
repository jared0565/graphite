"""Pure process-launch contract builders shared across platforms."""
from __future__ import annotations

from collections.abc import Mapping
import threading

# Same-process invariant: repository-owned Windows launchers that enable handle
# inheritance must hold this lock. It cannot serialize untrusted third-party
# native threads or external processes, so child handles remain inheritable only
# for the CreateProcessW call itself and are constrained by HANDLE_LIST.
WINDOWS_PROCESS_CREATION_LOCK = threading.RLock()


def build_windows_environment_block(environment: Mapping[str, str]) -> str:
    """Build a sorted, double-NUL-terminated Unicode environment block."""
    entries: list[str] = []
    for key, value in sorted(environment.items(), key=lambda item: item[0].upper()):
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError("invalid environment")
        entries.append(f"{key}={value}")
    return "\0".join(entries) + "\0\0"
