"""Python module→file resolution, import-edge binding, and call binding."""
from __future__ import annotations

from pathlib import Path

from graphite.resolve import SourceIndex


def _index(rel_paths: set[str]) -> SourceIndex:
    from graphite.ts_bridge import TypeScriptCompilerIndex

    return SourceIndex(
        root=Path("."),
        rel_paths=frozenset(rel_paths),
        path_aliases=(),
        typescript=TypeScriptCompilerIndex(available=False, reason="unavailable"),
    )


FILES = {
    "src/pkg/__init__.py",
    "src/pkg/ledger.py",
    "src/pkg/tdd.py",
    "src/pkg/commands/__init__.py",
    "src/pkg/commands/drain.py",
    "flat.py",
    "src/json.py",
}


def test_absolute_module_resolves_under_src_root():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "pkg.ledger") == "src/pkg/ledger.py"


def test_absolute_package_resolves_to_init():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "pkg") == "src/pkg/__init__.py"


def test_repo_root_beats_src_root():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "flat") == "flat.py"


def test_relative_single_dot():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "tdd", relative_dots=1) == "src/pkg/tdd.py"


def test_relative_two_dots():
    idx = _index(FILES)
    assert (
        idx.resolve_python_module("src/pkg/commands/drain.py", "ledger", relative_dots=2)
        == "src/pkg/ledger.py"
    )


def test_bare_relative_resolves_package_init():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "", relative_dots=1) == "src/pkg/__init__.py"


def test_stdlib_module_unresolved():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "pathlib") is None


def test_local_file_shadows_stdlib_name():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "json") == "src/json.py"


def test_empty_absolute_module_is_none():
    idx = _index(FILES)
    assert idx.resolve_python_module("src/pkg/pipeline.py", "") is None
