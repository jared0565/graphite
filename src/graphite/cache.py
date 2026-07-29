"""Content-addressed JSON cache for deterministic incremental builds."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

# The two partition shapes that exist on disk. Both are matched because a rule
# covering only the engine-suffixed form would strand the legacy ones and still
# appear to work:
#   v11-0570918cea69669b   post-#21, `{version}-{engine[:16]}`
#   v10                    pre-#21, bare version
# Lowercase hex and an exact width of 16 are required so a directory that
# merely resembles a partition is left alone -- `.cache/graphite/` is
# graphite-owned, but a rule loose enough to delete an unrelated directory is
# not worth the reclaimed megabytes.
_PARTITION_RE = re.compile(r"^(?:v\d+|.+-[0-9a-f]{16})$")


def _is_partition_dir(path: Path) -> bool:
    try:
        if not path.is_dir():
            return False
    except OSError:
        return False
    return bool(_PARTITION_RE.match(path.name))


class Cache:
    """Simple content-addressed cache on disk."""

    def __init__(self, root: Path, version: str, *, engine: str):
        # Partitioned on engine identity as well as `cache_version` (#21).
        # `engine_identity()` is strictly finer-grained -- it hashes the packaged
        # engine files and takes `cache_version` as an input -- so keying on the
        # version alone let an extraction change serve the previous engine's
        # cached result while the graph's recorded fingerprint moved to the new
        # value, which made `check` report the stale graph as fresh.
        #
        # `engine` is a required keyword rather than an optional one on purpose:
        # a caller that forgets it must fail loudly, not silently fall back to a
        # shared partition and reintroduce the defect.
        self.root = root / f"{version}-{engine[:16]}"
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

    def prune_other_partitions(self) -> list[str]:
        """Delete sibling partitions this cache can never read again (#23).

        Only one partition is reachable at a time -- the one whose name matches
        the current `cache_version` and engine -- so every other one is dead
        weight. Nothing ever removed them, and since #21 a new one appears per
        distinct engine build rather than per manual version bump, so the leak
        grew from bounded to unbounded.

        Reachability is the rule, deliberately, rather than a count or an age.
        "Keep the newest N" is a guess that can delete a partition another
        build is mid-read on; "is not the partition I am using" is a fact.
        Callers hold the repo build lock, which serialises builds of the same
        repo, so no concurrent build of this cache can be reading a sibling.

        Failures are swallowed. A partition can be locked by another process
        (routine on Windows), and cache content is regenerable, so a failed
        reclaim is a non-event that must never fail a build.

        Returns the names actually removed.
        """
        parent = self.root.parent
        removed: list[str] = []
        try:
            entries = list(parent.iterdir())
        except OSError:
            return removed
        for entry in entries:
            if entry.name == self.root.name or not _is_partition_dir(entry):
                continue
            try:
                shutil.rmtree(entry)
            except OSError:
                continue
            removed.append(entry.name)
        return removed


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
