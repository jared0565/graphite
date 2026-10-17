"""Every extractor records a definition's line 1-based, the first line included.

`source_location` (`L<n>`) is what a reader is pointed at, and an anonymous
TS/JS function's id carries its line and column. Only the Python extractor's
lines were held by a test: an off-by-one in the TS/JS, Go or Rust line, or in
the anonymous column, passed the whole suite. The first-line cases are the
row-0 / column-0 positions that an `if node.start_point else 1` fallback
appeared to guard; a node's start point is set there too.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.ingest import collect_files

FILES = {
    "web/app.ts": (
        "function first() { return 1; }\n"
        "\n"
        "export const later = () => first();\n"
        "[1].map((x) => first());\n"
    ),
    "svc/main.go": (
        "package svc\n"
        "\n"
        "func Start() int { return 1 }\n"
    ),
    "core/lib.rs": (
        "fn helper() -> i32 { 1 }\n"
        "\n"
        "pub fn start() -> i32 { helper() }\n"
    ),
    "py/mod.py": (
        "def first():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def later():\n"
        "    return first()\n"
    ),
}


def _nodes(tmp_path: Path) -> list[dict[str, Any]]:
    for rel, text in FILES.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8", newline="\n")
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite", typescript_resolver="disabled")
    return list(extract_all(collect_files(tmp_path, cfg), cfg).nodes)


def _line_of(nodes: list[dict[str, Any]], rel: str, name: str) -> str:
    found = [n for n in nodes if n.get("source_file") == rel and n.get("name") == name and n.get("kind") != "file"]
    assert len(found) == 1, (rel, name, found)
    return found[0]["source_location"]


def test_each_extractor_records_a_definition_s_line_from_one(tmp_path: Path) -> None:
    nodes = _nodes(tmp_path)

    assert _line_of(nodes, "web/app.ts", "first") == "L1"
    assert _line_of(nodes, "web/app.ts", "later") == "L3"
    assert _line_of(nodes, "svc/main.go", "Start") == "L3"
    assert _line_of(nodes, "core/lib.rs", "helper") == "L1"
    assert _line_of(nodes, "core/lib.rs", "start") == "L3"
    assert _line_of(nodes, "py/mod.py", "first") == "L1"
    assert _line_of(nodes, "py/mod.py", "later") == "L5"


def test_an_anonymous_function_s_id_names_its_line_and_column_from_one(tmp_path: Path) -> None:
    anonymous = [n for n in _nodes(tmp_path) if n.get("anonymous") and n.get("source_file") == "web/app.ts"]

    # `(x) => first()` starts at row 3, column 8 of the file: line 4, column 9.
    assert len(anonymous) == 1, anonymous
    assert anonymous[0]["source_location"] == "L4"
    assert anonymous[0]["id"].endswith("_l4_c9"), anonymous[0]["id"]
