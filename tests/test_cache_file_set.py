"""AST cache must depend on the repo file set, not on file content alone (#2).

Import resolution runs at extraction time, so a cached extraction embeds
conclusions about which sibling modules existed when it was written. The
importer's own bytes never change when a sibling appears, so a content-keyed
cache served a stale answer indefinitely -- until the importer happened to be
edited or the cache version was bumped.
"""
from __future__ import annotations

from pathlib import Path

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.ingest import collect_files
from graphite.resolve import SourceIndex


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(tmp_path: Path):
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite")
    return extract_all(collect_files(tmp_path, cfg), cfg)


def _import_targets(result, source_file: str) -> set[str]:
    """Import edge TARGETS, which is what a file-set change actually moves.

    Confidence does not discriminate here: `from pkg import helper` binds to
    the package `__init__` as EXACT_IMPORT whether or not the submodule exists.
    What changes is whether the submodule file node is also bound.
    """
    return {
        e["target"]
        for e in result.edges
        if e["relation"] == "imports" and e.get("source_file") == source_file
    }


def _seed_package(tmp_path: Path) -> Path:
    _write(tmp_path / "pkg" / "__init__.py", "")
    importer = tmp_path / "pkg" / "app.py"
    _write(importer, "from pkg import helper\n\n\ndef run():\n    return helper.go()\n")
    return importer


def test_adding_a_sibling_module_rebinds_an_unchanged_importer(tmp_path: Path) -> None:
    importer = _seed_package(tmp_path)

    before = _import_targets(_extract(tmp_path), "pkg/app.py")
    assert "pkg_helper_py" not in before, f"fixture must start unbound: {before}"

    original_bytes = importer.read_bytes()
    _write(tmp_path / "pkg" / "helper.py", "def go():\n    return 1\n")
    # The importer is deliberately untouched -- that is the whole defect.
    assert importer.read_bytes() == original_bytes

    after = _import_targets(_extract(tmp_path), "pkg/app.py")
    assert "pkg_helper_py" in after, (
        f"unchanged importer served a stale cached resolution: {after}"
    )


def test_removing_a_sibling_module_unbinds_an_unchanged_importer(tmp_path: Path) -> None:
    """The symmetric direction: a resolution that was true must stop being served."""
    importer = _seed_package(tmp_path)
    _write(tmp_path / "pkg" / "helper.py", "def go():\n    return 1\n")

    assert "pkg_helper_py" in _import_targets(_extract(tmp_path), "pkg/app.py")

    original_bytes = importer.read_bytes()
    (tmp_path / "pkg" / "helper.py").unlink()
    assert importer.read_bytes() == original_bytes

    after = _import_targets(_extract(tmp_path), "pkg/app.py")
    assert "pkg_helper_py" not in after, (
        f"removed sibling still served as resolved from cache: {after}"
    )


def test_file_set_digest_is_order_independent_and_change_sensitive() -> None:
    from graphite.resolve import _rel_path_set_digest

    a = _rel_path_set_digest(frozenset({"src/a.py", "src/b.py"}))
    b = _rel_path_set_digest(frozenset({"src/b.py", "src/a.py"}))
    c = _rel_path_set_digest(frozenset({"src/a.py", "src/b.py", "src/c.py"}))

    assert a == b, "a set digest must not depend on insertion order"
    assert a != c, "adding a file must change the digest"


def test_unchanged_file_set_still_hits_the_cache(tmp_path: Path) -> None:
    """Guard against throwing the cache away entirely: identical inputs reuse it."""
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "app.py", "def run():\n    return 1\n")

    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite")
    entries = collect_files(tmp_path, cfg)
    index = SourceIndex.from_entries(entries, cfg)
    first = index.file_set_digest()

    entries_again = collect_files(tmp_path, cfg)
    index_again = SourceIndex.from_entries(entries_again, cfg)

    assert index_again.file_set_digest() == first, (
        "an unchanged repo must produce a stable digest, or every build re-extracts"
    )
