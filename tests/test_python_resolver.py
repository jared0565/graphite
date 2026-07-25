"""Python module→file resolution, import-edge binding, and call binding."""
from __future__ import annotations

from pathlib import Path

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.graph import build_graph
from graphite.ingest import collect_files
from graphite.query import query
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


def _graph_for(tmp_path):
    result = _extract(tmp_path)
    return build_graph(result.nodes, result.edges)


def test_module_attribute_call_binds_cross_module(tmp_path):
    _py_fixture(tmp_path)
    g = _graph_for(tmp_path)
    out = query(g, "callers auto_resolve_tdd")
    ids = [c["id"] for c in out.get("callers", [])]
    assert "src_pkg_pipeline_run" in ids  # tdd.auto_resolve_tdd(...) bound


def test_from_import_aliased_symbol_call_binds(tmp_path):
    _py_fixture(tmp_path)
    g = _graph_for(tmp_path)
    # run() calls the def twice: tdd.auto_resolve_tdd(...) AND art(...) (alias).
    # build_graph merges duplicate edges by incrementing weight -> weight >= 2
    # proves BOTH the module-attr path and the aliased-symbol path bound.
    edge = g.get_edge_data("src_pkg_pipeline_run", "src_pkg_tdd_auto_resolve_tdd")
    assert edge is not None and edge.get("weight", 0) >= 2.0


def test_class_instantiation_binds_to_class_node(tmp_path):
    _py_fixture(tmp_path)
    g = _graph_for(tmp_path)
    # Ledger() in pipeline.run binds to the class node in ledger.py
    assert g.has_edge("src_pkg_pipeline_run", "src_pkg_ledger_ledger")


def test_same_file_call_binding_unchanged(tmp_path):
    _write(
        tmp_path / "solo.py",
        "def helper():\n    return 1\n\ndef main():\n    return helper()\n",
    )
    g = _graph_for(tmp_path)
    out = query(g, "callers helper")
    assert [c["id"] for c in out.get("callers", [])] == ["solo_main"]


def test_aliased_dotted_import_binds_cross_module(tmp_path):
    # `import pkg.ledger as lg` then `lg.scan()` — exercises the
    # aliased_import child of _collect_python_import_maps's import_statement
    # branch (dotted module + alias -> alias_map).
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
        tmp_path / "src" / "pkg" / "consumer.py",
        "import pkg.ledger as lg\n"
        "\n"
        "def run():\n"
        "    return lg.scan()\n",
    )
    g = _graph_for(tmp_path)
    assert g.has_edge("src_pkg_consumer_run", "src_pkg_ledger_scan")


def test_plain_import_binds_cross_module(tmp_path):
    # `import flatmod` (non-dotted, no alias) then `flatmod.func()` —
    # exercises the dotted_name child of _collect_python_import_maps's
    # import_statement branch (non-dotted module -> alias_map keyed by
    # its own name).
    _write(tmp_path / "flatmod.py", "def func():\n    return 1\n")
    _write(
        tmp_path / "consumer.py",
        "import flatmod\n"
        "\n"
        "def run():\n"
        "    return flatmod.func()\n",
    )
    g = _graph_for(tmp_path)
    assert g.has_edge("consumer_run", "flatmod_func")


def test_function_local_import_binds(tmp_path):
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(tmp_path / "src" / "pkg" / "util.py", "def deep():\n    return 1\n")
    _write(
        tmp_path / "src" / "pkg" / "lazy.py",
        "def caller():\n"
        "    from pkg.util import deep\n"
        "    return deep()\n",
    )
    g = _graph_for(tmp_path)
    out = query(g, "callers deep")
    assert "src_pkg_lazy_caller" in [c["id"] for c in out.get("callers", [])]
