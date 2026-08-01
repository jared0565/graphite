"""Unit tests for SourceIndex resolution helpers."""
from __future__ import annotations

from pathlib import Path

from graphite.resolve import (
    RustUseResolution,
    SourceIndex,
    _load_cargo_crate_dependencies,
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
    crate_deps: tuple[tuple[str, frozenset[str]], ...] = (),
) -> SourceIndex:
    return SourceIndex(
        root=tmp_path,
        rel_paths=frozenset(rel_paths),
        path_aliases=(),
        typescript=None,
        cargo_crates=crates,
        cargo_dependencies=deps,
        cargo_crate_dependencies=crate_deps,
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


# --- issue #38: dependency attribution is per-crate, not repo-wide -----------


def test_a_crates_dependency_does_not_leak_to_a_sibling_crate(tmp_path: Path) -> None:
    """The defect. `serde` is declared by ofw-policy only, so ofw-contracts
    importing it is an UNDECLARED import and must count as unresolved.

    Classifying it external would drop it from the resolution-health denominator
    -- externals are excluded from the ratio -- so a broken import would inflate
    the health number that strict graph-first enforcement gates on.
    """
    index = _index(
        tmp_path,
        FILES,
        CRATES,
        deps=frozenset({"serde"}),
        crate_deps=(
            ("crates/ofw-policy", frozenset({"serde"})),
            ("crates/ofw-contracts", frozenset()),
        ),
    )

    assert index.resolve_rust_use(
        "crates/ofw-contracts/src/lib.rs", "serde::Serialize"
    ) == RustUseResolution(None, False)


def test_the_crate_that_declares_it_still_tags_external(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        FILES,
        CRATES,
        deps=frozenset({"serde"}),
        crate_deps=(
            ("crates/ofw-policy", frozenset({"serde"})),
            ("crates/ofw-contracts", frozenset()),
        ),
    )

    assert index.resolve_rust_use(
        "crates/ofw-policy/src/lib.rs", "serde::Serialize"
    ) == RustUseResolution(None, True)


def test_a_file_outside_any_known_crate_falls_back_to_the_union(tmp_path: Path) -> None:
    """Repos whose manifests were not scanned must not regress to "everything
    unresolved" -- the fallback keeps them working exactly as before."""
    index = _index(
        tmp_path,
        FILES | {"scratch/main.rs"},
        (),
        deps=frozenset({"serde"}),
        crate_deps=(),
    )

    assert index.resolve_rust_use(
        "scratch/main.rs", "serde::Serialize"
    ) == RustUseResolution(None, True)


def test_workspace_dependencies_are_inheritable_by_every_member(tmp_path: Path) -> None:
    """`dep = { workspace = true }` is legitimate, so a workspace-level
    declaration must remain usable by members. Only CROSS-CRATE leakage is the
    bug -- workspace deps are shared by design."""
    _write(
        tmp_path,
        "Cargo.toml",
        '[workspace]\nmembers = ["crates/a"]\n\n[workspace.dependencies]\ntokio-util = "0.7"\n',
    )
    _write(tmp_path, "crates/a/Cargo.toml", '[package]\nname = "a"\n')
    rel_paths = frozenset({"Cargo.toml", "crates/a/Cargo.toml"})

    per_crate = dict(_load_cargo_crate_dependencies(tmp_path, rel_paths))

    assert "tokio_util" in per_crate["crates/a"]


def test_per_crate_dependencies_are_kept_apart(tmp_path: Path) -> None:
    _write(tmp_path, "crates/a/Cargo.toml", '[package]\nname = "a"\n\n[dependencies]\nserde = "1"\n')
    _write(tmp_path, "crates/b/Cargo.toml", '[package]\nname = "b"\n\n[dependencies]\nrand = "0.8"\n')
    rel_paths = frozenset({"crates/a/Cargo.toml", "crates/b/Cargo.toml"})

    per_crate = dict(_load_cargo_crate_dependencies(tmp_path, rel_paths))

    assert per_crate["crates/a"] == frozenset({"serde"})
    assert per_crate["crates/b"] == frozenset({"rand"})


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


def test_resolve_rust_mod_from_lib_finds_a_sibling_file(tmp_path: Path) -> None:
    index = _index(tmp_path, FILES, CRATES)
    assert (
        index.resolve_rust_mod("crates/ofw-policy/src/lib.rs", "rule")
        == "crates/ofw-policy/src/rule.rs"
    )


def test_resolve_rust_mod_from_a_file_module_descends_into_its_directory(tmp_path: Path) -> None:
    files = FILES | {"crates/ofw-policy/src/rule/kind.rs"}
    index = _index(tmp_path, files, CRATES)
    assert (
        index.resolve_rust_mod("crates/ofw-policy/src/rule.rs", "kind")
        == "crates/ofw-policy/src/rule/kind.rs"
    )


def test_resolve_rust_mod_accepts_the_mod_rs_layout(tmp_path: Path) -> None:
    files = FILES | {"crates/ofw-policy/src/net/mod.rs"}
    index = _index(tmp_path, files, CRATES)
    assert (
        index.resolve_rust_mod("crates/ofw-policy/src/lib.rs", "net")
        == "crates/ofw-policy/src/net/mod.rs"
    )


def test_resolve_rust_mod_returns_none_when_absent(tmp_path: Path) -> None:
    index = _index(tmp_path, FILES, CRATES)
    assert index.resolve_rust_mod("crates/ofw-policy/src/lib.rs", "missing") is None
