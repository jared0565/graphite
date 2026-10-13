"""A cached extraction belongs to the file it was taken from (#74).

What `extract_file` caches names the file: every node id and every
`source_file` in it is built from the path. The entry was keyed on the file's
bytes and its language alone, so two files with the same bytes shared one
entry, and the second to ask was answered with the first one's nodes and got
none of its own.

Two things followed. A file with a twin lost its node, its definitions and
their edges to that twin: on every build once the cache was warm, and by a
race between worker threads on a cold one, which is why one tree built twice
gave two node sets. And a file moved with its bytes unchanged kept its old
path in the graph.

Every extraction here runs on one worker and on a real cache. The collision is
then decided rather than raced: the second of two twins reads what the first
has just written.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite.cache import Cache
from graphite.config import Config
from graphite.extract.ast import ExtractionResult, extract_all
from graphite.ingest import collect_files

ENGINE = "a" * 64

PYTHON = "def helper():\n    return 1\n"
GO = "package g\n\nfunc Helper() int { return 1 }\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(root: Path) -> ExtractionResult:
    """One build's extraction of `root`, on the cache the last one left there."""
    cfg = Config(workers=1, cache_dir=root / ".cache" / "graphite")
    cache = Cache(cfg.cache_dir, cfg.cache_version, engine=ENGINE)
    return extract_all(collect_files(root, cfg), cfg, cache)


def _files(result: ExtractionResult) -> set[str]:
    return {node["source_file"] for node in result.nodes if node["kind"] == "file"}


def _definitions(result: ExtractionResult) -> set[tuple[str, str]]:
    return {(node["source_file"], node["name"]) for node in result.nodes if node["kind"] != "file"}


TWINS = [
    pytest.param("a/same.html", "b/other.html", "<p>{{ form }}</p>\n", id="a-template"),
    pytest.param("notes/a.txt", "notes/b.txt", "the same words\n", id="a-text-file"),
    pytest.param("pkg1/__init__.py", "pkg2/__init__.py", "", id="an-empty-package-file"),
    pytest.param("m1/util.py", "m2/util2.py", PYTHON, id="python"),
    pytest.param("g1/first.go", "g2/second.go", GO, id="go"),
]


@pytest.mark.parametrize(("first", "second", "text"), TWINS)
def test_two_files_with_the_same_bytes_each_keep_their_node(tmp_path: Path, first: str, second: str, text: str) -> None:
    _write(tmp_path / first, text)
    _write(tmp_path / second, text)

    assert _files(_extract(tmp_path)) == {first, second}


@pytest.mark.parametrize(("first", "second", "text"), TWINS)
def test_a_second_build_names_the_files_the_first_did(tmp_path: Path, first: str, second: str, text: str) -> None:
    """The issue as filed: nothing changes between two builds and the graph does."""
    _write(tmp_path / first, text)
    _write(tmp_path / second, text)

    cold = _extract(tmp_path)
    warm = _extract(tmp_path)

    assert _files(warm) == {first, second}
    assert sorted(node["id"] for node in warm.nodes) == sorted(node["id"] for node in cold.nodes)


@pytest.mark.parametrize(
    ("first", "second", "text", "name"),
    [
        pytest.param("m1/util.py", "m2/util2.py", PYTHON, "helper", id="python"),
        pytest.param("g1/first.go", "g2/second.go", GO, "Helper", id="go"),
    ],
)
def test_a_definition_in_each_of_two_identical_files_is_kept(
    tmp_path: Path, first: str, second: str, text: str, name: str
) -> None:
    """Not only the file node went: what the file defines went with it, and the
    edge from the file to it. `callers helper` then answered for one file."""
    _write(tmp_path / first, text)
    _write(tmp_path / second, text)

    result = _extract(tmp_path)

    assert _definitions(result) == {(first, name), (second, name)}
    file_of = {node["id"]: node["source_file"] for node in result.nodes}
    tied = {(file_of[edge["source"]], file_of[edge["target"]]) for edge in result.edges}
    assert tied == {(first, first), (second, second)}


