"""Unreachable extraction-cache partitions must be reclaimed (#23).

Every `cache_version` bump -- and, since #21, every distinct engine build --
creates a new partition directory, and nothing ever removed the old one. Only
one partition per repo is reachable at any time; the rest are dead weight that
grows without bound. Measured on this machine before the fix: graphite held 21
partitions with exactly one reachable, roughly 164 MB unreclaimable.

`Cache.clear()` could never help: it is scoped to `self.root`, i.e. the
*current* partition, so it cannot reach a sibling.

Two partition shapes exist on disk and both must be handled -- a rule matching
only the engine-suffixed form would silently strand the legacy ones and still
look like it worked:

    v11-0570918cea69669b   post-#21, engine-suffixed  (the unbounded leak)
    v10, v11               pre-#21, bare version      (bounded, but real)
"""
from __future__ import annotations

from pathlib import Path

from graphite.cache import Cache

ENGINE_A = "a" * 64
ENGINE_B = "b" * 64


def _partition(root: Path, name: str) -> Path:
    """A directory that looks like a real partition, with content in it."""
    p = root / name
    (p / "ab").mkdir(parents=True, exist_ok=True)
    (p / "ab" / "deadbeef.json").write_text("{}", encoding="utf-8")
    return p


def test_prunes_an_unreachable_engine_suffixed_sibling(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    stale = _partition(root, f"v11-{'c' * 16}")

    cache = Cache(root, "v11", engine=ENGINE_A)
    pruned = cache.prune_other_partitions()

    assert not stale.exists()
    assert stale.name in pruned


def test_prunes_a_legacy_bare_version_sibling(tmp_path: Path) -> None:
    """Pre-#21 partitions carry no engine suffix. aramid held v4..v11 of these."""
    root = tmp_path / "cache"
    legacy = _partition(root, "v10")

    cache = Cache(root, "v11", engine=ENGINE_A)
    pruned = cache.prune_other_partitions()

    assert not legacy.exists()
    assert "v10" in pruned


def test_never_prunes_the_current_partition(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    cache = Cache(root, "v11", engine=ENGINE_A)
    cache.write({"n": 1}, "ast", "h", "python")

    cache.prune_other_partitions()

    assert cache.root.exists()
    assert cache.read("ast", "h", "python") == {"n": 1}, "pruning destroyed live cache"


def test_never_prunes_directories_that_are_not_partitions(tmp_path: Path) -> None:
    """The rule must be tight enough that an unrecognised directory survives.
    `.cache/graphite/` is graphite-owned, but a rule loose enough to delete an
    unrelated directory is not worth the reclaimed megabytes."""
    root = tmp_path / "cache"
    keepers = [
        _partition(root, "notes"),
        _partition(root, "v11-tooshort"),
        _partition(root, f"v11-{'c' * 16}-extra"),
        _partition(root, f"v11-{'C' * 16}"),  # uppercase is not the hex we emit
    ]

    Cache(root, "v11", engine=ENGINE_A).prune_other_partitions()

    for path in keepers:
        assert path.exists(), f"pruned a non-partition directory: {path.name}"


def test_never_prunes_a_file(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir(parents=True, exist_ok=True)
    stray = root / f"v11-{'c' * 16}"
    stray.write_text("not a directory", encoding="utf-8")

    Cache(root, "v11", engine=ENGINE_A).prune_other_partitions()

    assert stray.exists(), "pruned a file that merely had a partition-shaped name"


def test_pruning_tolerates_an_undeletable_partition(tmp_path: Path, monkeypatch) -> None:
    """A partition locked by another process (routine on Windows) must not fail
    the build. Cache is regenerable; a failed reclaim is a non-event."""
    import graphite.cache as cache_mod

    root = tmp_path / "cache"
    _partition(root, f"v11-{'c' * 16}")

    def _boom(*args, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(cache_mod.shutil, "rmtree", _boom)
    cache = Cache(root, "v11", engine=ENGINE_A)

    assert cache.prune_other_partitions() == []


def test_pruning_is_idempotent_and_reclaims_across_an_engine_change(tmp_path: Path) -> None:
    """The #23 scenario end to end: successive engine builds must not accumulate."""
    root = tmp_path / "cache"

    Cache(root, "v11", engine=ENGINE_A).write({"n": 1}, "ast", "h", "python")
    second = Cache(root, "v11", engine=ENGINE_B)
    second.prune_other_partitions()

    assert [p.name for p in root.iterdir()] == [second.root.name]
    assert second.prune_other_partitions() == []
