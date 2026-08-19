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

    assert nodes["store_store_go_helper_b0636a"]["kind"] == "function"
    assert nodes["store_store_go_store_e25930"]["kind"] == "class"  # type Store struct{}
    assert nodes["store_store_go_load_4cf55b"].get("is_method") is True
    assert ("store_store_go_caller_6c08c3", "store_store_go_helper_b0636a") in calls


def test_go_method_dispatch_resolves_cross_file_and_noise_dropped(tmp_path: Path) -> None:
    _go_fixture(tmp_path)
    result = _extract(tmp_path)
    calls = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}

    # s.Load() in app.go re-points to the method definition in store.go.
    assert ("app_app_go_use_0718fe", "store_store_go_load_4cf55b") in calls
    # fmt.Println matches no project method -> dropped, not a phantom.
    assert not any("println" in tgt for _s, tgt in calls)


def test_go_imports_recorded_per_spec(tmp_path: Path) -> None:
    _go_fixture(tmp_path)
    result = _extract(tmp_path)
    imports = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "imports"}

    assert ("app_app_go", "fmt") in imports
    assert ("app_app_go", "example_com_repo_store") in imports


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

    assert nodes["src_store_rs_helper"]["kind"] == "function"
    assert nodes["src_store_rs_store_fa7824"]["kind"] == "class"  # pub struct Store
    assert nodes["src_store_rs_load"].get("is_method") is True
    assert nodes["src_store_rs_new"].get("is_method") is True
    assert ("src_store_rs_caller", "src_store_rs_helper") in calls
    # impl methods hang off the struct node via contains.
    contains = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "contains"}
    assert ("src_store_rs_store_fa7824", "src_store_rs_load") in contains


def test_rust_method_and_assoc_fn_dispatch_cross_file(tmp_path: Path) -> None:
    _rust_fixture(tmp_path)
    result = _extract(tmp_path)
    calls = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}

    # store.load() (field_expression) resolves to the impl method.
    assert ("src_app_rs_use_store", "src_store_rs_load") in calls
    # Store::new() (scoped_identifier) resolves to the associated fn.
    assert ("src_app_rs_make", "src_store_rs_new") in calls
    # .clone() matches no project method -> dropped; println! is a macro,
    # never extracted as a call.
    assert not any("clone" in tgt for _s, tgt in calls)
    assert not any("println" in tgt for _s, tgt in calls)


def test_rust_use_declarations_become_import_edges(tmp_path: Path) -> None:
    _rust_fixture(tmp_path)
    result = _extract(tmp_path)
    imports = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "imports"}
    confidence = {
        e["target"]: e.get("confidence")
        for e in result.edges
        if e["relation"] == "imports" and e["source"] == "src_app_rs"
    }

    # std is *confirmed* external: unbound synthetic target, tagged so the
    # health ratio excludes it rather than counting it as a resolver failure.
    assert ("src_app_rs", "std_collections_hashmap_ea9e65") in imports
    assert confidence["std_collections_hashmap_ea9e65"] == "EXTERNAL_IMPORT"

    # crate::store::Store now binds to the real file node -- this assertion
    # used to read "crate_store_store", a synthetic id that matched no node.
    # The fixture ships no Cargo.toml, so this also covers the nearest-`src`
    # fallback in _rust_crate_for.
    assert ("src_app_rs", "src_store_rs") in imports
    assert confidence["src_store_rs"] != "EXTERNAL_IMPORT"


def _rust_workspace_fixture(tmp_path: Path) -> None:
    _write(tmp_path / "crates" / "a" / "Cargo.toml", '[package]\nname = "a"\n[dependencies]\nserde = "1"\n')
    _write(tmp_path / "crates" / "b" / "Cargo.toml", '[package]\nname = "b"\n')
    _write(tmp_path / "crates" / "b" / "src" / "lib.rs", "pub struct Rule;\n")
    _write(
        tmp_path / "crates" / "a" / "src" / "lib.rs",
        "use std::collections::BTreeSet;\n"
        "use serde::Serialize;\n"
        "use b::Rule;\n"
        "use typo_crate::Thing;\n",
    )


def test_rust_use_binds_in_repo_and_tags_only_confirmed_external(tmp_path: Path) -> None:
    _rust_workspace_fixture(tmp_path)
    result = _extract(tmp_path)
    confidence = {
        e["target"]: e.get("confidence")
        for e in result.edges
        if e["relation"] == "imports" and e["source"] == "crates_a_src_lib_rs"
    }

    # A sibling workspace crate binds to its real lib.rs node.
    assert "crates_b_src_lib_rs" in confidence
    assert confidence["crates_b_src_lib_rs"] != "EXTERNAL_IMPORT"
    # Allowlisted root and a declared Cargo dependency are confirmed external.
    assert confidence["std_collections_btreeset_c9300d"] == "EXTERNAL_IMPORT"
    assert confidence["serde_serialize_d44030"] == "EXTERNAL_IMPORT"
    # The anti-laundering guard: an unresolvable target must stay counted
    # against the ratio, never be excused as "external".
    assert confidence["typo_crate_thing_e09050"] != "EXTERNAL_IMPORT"


