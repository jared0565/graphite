"""Unit tests for SourceIndex resolution helpers."""
from __future__ import annotations

from pathlib import Path

from graphite.resolve import (
    _load_cargo_crates,
    _load_cargo_dependencies,
    _normalize_crate_name,
    _rust_module_candidates,
    _rust_module_dir,
    _rust_module_segments,
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
