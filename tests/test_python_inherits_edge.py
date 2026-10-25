"""An `inherits` edge ends at the class the base names (#86).

The far end used to be a node made from the base's bare name, of kind
`unknown`, also when the class is defined in the same file, and one node for
every class of that name.
"""
from __future__ import annotations

from pathlib import Path

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.ingest import collect_files


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(root: Path):
    cfg = Config(workers=1, cache_dir=root / ".cache" / "graphite")
    return extract_all(collect_files(root, cfg), cfg)


def _class_id(result, source_file: str, qualname: str) -> str:
    ids = [n["id"] for n in result.nodes if n.get("qualname") == qualname and n["source_file"] == source_file]
    assert len(ids) == 1, f"{source_file}:{qualname}: {ids}"
    return ids[0]


def _inherits(result, source_id: str) -> set[str]:
    return {e["target"] for e in result.edges if e["relation"] == "inherits" and e["source"] == source_id}


_SHAPES = "class Base:\n    def area(self):\n        return 0\n\n\nclass Square(Base):\n    def area(self):\n        return 4\n"


def test_a_base_defined_in_the_same_file_is_the_end_of_the_edge(tmp_path: Path) -> None:
    _write(tmp_path / "shapes.py", _SHAPES)
    result = _extract(tmp_path)

    assert _inherits(result, _class_id(result, "shapes.py", "Square")) == {_class_id(result, "shapes.py", "Base")}


def test_a_base_imported_by_name_ends_at_the_file_that_defines_it(tmp_path: Path) -> None:
    _write(tmp_path / "shapes.py", _SHAPES)
    _write(tmp_path / "other.py", "from shapes import Base\n\n\nclass Circle(Base):\n    pass\n")
    result = _extract(tmp_path)

    assert _inherits(result, _class_id(result, "other.py", "Circle")) == {_class_id(result, "shapes.py", "Base")}


def test_a_base_a_package_re_exports_ends_at_the_file_that_defines_it(tmp_path: Path) -> None:
    """`from pkg import Base` names `pkg/__init__.py`, which only re-exports it:
    the edge ends where `Base` is defined (#87), not at a node of the package."""
    _write(tmp_path / "pkg" / "__init__.py", "from pkg.shapes import Base\n")
    _write(tmp_path / "pkg" / "shapes.py", _SHAPES)
    _write(tmp_path / "other.py", "from pkg import Base\n\n\nclass Circle(Base):\n    pass\n")
    result = _extract(tmp_path)

    assert _inherits(result, _class_id(result, "other.py", "Circle")) == {_class_id(result, "pkg/shapes.py", "Base")}


def test_two_bases_of_one_name_are_two_ends(tmp_path: Path) -> None:
    _write(tmp_path / "one.py", "class Base:\n    pass\n\n\nclass A(Base):\n    pass\n")
    _write(tmp_path / "two.py", "class Base:\n    pass\n\n\nclass B(Base):\n    pass\n")
    result = _extract(tmp_path)

    first = _inherits(result, _class_id(result, "one.py", "A"))
    second = _inherits(result, _class_id(result, "two.py", "B"))
    assert first == {_class_id(result, "one.py", "Base")}
    assert second == {_class_id(result, "two.py", "Base")}


def test_a_base_that_names_a_module_does_not_end_at_the_module_file(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "mod.py", "VALUE = 1\n")
    _write(tmp_path / "user.py", "from pkg import mod\n\n\nclass Odd(mod):\n    pass\n")
    result = _extract(tmp_path)
    file_ids = {n["id"] for n in result.nodes if n["kind"] == "file"}

    targets = _inherits(result, _class_id(result, "user.py", "Odd"))
    assert targets and targets.isdisjoint(file_ids)


def test_a_base_the_source_does_not_state_keeps_a_placeholder(tmp_path: Path) -> None:
    _write(
        tmp_path / "mixed.py",
        "import enum\n\n\nclass Color(enum.Enum):\n    pass\n\n\nclass Plain(dict):\n    pass\n\n\nclass Unbound(Missing):\n    pass\n",
    )
    result = _extract(tmp_path)
    class_ids = {n["id"] for n in result.nodes if n["kind"] == "class"}

    for name in ("Plain", "Unbound"):
        targets = _inherits(result, _class_id(result, "mixed.py", name))
        assert targets and targets.isdisjoint(class_ids), (name, targets)
