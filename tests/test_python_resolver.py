"""Python module→file resolution, import-edge binding, and call binding."""
from __future__ import annotations

from pathlib import Path

from graphite.config import Config
from graphite.extract.ast import _file_node_id, extract_all
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


def _submodule_fixture(tmp_path: Path) -> None:
    """Shared fixture for the from-package-submodule idiom table (spec §9).

    pkg/__init__.py          (empty)
    pkg/pipeline.py          (def run(): ...)
    pkg/runners/__init__.py  (empty)
    pkg/runners/tests.py     (def go(): ...)
    """
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "pipeline.py", "def run():\n    return 1\n")
    _write(tmp_path / "pkg" / "runners" / "__init__.py", "")
    _write(tmp_path / "pkg" / "runners" / "tests.py", "def go():\n    return 1\n")


def test_from_import_submodule_binds_both_package_and_submodule(tmp_path):
    # idiom 1: `from pkg import pipeline` — this is issue #7 in miniature
    # (`from aramid import pipeline` bound to the package __init__ ONLY).
    # Both the kept base-module edge AND the new submodule edge must exist.
    _submodule_fixture(tmp_path)
    _write(tmp_path / "consumer.py", "from pkg import pipeline\n")
    result = _extract(tmp_path)
    edges = _import_edges(result, "consumer.py")
    by_target = {(e["target"], e["confidence"]) for e in edges}
    assert (_file_node_id("pkg/__init__.py"), "EXACT_IMPORT") in by_target
    assert (_file_node_id("pkg/pipeline.py"), "EXACT_IMPORT") in by_target


def test_from_import_multiple_names_bind_each_submodule(tmp_path):
    # idiom 2: `from pkg import pipeline, runners` — two submodules, both
    # bind alongside the kept package edge.
    _submodule_fixture(tmp_path)
    _write(tmp_path / "consumer.py", "from pkg import pipeline, runners\n")
    result = _extract(tmp_path)
    edges = _import_edges(result, "consumer.py")
    by_target = {(e["target"], e["confidence"]) for e in edges}
    assert (_file_node_id("pkg/__init__.py"), "EXACT_IMPORT") in by_target
    assert (_file_node_id("pkg/pipeline.py"), "EXACT_IMPORT") in by_target
    assert (_file_node_id("pkg/runners/__init__.py"), "EXACT_IMPORT") in by_target


def test_from_dotted_import_aliased_submodule_binds(tmp_path):
    # idiom 3: `from pkg.runners import tests as t` — dotted base module PLUS
    # an aliased submodule name; both the base (pkg/runners/__init__.py) and
    # the resolved submodule (pkg/runners/tests.py) bind.
    _submodule_fixture(tmp_path)
    _write(tmp_path / "consumer.py", "from pkg.runners import tests as t\n")
    result = _extract(tmp_path)
    edges = _import_edges(result, "consumer.py")
    by_target = {(e["target"], e["confidence"]) for e in edges}
    assert (_file_node_id("pkg/runners/__init__.py"), "EXACT_IMPORT") in by_target
    assert (_file_node_id("pkg/runners/tests.py"), "EXACT_IMPORT") in by_target


def test_relative_bare_from_import_binds_submodule(tmp_path):
    # idiom 4: `from . import pipeline` from a sibling file INSIDE the
    # package (bare relative, no dotted module name) — must resolve the
    # package __init__ AND the sibling submodule.
    _submodule_fixture(tmp_path)
    _write(tmp_path / "pkg" / "sibling_user.py", "from . import pipeline\n")
    result = _extract(tmp_path)
    edges = _import_edges(result, "pkg/sibling_user.py")
    by_target = {(e["target"], e["confidence"]) for e in edges}
    assert (_file_node_id("pkg/__init__.py"), "EXACT_IMPORT") in by_target
    assert (_file_node_id("pkg/pipeline.py"), "EXACT_IMPORT") in by_target


def test_parenthesized_from_import_submodules_bind(tmp_path):
    # idiom 5: parenthesized form of idiom 2 — must not regress the
    # sibling-token trap already fixed for the call-binding path (the first
    # name inside parens has prev_sibling `(`, not `import`/`,`).
    _submodule_fixture(tmp_path)
    _write(tmp_path / "consumer.py", "from pkg import (pipeline, runners)\n")
    result = _extract(tmp_path)
    edges = _import_edges(result, "consumer.py")
    by_target = {(e["target"], e["confidence"]) for e in edges}
    assert (_file_node_id("pkg/__init__.py"), "EXACT_IMPORT") in by_target
    assert (_file_node_id("pkg/pipeline.py"), "EXACT_IMPORT") in by_target
    assert (_file_node_id("pkg/runners/__init__.py"), "EXACT_IMPORT") in by_target


def test_from_import_symbol_only_edge_unchanged(tmp_path):
    # idiom 6: `from pkg.pipeline import run` imports a SYMBOL (a function),
    # not a submodule — `pkg.pipeline.run` does not resolve to a file, so no
    # new edge is added; count is unchanged vs today (one edge, to the
    # module file only).
    _submodule_fixture(tmp_path)
    _write(tmp_path / "consumer.py", "from pkg.pipeline import run\n")
    result = _extract(tmp_path)
    edges = _import_edges(result, "consumer.py")
    assert len(edges) == 1
    assert edges[0]["target"] == _file_node_id("pkg/pipeline.py")
    assert edges[0]["confidence"] == "EXACT_IMPORT"


def _graph_for(tmp_path):
    result = _extract(tmp_path)
    return build_graph(result.nodes, result.edges)


