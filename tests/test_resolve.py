"""Unit tests for SourceIndex resolution helpers."""
from __future__ import annotations

from pathlib import Path

from graphite.resolve import (
    RustUseResolution,
    SourceIndex,
    _load_cargo_crates,
    _load_cargo_dependencies,
    _normalize_crate_name,
    _rust_module_candidates,
    _rust_module_dir,
    _rust_module_segments,
)

CRATES = (
    ("ofw_policy", "crates/ofw-policy", "crates/ofw-policy/src"),
    ("ofw_contracts", "crates/ofw-contracts", "crates/ofw-contracts/src"),
)
FILES = {
    "crates/ofw-policy/src/lib.rs",
    "crates/ofw-policy/src/rule.rs",
    "crates/ofw-contracts/src/lib.rs",
}


def _index(
    tmp_path: Path,
    rel_paths: set[str],
    crates: tuple[tuple[str, str, str], ...] = (),
    deps: frozenset[str] = frozenset(),
) -> SourceIndex:
    return SourceIndex(
        root=tmp_path,
        rel_paths=frozenset(rel_paths),
        path_aliases=(),
        typescript=None,
        cargo_crates=crates,
        cargo_dependencies=deps,
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_normalize_crate_name_uses_cargo_underscore_rule() -> None:
    assert _normalize_crate_name("ofw-contracts") == "ofw_contracts"
    assert _normalize_crate_name("  ofw_policy ") == "ofw_policy"


def test_load_cargo_crates_maps_name_to_src_root(tmp_path: Path) -> None:
    _write(tmp_path, "crates/ofw-contracts/Cargo.toml", '[package]\nname = "ofw-contracts"\n')
    rel_paths = frozenset({"crates/ofw-contracts/Cargo.toml"})

    assert _load_cargo_crates(tmp_path, rel_paths) == (
        ("ofw_contracts", "crates/ofw-contracts", "crates/ofw-contracts/src"),
    )


def test_load_cargo_crates_skips_virtual_and_malformed_manifests(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", '[workspace]\nmembers = ["crates/*"]\n')
    _write(tmp_path, "crates/broken/Cargo.toml", "this is not valid toml {{{")
    rel_paths = frozenset({"Cargo.toml", "crates/broken/Cargo.toml"})

    assert _load_cargo_crates(tmp_path, rel_paths) == ()


def test_load_cargo_dependencies_unions_all_tables(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "crates/a/Cargo.toml",
        '[package]\nname = "a"\n'
        '[dependencies]\nserde = "1"\n'
        '[dev-dependencies]\nproptest = "1"\n'
        '[build-dependencies]\ncc = "1"\n',
    )
    _write(tmp_path, "Cargo.toml", '[workspace.dependencies]\ntokio-util = "0.7"\n')
    rel_paths = frozenset({"crates/a/Cargo.toml", "Cargo.toml"})

    assert _load_cargo_dependencies(tmp_path, rel_paths) == frozenset(
        {"serde", "proptest", "cc", "tokio_util"}
    )


def test_rust_module_dir_root_files_use_their_own_directory() -> None:
    assert _rust_module_dir("crates/a/src/lib.rs") == "crates/a/src"
    assert _rust_module_dir("crates/a/src/main.rs") == "crates/a/src"
    assert _rust_module_dir("crates/a/src/net/mod.rs") == "crates/a/src/net"


def test_rust_module_dir_file_module_uses_a_directory_named_after_it() -> None:
    assert _rust_module_dir("crates/a/src/policy.rs") == "crates/a/src/policy"


def test_rust_module_candidates_covers_both_layouts() -> None:
    assert _rust_module_candidates("src/policy") == ["src/policy.rs", "src/policy/mod.rs"]


def test_rust_module_segments_stops_at_the_first_item_segment() -> None:
    # Modules are snake_case, items are CamelCase -- Rule is an item, not a dir.
    assert _rust_module_segments(["policy", "rule", "Rule"]) == ["policy", "rule"]
    assert _rust_module_segments(["Rule"]) == []
    assert _rust_module_segments(["policy", "rule"]) == ["policy", "rule"]


def test_resolve_rust_use_tags_allowlisted_roots_external(tmp_path: Path) -> None:
    index = _index(tmp_path, FILES, CRATES)
    assert index.resolve_rust_use(
        "crates/ofw-policy/src/lib.rs", "std::collections::BTreeSet"
    ) == RustUseResolution(None, True)


def test_resolve_rust_use_tags_declared_dependencies_external(tmp_path: Path) -> None:
    index = _index(tmp_path, FILES, CRATES, frozenset({"serde"}))
    assert index.resolve_rust_use(
        "crates/ofw-policy/src/lib.rs", "serde::Serialize"
    ) == RustUseResolution(None, True)


def test_resolve_rust_use_leaves_an_unknown_root_unresolved(tmp_path: Path) -> None:
    # The guard against laundering a real miss as "external".
    index = _index(tmp_path, FILES, CRATES, frozenset({"serde"}))
    assert index.resolve_rust_use(
        "crates/ofw-policy/src/lib.rs", "typo_crate::Thing"
    ) == RustUseResolution(None, False)


def test_resolve_rust_use_binds_a_sibling_workspace_crate(tmp_path: Path) -> None:
    index = _index(tmp_path, FILES, CRATES)
    assert index.resolve_rust_use(
        "crates/ofw-policy/src/lib.rs", "ofw_contracts::Rule"
    ) == RustUseResolution("crates/ofw-contracts/src/lib.rs", False)


def test_resolve_rust_use_binds_crate_relative_module(tmp_path: Path) -> None:
    index = _index(tmp_path, FILES, CRATES)
    assert index.resolve_rust_use(
        "crates/ofw-policy/src/lib.rs", "crate::rule::Rule"
    ) == RustUseResolution("crates/ofw-policy/src/rule.rs", False)


def test_resolve_rust_use_super_inside_an_inline_module_is_the_same_file(tmp_path: Path) -> None:
    index = _index(tmp_path, FILES, CRATES)
    assert index.resolve_rust_use(
        "crates/ofw-policy/src/lib.rs", "super::Rule", inline_mod_depth=1
    ) == RustUseResolution("crates/ofw-policy/src/lib.rs", False)


def test_resolve_rust_use_super_at_file_scope_is_the_parent_module(tmp_path: Path) -> None:
    index = _index(tmp_path, FILES, CRATES)
    assert index.resolve_rust_use(
        "crates/ofw-policy/src/rule.rs", "super::Thing", inline_mod_depth=0
    ) == RustUseResolution("crates/ofw-policy/src/lib.rs", False)


def test_resolve_rust_use_crate_falls_back_to_nearest_src_without_a_manifest(
    tmp_path: Path,
) -> None:
    """A repo whose Cargo.toml was not scanned must still resolve `crate::`."""
    index = _index(tmp_path, {"src/app.rs", "src/store.rs"}, crates=())
    assert index.resolve_rust_use(
        "src/app.rs", "crate::store::Store"
    ) == RustUseResolution("src/store.rs", False)
