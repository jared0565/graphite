"""Python module→file resolution, import-edge binding, and call binding."""
from __future__ import annotations

from pathlib import Path

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.ingest import collect_files
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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(tmp_path: Path):
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite")
    entries = collect_files(tmp_path, cfg)
    return extract_all(entries, cfg)


def _py_fixture(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(
        tmp_path / "src" / "pkg" / "ledger.py",
        "class Ledger:\n"
        "    def record_run(self, run_id):\n"
        "        return [run_id]\n"
        "\n"
        "def scan():\n"
        "    return None\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "tdd.py",
        "def auto_resolve_tdd(run_id):\n"
        "    return run_id\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "pipeline.py",
        "import json\n"
        "from pkg import tdd\n"
        "from pkg.ledger import Ledger\n"
        "from .tdd import auto_resolve_tdd as art\n"
        "\n"
        "def run(run_id):\n"
        "    tdd.auto_resolve_tdd(run_id)\n"
        "    art(run_id)\n"
        "    ledger = Ledger()\n"
        "    ledger.record_run(run_id)\n"
        "    return json.dumps({})\n",
    )


def _import_edges(result, source_file):
    return [
        e for e in result.edges
        if e.get("relation") == "imports" and e.get("source_file") == source_file
    ]


def test_import_edges_shapes(tmp_path):
    _py_fixture(tmp_path)
    result = _extract(tmp_path)
    edges = _import_edges(result, "src/pkg/pipeline.py")
    by_target = {(e["target"], e["confidence"]) for e in edges}
    # stdlib: phantom + EXTERNAL_IMPORT
    assert ("json", "EXTERNAL_IMPORT") in by_target
    # from pkg import tdd -> module edge for pkg (resolved to package __init__)
    assert ("src_pkg_init", "EXACT_IMPORT") in by_target
    # from pkg.ledger import Ledger -> ONE edge for the module, file-node target
    assert ("src_pkg_ledger", "EXACT_IMPORT") in by_target
    # relative .tdd -> file node
    assert ("src_pkg_tdd", "EXACT_IMPORT") in by_target
    # imported NAMES never make import edges
    all_targets = {e["target"] for e in edges}
    assert "ledger" not in all_targets  # no dotted-module phantom for resolved module
    assert not any(t.endswith("auto_resolve_tdd") for t in all_targets)
    assert not any(t == "art" for t in all_targets)


def test_import_edge_count_is_one_per_module(tmp_path):
    _py_fixture(tmp_path)
    result = _extract(tmp_path)
    edges = _import_edges(result, "src/pkg/pipeline.py")
    assert len(edges) == 4  # json, pkg, pkg.ledger, .tdd
