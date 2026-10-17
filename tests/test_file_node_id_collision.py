"""Two paths that sanitise to one file node id keep a node each (#82).

`a/b.py`, `a_b.py` and `a/b_py` all slug to `a_b_py`. The graph held one file
node for them and hung the others' definitions off it. Only paths that collide
in the tree being built get a marker, so every other id stays what it was.
"""
from __future__ import annotations

from pathlib import Path

from graphite.cache import Cache
from graphite.config import Config
from graphite.extract.ast import _file_node_id, extract_all
from graphite.ingest import collect_files


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(root: Path, cache: Cache | None = None):
    cfg = Config(workers=1, cache_dir=root / ".cache" / "graphite")
    return extract_all(collect_files(root, cfg), cfg, cache)


def _cache(root: Path) -> Cache:
    cfg = Config(workers=1, cache_dir=root / ".cache" / "graphite")
    return Cache(cfg.cache_dir, cfg.cache_version, engine="test-engine")


def _file_nodes(result) -> dict[str, str]:
    """source_file -> file node id, failing if two files share an id."""
    by_path = {n["source_file"]: n["id"] for n in result.nodes if n["kind"] == "file"}
    assert len(set(by_path.values())) == len(by_path), f"file nodes share an id: {by_path}"
    return by_path


def _contained_in(result, qualname: str) -> str:
    """The id of the node that `contains` the definition with this qualname."""
    (definition,) = [n["id"] for n in result.nodes if n.get("qualname") == qualname]
    (parent,) = [e["source"] for e in result.edges if e["relation"] == "contains" and e["target"] == definition]
    return parent


def _colliding_tree(root: Path) -> None:
    _write(root / "a" / "b.py", "def one():\n    return 1\n")
    _write(root / "a_b.py", "def two():\n    return 2\n")
    _write(root / "notes.txt", "first\n")
    _write(root / "notes_txt", "second\n")


def test_colliding_paths_each_get_a_file_node(tmp_path: Path) -> None:
    _colliding_tree(tmp_path)
    files = _file_nodes(_extract(tmp_path))

    assert set(files) == {"a/b.py", "a_b.py", "notes.txt", "notes_txt"}


def test_a_definition_hangs_off_its_own_file(tmp_path: Path) -> None:
    _colliding_tree(tmp_path)
    result = _extract(tmp_path)
    files = _file_nodes(result)

    assert _contained_in(result, "one") == files["a/b.py"]
    assert _contained_in(result, "two") == files["a_b.py"]


def test_a_path_that_collides_with_nothing_keeps_its_plain_id(tmp_path: Path) -> None:
    _colliding_tree(tmp_path)
    _write(tmp_path / "lonely.py", "def x():\n    return 0\n")
    files = _file_nodes(_extract(tmp_path))

    assert files["lonely.py"] == _file_node_id("lonely.py")


def test_a_tree_without_collisions_is_unchanged(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "mod.py", "def f():\n    return 1\n")
    _write(tmp_path / "other.py", "def g():\n    return 2\n")
    files = _file_nodes(_extract(tmp_path))

    assert files == {"pkg/mod.py": _file_node_id("pkg/mod.py"), "other.py": _file_node_id("other.py")}


def test_the_ids_do_not_depend_on_the_order_files_are_listed(tmp_path: Path) -> None:
    _colliding_tree(tmp_path)
    first = _file_nodes(_extract(tmp_path))
    second = _file_nodes(_extract(tmp_path))

    assert first == second


def test_an_import_of_a_colliding_file_lands_on_that_file(tmp_path: Path) -> None:
    _write(tmp_path / "a" / "b.py", "def one():\n    return 1\n")
    _write(tmp_path / "a_b.py", "def two():\n    return 2\n")
    _write(tmp_path / "main.py", "import a_b\n\n\ndef run():\n    return a_b.two()\n")
    result = _extract(tmp_path)
    files = _file_nodes(result)

    targets = {e["target"] for e in result.edges if e["relation"] == "imports" and e["source_file"] == "main.py"}
    assert targets == {files["a_b.py"]}
    (two,) = [n["id"] for n in result.nodes if n.get("qualname") == "two"]
    (run,) = [n["id"] for n in result.nodes if n.get("qualname") == "run"]
    assert {e["target"] for e in result.edges if e["relation"] == "calls" and e["source"] == run} == {two}


