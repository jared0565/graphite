"""Content-addressed JSON cache for deterministic incremental builds."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
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

# The one entry shape `_path` ever writes: `<partition>/<2 hex>/<64 hex>.json`,
# with the shard equal to the key's first two characters. Entry pruning (#66)
# matches this exactly, for the same reason the partition rule is exact.
_SHARD_RE = re.compile(r"^[0-9a-f]{2}$")
_ENTRY_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


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
        # Every key this instance read (and found) or wrote: the entries the
        # build it belongs to can reach. `extract_all` shares one instance
        # across its worker threads, hence the lock -- a set add is atomic
        # under the GIL today, but the contract should not depend on that.
        self._touched: set[str] = set()
        self._touched_lock = threading.Lock()

    def _key(self, *parts: str) -> str:
        """Stable cache key from parts."""
        return hashlib.sha256("::".join(parts).encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def _touch(self, key: str) -> None:
        with self._touched_lock:
            self._touched.add(key)

    def read(self, *parts: str) -> Any | None:
        key = self._key(*parts)
        path = self._path(key)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                value = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        # Only a HIT is a reach. A miss is followed by a write, which touches;
        # an unreadable entry is replaced by that write too, so counting it
        # here would keep a corrupt file alive for nothing.
        self._touch(key)
        return value

    def write(self, value: Any, *parts: str) -> None:
        key = self._key(*parts)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
        self._touch(key)

    def prune_unreachable_entries(self) -> tuple[int, int]:
        """Delete every entry in this partition that this build neither read
        nor wrote (#66). Returns (entries removed, bytes reclaimed).

        #23 reclaims whole partitions a build can never read again; nothing
        reclaimed entries INSIDE the partition it uses. Keys carry the file's
        content hash, so every edit that triggers a rebuild adds an entry and
        orphans the previous one, and a checkout that changes often grows
        without bound -- measured at 15,406 entries / 419 MB in one consumer
        checkout, against 1,780 / 62 MB for a tree of the same order.

        Same rule as #23, one level down: reachability, not age or size. After
        a build has extracted every file in the tree, the keys it touched are
        exactly the entries the graph it wrote was built from; anything else
        describes content no longer in the tree. Checking out older content
        later re-extracts it once -- performance, never correctness, the
        trade-off #23 already accepted for engine switches.

        Safe without a lock, unlike a partition: `read` treats a vanished file
        as a miss, so a concurrent reader of an entry deleted here re-extracts
        rather than fails. Callers still prune only after their own extraction
        completed, so the touched set is whole. A build that touched NOTHING
        proves nothing about reachability and prunes nothing.

        Only the exact shape `_path` writes is eligible -- a lowercase 64-hex
        `.json` inside the two-hex shard its key names -- so a stray file, an
        entry in the wrong shard or an unrelated directory survives. Failures
        are swallowed per entry: content is regenerable and a Windows handle
        race must never fail a build.
        """
        with self._touched_lock:
            touched = set(self._touched)
        if not touched:
            return 0, 0
        removed = 0
        reclaimed = 0
        try:
            shards = list(self.root.iterdir())
        except OSError:
            return 0, 0
        for shard in shards:
            try:
                if not shard.is_dir() or not _SHARD_RE.match(shard.name):
                    continue
                entries = list(shard.iterdir())
            except OSError:
                continue
            for entry in entries:
                key = entry.stem
                if (
                    entry.suffix != ".json"
                    or not _ENTRY_KEY_RE.match(key)
                    or key[:2] != shard.name
                    or key in touched
                ):
                    continue
                try:
                    size = entry.stat().st_size
                    entry.unlink()
                except OSError:
                    continue
                removed += 1
                reclaimed += size
        return removed, reclaimed

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
