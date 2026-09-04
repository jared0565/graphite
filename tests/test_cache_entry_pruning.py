"""Unreachable extraction-cache ENTRIES must be reclaimed inside a partition (#66).

#23 reclaims whole partitions a build can never read again and left the one it
uses alone. Entries are keyed on the file's content hash, so every edit that
triggers a rebuild adds an entry and orphans the previous one, and nothing ever
removed an orphan. Measured 2026-09-04 on the same engine fingerprint: one
consumer checkout, rebuilt by the daemon on every change, held 15,406 entries /
419 MB in its single partition (11,592 of them from four days), against
1,780 / 62 MB for graphite's own tree.

Same rule as #23, one level down: after a build has extracted the tree, the
keys it read or wrote are exactly the entries its graph was built from;
everything else in the partition is unreachable and goes. Older content checked
out later re-extracts once -- performance, never correctness.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from graphite.cache import Cache

ENGINE = "a" * 64


def _entries(partition: Path) -> set[str]:
    return {p.stem for p in partition.rglob("*.json")}


def _plant(partition: Path, shard: str, name: str, text: str = "{}") -> Path:
    path = partition / shard / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- the rule ----------------------------------------------------------------


def test_prunes_entries_the_build_neither_read_nor_wrote(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    earlier = Cache(root, "v11", engine=ENGINE)
    for h in ("h1", "h2", "h3"):
        earlier.write({"n": h}, "ast", h, "python:ast")
    assert len(_entries(earlier.root)) == 3

    build = Cache(root, "v11", engine=ENGINE)  # the next build: fresh touched set
    assert build.read("ast", "h1", "python:ast") == {"n": "h1"}  # a hit: reachable
    build.write({"n": "h4"}, "ast", "h4", "python:ast")  # a write: reachable
    removed, reclaimed = build.prune_unreachable_entries()

    assert removed == 2
    assert reclaimed > 0
    assert _entries(build.root) == {
        build._key("ast", "h1", "python:ast"),
        build._key("ast", "h4", "python:ast"),
    }
    assert build.read("ast", "h1", "python:ast") == {"n": "h1"}, "pruning destroyed a live entry"
    assert build.read("ast", "h2", "python:ast") is None


def test_a_miss_and_an_unreadable_entry_are_not_reaches(tmp_path: Path) -> None:
    """Only a HIT keeps an entry. A miss is followed by a write that touches;
    a corrupt entry is replaced by that write, so counting the failed read
    would keep the corrupt file alive for nothing."""
    root = tmp_path / "cache"
    earlier = Cache(root, "v11", engine=ENGINE)
    earlier.write({"n": 1}, "ast", "good", "python:ast")
    corrupt = earlier._path(earlier._key("ast", "bad", "python:ast"))
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{not json", encoding="utf-8")

    build = Cache(root, "v11", engine=ENGINE)
    assert build.read("ast", "never-written", "python:ast") is None
    assert build.read("ast", "bad", "python:ast") is None
    assert build.read("ast", "good", "python:ast") == {"n": 1}
    removed, _ = build.prune_unreachable_entries()

    assert removed == 1
    assert not corrupt.exists()
    assert build.read("ast", "good", "python:ast") == {"n": 1}


def test_a_build_that_touched_nothing_prunes_nothing(tmp_path: Path) -> None:
    """No reads and no writes prove nothing about reachability -- an empty
    scan, an extraction that bypassed the cache -- so the partition is kept."""
    root = tmp_path / "cache"
    Cache(root, "v11", engine=ENGINE).write({"n": 1}, "ast", "h", "python:ast")

    build = Cache(root, "v11", engine=ENGINE)
    assert build.prune_unreachable_entries() == (0, 0)
    assert len(_entries(build.root)) == 1


def test_pruning_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    Cache(root, "v11", engine=ENGINE).write({"n": 1}, "ast", "old", "python:ast")
    build = Cache(root, "v11", engine=ENGINE)
    build.write({"n": 2}, "ast", "new", "python:ast")

    assert build.prune_unreachable_entries()[0] == 1
    assert build.prune_unreachable_entries() == (0, 0)


# --- the rule is exact -------------------------------------------------------


def test_only_the_shape_the_cache_writes_is_eligible(tmp_path: Path) -> None:
    """A stray file, an entry in the wrong shard, a non-hex shard or an
    uppercase key is nothing `_path` ever produced, so it is left alone --
    the same tightness the partition rule has."""
    root = tmp_path / "cache"
    build = Cache(root, "v11", engine=ENGINE)
    build.write({"n": 1}, "ast", "h", "python:ast")
    keepers = [
        _plant(build.root, "ab", "notes.txt"),
        _plant(build.root, "ab", "short.json"),
        _plant(build.root, "ab", f"cd{'0' * 62}.json"),  # key says shard `cd`, sits in `ab`
        # Uppercase is not the hex we emit. A DIFFERENT key from the doomed
        # entry below: on a case-insensitive filesystem (Windows, the default
        # macOS volume) `AB…` and `ab…` would be one file.
        _plant(build.root, "ab", f"AB{'1' * 62}.json"),
        _plant(build.root, "zz", f"zz{'0' * 62}.json"),  # `zz` is not a hex shard
        _plant(build.root, "abc", f"ab{'0' * 62}.json"),  # three-character shard
    ]
    doomed = _plant(build.root, "ab", f"ab{'0' * 62}.json")

    removed, _ = build.prune_unreachable_entries()

    assert removed == 1
    assert not doomed.exists()
    for path in keepers:
        assert path.exists(), f"pruned something the cache never wrote: {path.relative_to(build.root)}"


def test_a_stray_file_where_a_shard_should_be_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    build = Cache(root, "v11", engine=ENGINE)
    build.write({"n": 1}, "ast", "h", "python:ast")
    stray = build.root / "cd"
    stray.write_text("not a directory", encoding="utf-8")

    assert build.prune_unreachable_entries() == (0, 0)
    assert stray.exists()


# --- failure is a non-event --------------------------------------------------


def test_pruning_tolerates_an_undeletable_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry held open by another process (routine on Windows) must not
    fail the build; content is regenerable."""
    root = tmp_path / "cache"
    Cache(root, "v11", engine=ENGINE).write({"n": 1}, "ast", "old", "python:ast")
    build = Cache(root, "v11", engine=ENGINE)
    build.write({"n": 2}, "ast", "new", "python:ast")

    def _locked(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "unlink", _locked)

    assert build.prune_unreachable_entries() == (0, 0)
    assert len(_entries(build.root)) == 2