def test_rust_bare_method_on_an_unnameable_receiver_is_not_called_external(tmp_path: Path) -> None:
    """An unnameable receiver says nothing about where the call goes (#56).

    `_simple_rust_value` returns None for anything but an identifier or `self`, so
    `build().format()` reaches `_call_confidence` as the bare name `format` -- which
    is in `_EXTERNAL_GLOBALS`. Python (`:1414`) and TS/JS (`:474`) pass
    `attributable=` for exactly this case; the Rust and Go sites did not, so Rust
    produced the **false external** that #14 mechanism A fixed everywhere else: an
    edge excused from the health denominator without ever being shown to leave the
    repo.

    Go is deliberately not changed alongside this. Its `selector_expression` branch
    builds `called` from the operand's raw source text, so the bare-name fallback
    needs an operand that parses with no text at all -- no fixture here reaches it,
    and a guard nothing can trip is not a guard.
    """
    _write(
        tmp_path / "src" / "sink.rs",
        "pub struct Sink;\n"
        "\n"
        "impl Sink {\n"
        "    pub fn format(&self) -> i32 { 1 }\n"
        "}\n"
        "\n"
        "pub fn build() -> Sink { Sink }\n"
        "\n"
        "pub fn caller() -> i32 { build().format() }\n",
    )
    result = _extract(tmp_path)
    confidence = {
        e["target"]: e.get("confidence")
        for e in result.edges
        if e["relation"] == "calls" and e["source"] == "src_sink_rs_caller"
    }

    assert confidence, "the format() call produced no edge at all"
    assert "EXTERNAL_CALL" not in confidence.values(), (
        f"a bare method name off an unnameable receiver was classified external: {confidence}"
    )


def test_rust_use_super_in_inline_test_module_emits_no_self_edge(tmp_path: Path) -> None:
    _write(tmp_path / "crates" / "a" / "Cargo.toml", '[package]\nname = "a"\n')
    _write(
        tmp_path / "crates" / "a" / "src" / "lib.rs",
        "pub struct Rule;\n#[cfg(test)]\nmod tests {\n    use super::Rule;\n}\n",
    )
    result = _extract(tmp_path)

    # A file importing itself carries no dependency information.
    assert [e for e in result.edges if e["relation"] == "imports"] == []


def test_rust_mod_declaration_links_to_the_module_file(tmp_path: Path) -> None:
    """`mod foo;` is Rust's file-inclusion mechanism and must produce an edge."""
    _write(tmp_path / "crates" / "a" / "Cargo.toml", '[package]\nname = "a"\n')
    _write(tmp_path / "crates" / "a" / "src" / "lib.rs", "mod rule;\n")
    _write(tmp_path / "crates" / "a" / "src" / "rule.rs", "pub struct Rule;\n")
    result = _extract(tmp_path)

    imports = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "imports"}
    assert ("crates_a_src_lib_rs", "crates_a_src_rule_rs") in imports


def test_rust_inline_mod_emits_no_module_edge(tmp_path: Path) -> None:
    _write(tmp_path / "crates" / "a" / "Cargo.toml", '[package]\nname = "a"\n')
    _write(
        tmp_path / "crates" / "a" / "src" / "lib.rs",
        "mod tests {\n    pub fn helper() {}\n}\n",
    )
    result = _extract(tmp_path)

    assert [e for e in result.edges if e["relation"] == "imports"] == []


def test_go_rust_query_verbs_work_end_to_end(tmp_path: Path) -> None:
    _go_fixture(tmp_path)
    _rust_fixture(tmp_path)
    result = _extract(tmp_path)
    g = build_graph(result.nodes, result.edges)

    # Exact ids disambiguate the two same-named methods across languages.
    go_callers = query(g, "callers store_store_go_load_4cf55b")
    assert "app_app_go_use_0718fe" in {c["id"] for c in go_callers["callers"]}
    assert go_callers["match"]["type"] == "exact-id"
    rust_callers = query(g, "callers src_store_rs_load")
    assert "src_app_rs_use_store" in {c["id"] for c in rust_callers["callers"]}

    # A bare ambiguous name is answered deterministically AND flags the
    # other candidate as an alternate, so agents see the ambiguity.
    ambiguous = query(g, "callers load")
    assert ambiguous["match"]["type"] == "name"
    picked = {ambiguous["node"], *ambiguous["match"].get("alternates", [])}
    assert {"src_store_rs_load", "store_store_go_load_4cf55b"} <= picked