def test_an_import_of_each_of_two_empty_packages_lands_on_a_node(tmp_path: Path) -> None:
    """Empty `__init__.py` files are the twins most trees have. With one node
    for all of them, an import of any other package pointed at nothing."""
    _write(tmp_path / "pkg1" / "__init__.py", "")
    _write(tmp_path / "pkg2" / "__init__.py", "")
    _write(tmp_path / "app.py", "import pkg1\nimport pkg2\n")

    result = _extract(tmp_path)

    nodes = {node["id"] for node in result.nodes}
    imported = {edge["target"] for edge in result.edges if edge["relation"] == "imports"}
    assert len(imported) == 2, imported
    assert imported <= nodes


@pytest.mark.parametrize(
    ("before", "after", "text"),
    [
        pytest.param("c/unique.html", "c/renamed.html", "<p>one of a kind</p>\n", id="a-template"),
        pytest.param("g/first.go", "g/second.go", GO, id="go"),
    ],
)
def test_a_file_moved_with_its_bytes_unchanged_is_named_where_it_is_now(
    tmp_path: Path, before: str, after: str, text: str
) -> None:
    """A Python or TypeScript move re-extracts anyway, because the set of paths
    is in their key (#2). Nothing else had that, so the graph went on naming a
    path that no longer existed."""
    _write(tmp_path / before, text)
    assert _files(_extract(tmp_path)) == {before}

    (tmp_path / before).rename(tmp_path / after)

    assert {node["source_file"] for node in _extract(tmp_path).nodes} == {after}


def test_two_python_files_that_swap_their_bytes_are_each_read_again(tmp_path: Path) -> None:
    """The one move the set of paths does not see: the same two paths, each now
    holding what the other held."""
    _write(tmp_path / "a.py", "def one():\n    return 1\n")
    _write(tmp_path / "b.py", "def two():\n    return 2\n")
    assert _definitions(_extract(tmp_path)) == {("a.py", "one"), ("b.py", "two")}

    _write(tmp_path / "a.py", "def two():\n    return 2\n")
    _write(tmp_path / "b.py", "def one():\n    return 1\n")

    assert _definitions(_extract(tmp_path)) == {("a.py", "two"), ("b.py", "one")}


def test_each_of_two_identical_files_has_an_entry_of_its_own(tmp_path: Path) -> None:
    _write(tmp_path / "a" / "same.html", "<p>{{ form }}</p>\n")
    _write(tmp_path / "b" / "other.html", "<p>{{ form }}</p>\n")

    _extract(tmp_path)

    assert len(list((tmp_path / ".cache" / "graphite").rglob("*.json"))) == 2


def test_an_unchanged_file_is_still_answered_from_the_cache(tmp_path: Path) -> None:
    """The control. A path in the key must not turn every read into a miss:
    the entry the first build wrote is marked on disk, and the mark comes back."""
    _write(tmp_path / "a" / "page.html", "<p>one</p>\n")
    _extract(tmp_path)
    entries = list((tmp_path / ".cache" / "graphite").rglob("*.json"))
    assert len(entries) == 1, entries
    cached = json.loads(entries[0].read_text(encoding="utf-8"))
    cached["nodes"][0]["name"] = "served from the cache"
    entries[0].write_text(json.dumps(cached), encoding="utf-8")

    again = _extract(tmp_path)

    assert [node["name"] for node in again.nodes] == ["served from the cache"]


def test_a_file_whose_bytes_change_is_read_again(tmp_path: Path) -> None:
    """The other control. The path joins the bytes in the key; it does not
    replace them."""
    _write(tmp_path / "a.py", "def one():\n    return 1\n")
    assert _definitions(_extract(tmp_path)) == {("a.py", "one")}

    _write(tmp_path / "a.py", "def two():\n    return 2\n")

    assert _definitions(_extract(tmp_path)) == {("a.py", "two")}


def test_the_same_tree_built_twice_gives_the_same_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the real build and its worker threads. The first build is cold
    and the second is warm, which is where every twin read one entry."""
    from graphite.cli import _build_project

    written: set[str] = set()
    for first, second, text in (param.values for param in TWINS):
        _write(tmp_path / first, text)
        _write(tmp_path / second, text)
        written |= {first, second}
    monkeypatch.chdir(tmp_path)

    def built() -> list[tuple[str, str]]:
        _build_project(tmp_path, Config())
        graph = json.loads((tmp_path / "graph-out" / "graph.json").read_text(encoding="utf-8"))
        return sorted((node["source_file"], node["id"]) for node in graph["nodes"])

    cold = built()
    warm = built()

    assert warm == cold
    assert {source_file for source_file, _ in warm} == written
