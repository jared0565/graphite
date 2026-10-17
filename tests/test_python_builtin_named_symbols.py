"""A Python name the file binds is called, whatever other languages call a builtin (#85).

`_LANGUAGE_BUILTIN_GLOBALS` holds JavaScript, Go and Rust builtins beside
Python's. A bare call of a name on it got no edge, also where the file defines
or imports a class or function of that name, so it had no callers in the graph
and its call site was absent from the `calls` ratio.
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


def _node_id(result, qualname: str) -> str:
    ids = [n["id"] for n in result.nodes if n.get("qualname") == qualname]
    assert len(ids) == 1, f"{qualname}: {ids}"
    return ids[0]


def _call_targets(result, caller_qualname: str) -> set[str]:
    caller = _node_id(result, caller_qualname)
    return {e["target"] for e in result.edges if e["relation"] == "calls" and e["source"] == caller}


_CHECKS = (
    "class Error:\n    def __init__(self, message):\n        self.message = message\n\n\n"
    "class Problem:\n    def __init__(self, message):\n        self.message = message\n\n\n"
    "def make(message):\n    return Problem(message)\n\n\n"
    "def check():\n    return [Error('first'), make('second')]\n"
)


def test_a_class_named_like_a_javascript_builtin_has_its_caller(tmp_path: Path) -> None:
    _write(tmp_path / "checks.py", _CHECKS)
    result = _extract(tmp_path)

    assert _node_id(result, "Error") in _call_targets(result, "check")


def test_a_function_named_like_a_go_builtin_has_its_caller(tmp_path: Path) -> None:
    _write(tmp_path / "checks.py", _CHECKS)
    result = _extract(tmp_path)

    assert _call_targets(result, "check") >= {_node_id(result, "make")}


def test_an_imported_name_that_is_on_the_list_binds_to_its_definition(tmp_path: Path) -> None:
    _write(tmp_path / "lib.py", "def make(message):\n    return message\n")
    _write(tmp_path / "user.py", "from lib import make\n\n\ndef run():\n    return make('x')\n")
    result = _extract(tmp_path)

    assert _call_targets(result, "run") == {_node_id(result, "make")}


def test_a_name_on_the_list_that_nothing_binds_still_gets_no_edge(tmp_path: Path) -> None:
    _write(tmp_path / "plain.py", "def run(items):\n    return len(items), Error('x')\n")
    result = _extract(tmp_path)

    assert _call_targets(result, "run") == set()