def test_a_name_re_exported_from_a_colliding_file_binds_to_its_definition(tmp_path: Path) -> None:
    """A package re-export is followed to its file (#87), and that file's id is the marked one."""
    _write(tmp_path / "pkg" / "__init__.py", "from pkg.a.b import one\n")
    _write(tmp_path / "pkg" / "a" / "__init__.py", "")
    _write(tmp_path / "pkg" / "a" / "b.py", "def one():\n    return 1\n")
    _write(tmp_path / "pkg" / "a_b.py", "def two():\n    return 2\n")
    _write(tmp_path / "main.py", "from pkg import one\n\n\ndef run():\n    return one()\n")
    result = _extract(tmp_path)

    assert _file_nodes(result)["pkg/a/b.py"] != _file_node_id("pkg/a/b.py")
    (one,) = [n["id"] for n in result.nodes if n.get("qualname") == "one"]
    (run,) = [n["id"] for n in result.nodes if n.get("qualname") == "run"]
    assert {e["target"] for e in result.edges if e["relation"] == "calls" and e["source"] == run} == {one}


def test_an_unchanged_importer_follows_a_new_collision_through_the_cache(tmp_path: Path) -> None:
    _write(tmp_path / "a_b.py", "def two():\n    return 2\n")
    _write(tmp_path / "main.py", "import a_b\n")
    cache = _cache(tmp_path)

    before = _extract(tmp_path, cache)
    assert _file_nodes(before)["a_b.py"] == _file_node_id("a_b.py")

    importer_bytes = (tmp_path / "main.py").read_bytes()
    _write(tmp_path / "a" / "b.py", "def one():\n    return 1\n")
    assert (tmp_path / "main.py").read_bytes() == importer_bytes

    after = _extract(tmp_path, cache)
    files = _file_nodes(after)
    targets = {e["target"] for e in after.edges if e["relation"] == "imports" and e["source_file"] == "main.py"}
    assert targets == {files["a_b.py"]}
    assert files["a_b.py"] != _file_node_id("a_b.py")


def test_a_rust_importer_follows_a_new_collision_through_the_cache(tmp_path: Path) -> None:
    """Rust keys carry no file-set digest, so the marked ids must reach the key themselves."""
    _write(tmp_path / "Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.0"\n')
    _write(tmp_path / "src" / "lib.rs", "mod a_b;\n")
    _write(tmp_path / "src" / "a_b.rs", "pub fn two() {}\n")
    cache = _cache(tmp_path)

    before = _extract(tmp_path, cache)
    assert _file_nodes(before)["src/a_b.rs"] == _file_node_id("src/a_b.rs")

    importer_bytes = (tmp_path / "src" / "lib.rs").read_bytes()
    _write(tmp_path / "src" / "a" / "b.rs", "pub fn one() {}\n")
    assert (tmp_path / "src" / "lib.rs").read_bytes() == importer_bytes

    after = _extract(tmp_path, cache)
    files = _file_nodes(after)
    targets = {e["target"] for e in after.edges if e["relation"] == "imports" and e["source_file"] == "src/lib.rs"}
    assert targets == {files["src/a_b.rs"]}
    assert files["src/a_b.rs"] != _file_node_id("src/a_b.rs")


def test_a_method_call_inside_a_colliding_file_binds_to_the_method_of_that_file(tmp_path: Path) -> None:
    """The dispatch gate asks which file the caller is in; that id is read off its node."""
    _write(tmp_path / "a" / "b.py", "class A:\n    def go(self):\n        return 1\n")
    _write(
        tmp_path / "a_b.py",
        "class B:\n    def go(self):\n        return 2\n\n\ndef use(thing):\n    return thing.go()\n",
    )
    result = _extract(tmp_path)

    (use,) = [n["id"] for n in result.nodes if n.get("qualname") == "use"]
    (b_go,) = [n["id"] for n in result.nodes if n.get("qualname") == "B.go"]
    assert {e["target"] for e in result.edges if e["relation"] == "calls" and e["source"] == use} == {b_go}


def test_a_javascript_import_of_a_colliding_file_lands_on_that_file(tmp_path: Path) -> None:
    _write(tmp_path / "lib" / "util.js", "export function one() { return 1 }\n")
    _write(tmp_path / "lib_util.js", "export function two() { return 2 }\n")
    _write(tmp_path / "main.js", "import { two } from './lib_util.js'\n\nexport function run() { return two() }\n")
    result = _extract(tmp_path)
    files = _file_nodes(result)

    targets = {e["target"] for e in result.edges if e["relation"] == "imports" and e["source_file"] == "main.js"}
    assert targets == {files["lib_util.js"]}
    (two,) = [n["id"] for n in result.nodes if n["kind"] == "function" and n["name"] == "two"]
    (run,) = [n["id"] for n in result.nodes if n["kind"] == "function" and n["name"] == "run"]
    assert {e["target"] for e in result.edges if e["relation"] == "calls" and e["source"] == run} == {two}