def test_pruning_tolerates_an_unlistable_partition(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    build = Cache(root, "v11", engine=ENGINE)
    build.write({"n": 1}, "ast", "h", "python:ast")
    import shutil

    shutil.rmtree(build.root)

    assert build.prune_unreachable_entries() == (0, 0)


# --- one instance, many extractor threads -------------------------------------


def test_touches_from_extractor_threads_are_all_kept(tmp_path: Path) -> None:
    """`extract_all` shares one Cache across a thread pool; every worker's
    reads and writes must count, or the prune would delete what a sibling
    thread just wrote."""
    root = tmp_path / "cache"
    Cache(root, "v11", engine=ENGINE).write({"n": -1}, "ast", "stale", "python:ast")
    build = Cache(root, "v11", engine=ENGINE)

    def work(i: int) -> None:
        build.write({"n": i}, "ast", f"h{i}", "python:ast")
        assert build.read("ast", f"h{i}", "python:ast") == {"n": i}

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(64)))

    removed, _ = build.prune_unreachable_entries()

    assert removed == 1
    assert len(_entries(build.root)) == 64


# --- wired into the build -------------------------------------------------------


def _only_partition(project: Path) -> Path:
    partitions = [p for p in (project / ".cache" / "graphite").iterdir() if p.is_dir()]
    assert len(partitions) == 1, partitions
    return partitions[0]


def test_build_reclaims_the_entries_of_content_no_longer_in_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The #66 scenario end to end: edit, rebuild, and the previous content's
    entry is gone and reported; rebuild with no change and nothing is said."""
    source = tmp_path / "a.py"
    source.write_text("def f():\n    return 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    from graphite.cli import _build_project
    from graphite.config import Config

    _build_project(tmp_path, Config())
    partition = _only_partition(tmp_path)
    first = _entries(partition)
    assert first, "the first build cached nothing"
    graph = json.loads((tmp_path / "graph-out" / "graph.json").read_text(encoding="utf-8"))
    assert graph  # the build is real, not a no-op

    source.write_text("def f():\n    return 2\n", encoding="utf-8")
    capsys.readouterr()
    _build_project(tmp_path, Config())
    reported = capsys.readouterr().out
    second = _entries(partition)

    assert not (first & second), "the previous content's entry survived the rebuild"
    assert second, "the rebuild cached nothing"
    assert "reclaimed 1 unreachable cache entry (" in reported

    capsys.readouterr()
    _build_project(tmp_path, Config())
    assert "reclaimed" not in capsys.readouterr().out
    assert _entries(partition) == second
