"""Content-addressed JSON cache for deterministic incremental builds."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class Cache:
    """Simple content-addressed cache on disk."""

    def __init__(self, root: Path, version: str):
        self.root = root / version
        self.root.mkdir(parents=True, exist_ok=True)

    def _key(self, *parts: str) -> str:
        """Stable cache key from parts."""
        return hashlib.sha256("::".join(parts).encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def read(self, *parts: str) -> Any | None:
        path = self._path(self._key(*parts))
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def write(self, value: Any, *parts: str) -> None:
        path = self._path(self._key(*parts))
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, separators=(",", ":"))

    def clear(self) -> None:
        if self.root.exists():
            for item in self.root.rglob("*.json"):
                item.unlink()


def file_hash(path: Path) -> str:
    """Hash a file's content. Empty/missing files return empty string."""
    if not path.exists():
        return ""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def content_hash(text: str) -> str:
    """Hash arbitrary text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
