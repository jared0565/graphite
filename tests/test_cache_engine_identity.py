"""Extraction cache must be keyed on engine identity, not `cache_version` alone (#21).

`graphite check` detects an engine change and says "rebuild to refresh". The
rebuild did not refresh: `Cache` partitioned only on `cache_version`, so
unchanged files hit the cache and old-engine extraction was served, while
`metadata.engine.fingerprint` was rewritten to the new value. `check` then
reported the graph as fresh -- the one signal that could have caught it was
cleared by the act that failed to fix it.

`engine_identity()` is strictly finer-grained than `cache_version`: it hashes
the packaged engine files and takes `cache_version` as an *input*. The precise
signal was computed and recorded, then not used for the one thing it would fix.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.cache import Cache


def _engine(fingerprint: str) -> dict[str, str]:
    return {
        "cache_version": "v11",
        "schema_version": "1",
        "version": "0.0.0",
        "fingerprint": fingerprint,
    }


def test_engine_change_isolates_cached_extraction(tmp_path: Path) -> None:
    """Same cache_version, different engine -- the old entry must not be served."""
    root = tmp_path / "cache"

    old = Cache(root, "v11", engine="a" * 64)
    old.write({"nodes": ["stale"]}, "ast", "content-hash", "python")

    new = Cache(root, "v11", engine="b" * 64)

    assert new.read("ast", "content-hash", "python") is None, (
        "a new engine served the previous engine's cached extraction"
    )


def test_unchanged_engine_still_hits_the_cache(tmp_path: Path) -> None:
    """Guard against throwing the cache away entirely: identical inputs reuse it."""
    root = tmp_path / "cache"

    first = Cache(root, "v11", engine="a" * 64)
    first.write({"nodes": ["fresh"]}, "ast", "content-hash", "python")

    second = Cache(root, "v11", engine="a" * 64)

    assert second.read("ast", "content-hash", "python") == {"nodes": ["fresh"]}, (
        "an unchanged engine must reuse cached extraction, or every build re-extracts"
    )


def test_cache_version_still_partitions_independently(tmp_path: Path) -> None:
    """The coarse manual override must keep working alongside the engine key."""
    root = tmp_path / "cache"

    Cache(root, "v11", engine="a" * 64).write({"n": 1}, "ast", "h", "python")

    assert Cache(root, "v12", engine="a" * 64).read("ast", "h", "python") is None, (
        "a cache_version bump must still invalidate, independently of the engine"
    )


def test_build_partitions_the_cache_by_engine_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring test: `_build` must pass the engine through to the Cache.

    This is the defect as reported -- two builds of an unchanged repo under the
    same `cache_version` but different engines. Before the fix both builds share
    one partition, which is exactly how stale extraction got served.
    """
    from graphite import cli

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GRAPHITE_DAEMON_CHILD", "1")  # don't touch the live activation registry

    monkeypatch.setattr(cli, "engine_identity", lambda _v: _engine("a" * 64))
    assert cli.main(["build", "."]) == 0

    # Same repo, same cache_version, different engine. Nothing else changes.
    monkeypatch.setattr(cli, "engine_identity", lambda _v: _engine("b" * 64))
    assert cli.main(["build", "."]) == 0

    partitions = sorted(p.name for p in (repo / ".cache" / "graphite").iterdir() if p.is_dir())

    assert len(partitions) == 2, (
        f"two engines shared one cache partition, so the second build served the "
        f"first's extraction: {partitions}"
    )