def test_parenthesized_from_import_first_name_binds(tmp_path):
    # `from pkg.mod import (first_fn, second_fn)` — the FIRST name's
    # prev_sibling is `(`, not `import`/`,`, which is exactly the case the
    # old sibling-token guard missed. Both names must bind cross-module.
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(
        tmp_path / "pkg" / "mod.py",
        "def first_fn():\n    return 1\n\ndef second_fn():\n    return 2\n",
    )
    _write(
        tmp_path / "consumer.py",
        "from pkg.mod import (first_fn, second_fn)\n"
        "\n"
        "def run():\n"
        "    first_fn()\n"
        "    second_fn()\n",
    )
    g = _graph_for(tmp_path)
    assert g.has_edge("consumer_run", "pkg_mod_first_fn")
    assert g.has_edge("consumer_run", "pkg_mod_second_fn")


def test_parenthesized_from_import_multiline_trailing_comma_binds(tmp_path):
    # Black-style single-name multiline import with a trailing comma:
    #   from pkg.mod import (
    #       only_fn,
    #   )
    # `only_fn`'s prev_sibling is also `(`, not `import`.
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "mod.py", "def only_fn():\n    return 1\n")
    _write(
        tmp_path / "consumer.py",
        "from pkg.mod import (\n"
        "    only_fn,\n"
        ")\n"
        "\n"
        "def run():\n"
        "    only_fn()\n",
    )
    g = _graph_for(tmp_path)
    assert g.has_edge("consumer_run", "pkg_mod_only_fn")


def test_parenthesized_from_import_single_edge_per_module(tmp_path):
    # Regression guard: a parenthesized multi-name from-import must still
    # produce exactly ONE `imports` edge for the module (names are not
    # modules and must not each spawn their own import edge).
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(
        tmp_path / "pkg" / "mod.py",
        "def first_fn():\n    return 1\n\ndef second_fn():\n    return 2\n",
    )
    _write(
        tmp_path / "consumer.py",
        "from pkg.mod import (first_fn, second_fn)\n"
        "\n"
        "def run():\n"
        "    first_fn()\n"
        "    second_fn()\n",
    )
    result = _extract(tmp_path)
    edges = _import_edges(result, "consumer.py")
    assert len(edges) == 1
    assert edges[0]["target"] == "pkg_mod"


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


def test_instance_method_call_binds_via_dispatch(tmp_path):
    _py_fixture(tmp_path)
    g = _graph_for(tmp_path)
    out = query(g, "callers record_run")
    assert "src_pkg_pipeline_run" in [c["id"] for c in out.get("callers", [])]


def test_self_call_binds_to_own_class_method(tmp_path):
    _write(
        tmp_path / "svc.py",
        "class Svc:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "    def run(self):\n"
        "        return self.helper()\n",
    )
    g = _graph_for(tmp_path)
    out = query(g, "callers helper")
    assert "svc_run" in [c["id"] for c in out.get("callers", [])]


def test_unresolved_member_phantom_dropped(tmp_path):
    _write(
        tmp_path / "ext.py",
        "def go(conn):\n"
        "    return conn.execute_special_thing()\n",
    )
    result = _extract(tmp_path)
    g = build_graph(result.nodes, result.edges)
    # no method named execute_special_thing exists -> edge dropped, no phantom node
    assert not any("execute_special_thing" in n for n in g.nodes())


def test_noisy_member_calls_still_filtered(tmp_path):
    _write(
        tmp_path / "noise.py",
        "def go(items):\n"
        "    items.append(1)\n"
        "    return items\n",
    )
    result = _extract(tmp_path)
    assert not any(
        e.get("relation") == "calls" and "append" in e.get("target", "")
        for e in result.edges
    )


def test_python_methods_tagged_top_level_functions_not(tmp_path):
    _py_fixture(tmp_path)
    result = _extract(tmp_path)
    by_id = {n["id"]: n for n in result.nodes}
    assert by_id["src_pkg_ledger_record_run"].get("is_method") is True
    assert by_id["src_pkg_tdd_auto_resolve_tdd"].get("is_method") is None


def test_end_to_end_build_binds_and_is_healthy(tmp_path, monkeypatch):
    import json as _json

    _py_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    from graphite.cli import _build_project

    _build_project(tmp_path, Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite"))
    bundle = _json.loads((tmp_path / "graph-out" / "graph.json").read_text(encoding="utf-8"))
    block = bundle["analysis"]["resolution_health"]
    assert block["schema"] == 2
    assert block["healthy"] is True
    assert block["by_relation"]["imports"]["ratio"] == 1.0
    assert block["by_relation"]["imports"]["external"] >= 1  # json import
    assert block["by_relation"]["calls"]["ratio"] >= 0.8


def test_impact_finds_test_via_from_package_submodule_import(tmp_path):
    # End-to-end regression for issue #7: `from pkg import pipeline` in a
    # test file used to bind ONLY to pkg's __init__.py, so `impact`'s
    # predecessor walk from pkg/pipeline.py never reached the test file.
    # With the fix, the submodule edge lets impact find it directly.
    from graphite import cli

    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "pipeline.py", "def run():\n    return 1\n")
    _write(tmp_path / "tests" / "test_consumer.py", "from pkg import pipeline\n")
    g = _graph_for(tmp_path)
    result = cli._impact(g, ["pkg/pipeline.py"], 2)
    assert "tests/test_consumer.py" in result["likely_tests"]


def test_cache_version_is_v8():
    assert Config().cache_version == "v8"
