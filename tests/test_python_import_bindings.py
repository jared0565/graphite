"""Per-statement Python import bindings (#71).

`_python_import_bindings` is the per-statement half of what used to be the
file-wide `_collect_python_import_maps`: it says what each name ONE import
statement binds denotes. Where the binding lives is the scope walk's business
(`extract/pyscope.py`); flattening every import into file-wide maps was #71.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.config import Config
from graphite.extract.ast import _LOADER, _file_node_id, _make_id, _python_import_bindings
from graphite.ingest import collect_files
from graphite.resolve import SourceIndex

REPO = {
    "flatmod.py": "def func():\n    return 1\n",
    "pkg/__init__.py": "",
    "pkg/sub.py": "def f():\n    return 1\n\n\ndef g():\n    return 2\n",
}
FLAT = _file_node_id("flatmod.py")
SUB = _file_node_id("pkg/sub.py")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _bindings(tmp_path: Path, statement: str, rel: str = "m.py", *, indexed: bool = True):
    for path, text in REPO.items():
        _write(tmp_path / path, text)
    _write(tmp_path / rel, statement + "\n")
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite", typescript_resolver="disabled")
    index = SourceIndex.from_entries(collect_files(tmp_path, cfg), cfg) if indexed else None
    tree = _LOADER.parser("python").parse((statement + "\n").encode())
    (node,) = [c for c in tree.root_node.children if c.type in ("import_statement", "import_from_statement")]
    return _python_import_bindings(node, rel, index)


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("import flatmod", [("flatmod", "alias", FLAT)]),
        ("import json", [("json", "external", None)]),
        ("import pkg.sub", [("pkg", "alias", None)]),
        ("import nopkg.sub", [("nopkg", "external", None)]),
        ("import pkg.sub as s", [("s", "alias", SUB)]),
        ("import json as j", [("j", "external", None)]),
        ("import flatmod, json", [("flatmod", "alias", FLAT), ("json", "external", None)]),
        ("from pkg import sub", [("sub", "alias", SUB)]),
        ("from pkg.sub import f, g as h", [("f", "symbol", _make_id(SUB, "f")), ("h", "symbol", _make_id(SUB, "g"))]),
        ("from pkg.sub import (\n    f,\n    g,\n)", [("f", "symbol", _make_id(SUB, "f")), ("g", "symbol", _make_id(SUB, "g"))]),
        ("from json import loads", [("loads", "external", None)]),
        ("from pkg.sub import *", []),
    ],
    ids=[
        "module", "external-module", "dotted-binds-root", "dotted-external-root", "dotted-alias",
        "external-alias", "two-modules", "from-submodule", "from-symbols", "parenthesized",
        "from-external", "star",
    ],
)
def test_each_name_an_import_binds(tmp_path, statement, expected):
    assert _bindings(tmp_path, statement) == expected


def test_relative_imports_bind_through_the_package(tmp_path):
    assert _bindings(tmp_path, "from . import sub", rel="pkg/m.py") == [("sub", "alias", SUB)]
    assert _bindings(tmp_path, "from .sub import f", rel="pkg/m.py") == [("f", "symbol", _make_id(SUB, "f"))]


def test_without_a_source_index_every_binding_is_unknown(tmp_path):
    assert _bindings(tmp_path, "from x import a, b as c", indexed=False) == [
        ("a", "unknown", None),
        ("c", "unknown", None),
    ]
    assert _bindings(tmp_path, "import a.b", indexed=False) == [("a", "unknown", None)]
