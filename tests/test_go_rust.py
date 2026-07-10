"""Go and Rust heuristic extraction: functions, methods, types, imports, calls.

Same fidelity tier as the Python path. Method-style calls ride the existing
`_member` dispatch post-pass (cross-file resolution to `is_method` definitions,
unresolved framework noise dropped by the phantom filter).
"""
from __future__ import annotations

from pathlib import Path

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.graph import build_graph
from graphite.ingest import collect_files
from graphite.query import query


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(tmp_path: Path):
    cfg = Config(
        workers=1,
        cache_dir=tmp_path / ".cache" / "graphite",
        typescript_resolver="disabled",
    )
    entries = collect_files(tmp_path, cfg)
    return extract_all(entries, cfg)


# ─── Go ────────────────────────────────────────────────────────────────────────


def _go_fixture(tmp_path: Path) -> None:
    _write(
        tmp_path / "store" / "store.go",
        "package store\n"
        "\n"
        "type Store struct{}\n"
        "\n"
        "func (s *Store) Load() int { return 1 }\n"
        "\n"
        "func Helper() int { return 2 }\n"
        "\n"
        "func Caller() int { return Helper() }\n",
    )
    _write(
        tmp_path / "app" / "app.go",
        "package app\n"
        "\n"
        "import (\n"
        "\t\"fmt\"\n"
        "\t\"example.com/repo/store\"\n"
        ")\n"
        "\n"
        "func Use(s *store.Store) int {\n"
        "\tfmt.Println(\"using\")\n"
        "\treturn s.Load()\n"
        "}\n",
    )


def test_go_functions_types_and_same_file_calls(tmp_path: Path) -> None:
    _go_fixture(tmp_path)
    result = _extract(tmp_path)
    nodes = {n["id"]: n for n in result.nodes}
    calls = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}

    assert nodes["store_store_helper"]["kind"] == "function"
    assert nodes["store_store_store"]["kind"] == "class"  # type Store struct{}
    assert nodes["store_store_load"].get("is_method") is True
    assert ("store_store_caller", "store_store_helper") in calls


def test_go_method_dispatch_resolves_cross_file_and_noise_dropped(tmp_path: Path) -> None:
    _go_fixture(tmp_path)
    result = _extract(tmp_path)
    calls = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}

    # s.Load() in app.go re-points to the method definition in store.go.
    assert ("app_app_use", "store_store_load") in calls
    # fmt.Println matches no project method -> dropped, not a phantom.
    assert not any("println" in tgt for _s, tgt in calls)


def test_go_imports_recorded_per_spec(tmp_path: Path) -> None:
    _go_fixture(tmp_path)
    result = _extract(tmp_path)
    imports = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "imports"}

    assert ("app_app", "fmt") in imports
    assert ("app_app", "example_com_repo_store") in imports


# ─── Rust ──────────────────────────────────────────────────────────────────────


def _rust_fixture(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "store.rs",
        "pub struct Store;\n"
        "\n"
        "impl Store {\n"
        "    pub fn new() -> Store { Store }\n"
        "    pub fn load(&self) -> i32 { 1 }\n"
        "}\n"
        "\n"
        "pub fn helper() -> i32 { 2 }\n"
        "\n"
        "pub fn caller() -> i32 { helper() }\n",
    )
    _write(
        tmp_path / "src" / "app.rs",
        "use std::collections::HashMap;\n"
        "use crate::store::Store;\n"
        "\n"
        "fn use_store(store: &Store) -> i32 {\n"
        "    println!(\"using\");\n"
        "    let copy = store.clone();\n"
        "    store.load()\n"
        "}\n"
        "\n"
        "fn make() -> Store {\n"
        "    Store::new()\n"
        "}\n",
    )


def test_rust_functions_types_and_same_file_calls(tmp_path: Path) -> None:
    _rust_fixture(tmp_path)
    result = _extract(tmp_path)
    nodes = {n["id"]: n for n in result.nodes}
    calls = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}

    assert nodes["src_store_helper"]["kind"] == "function"
    assert nodes["src_store_store"]["kind"] == "class"  # pub struct Store
    assert nodes["src_store_load"].get("is_method") is True
    assert nodes["src_store_new"].get("is_method") is True
    assert ("src_store_caller", "src_store_helper") in calls
    # impl methods hang off the struct node via contains.
    contains = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "contains"}
    assert ("src_store_store", "src_store_load") in contains


def test_rust_method_and_assoc_fn_dispatch_cross_file(tmp_path: Path) -> None:
    _rust_fixture(tmp_path)
    result = _extract(tmp_path)
    calls = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}

    # store.load() (field_expression) resolves to the impl method.
    assert ("src_app_use_store", "src_store_load") in calls
    # Store::new() (scoped_identifier) resolves to the associated fn.
    assert ("src_app_make", "src_store_new") in calls
    # .clone() matches no project method -> dropped; println! is a macro,
    # never extracted as a call.
    assert not any("clone" in tgt for _s, tgt in calls)
    assert not any("println" in tgt for _s, tgt in calls)


def test_rust_use_declarations_become_import_edges(tmp_path: Path) -> None:
    _rust_fixture(tmp_path)
    result = _extract(tmp_path)
    imports = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "imports"}

    assert ("src_app", "std_collections_hashmap") in imports
    assert ("src_app", "crate_store_store") in imports


def test_go_rust_query_verbs_work_end_to_end(tmp_path: Path) -> None:
    _go_fixture(tmp_path)
    _rust_fixture(tmp_path)
    result = _extract(tmp_path)
    g = build_graph(result.nodes, result.edges)

    # Exact ids disambiguate the two same-named methods across languages.
    go_callers = query(g, "callers store_store_load")
    assert "app_app_use" in {c["id"] for c in go_callers["callers"]}
    assert go_callers["match"]["type"] == "exact-id"
    rust_callers = query(g, "callers src_store_load")
    assert "src_app_use_store" in {c["id"] for c in rust_callers["callers"]}

    # A bare ambiguous name is answered deterministically AND flags the
    # other candidate as an alternate, so agents see the ambiguity.
    ambiguous = query(g, "callers load")
    assert ambiguous["match"]["type"] == "name"
    picked = {ambiguous["node"], *ambiguous["match"].get("alternates", [])}
    assert {"src_store_load", "store_store_load"} <= picked
