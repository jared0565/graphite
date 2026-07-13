"""Pure process-launch contract builders shared across platforms."""
from __future__ import annotations

from collections.abc import Mapping


def build_windows_environment_block(environment: Mapping[str, str]) -> str:
    """Build a sorted, double-NUL-terminated Unicode environment block."""
    entries: list[str] = []
    for key, value in sorted(environment.items(), key=lambda item: item[0].upper()):
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError("invalid environment")
        entries.append(f"{key}={value}")
    return "\0".join(entries) + "\0\0"
