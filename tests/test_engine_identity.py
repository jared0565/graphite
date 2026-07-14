"""Engine identity and engine-aware freshness tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import graphite.engine_identity as engine_identity_module
import graphite.freshness as freshness_module
from graphite.config import Config
from graphite.engine_identity import EngineIdentityError, engine_identity
from graphite.freshness import check_graph_freshness
from graphite.ingest import collect_files


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "graphite_fixture"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "resolver.mjs").write_text("export const value = 1;\n", encoding="utf-8")
    (package / "ignored.pyc").write_bytes(b"host-specific bytecode")
    return package


def test_engine_identity_is_deterministic_and_path_free(tmp_path: Path) -> None:
    package = _package(tmp_path)

    first = engine_identity("cache-v1", package_root=package, version="1.2.3")
    second = engine_identity("cache-v1", package_root=package, version="1.2.3")

    assert first == second
    assert set(first) == {"version", "cache_version", "schema_version", "fingerprint"}
    assert first["version"] == "1.2.3"
    assert first["cache_version"] == "cache-v1"
    assert str(package) not in json.dumps(first)


def test_engine_identity_changes_when_trusted_source_changes(tmp_path: Path) -> None:
    package = _package(tmp_path)
    before = engine_identity("cache-v1", package_root=package, version="1.2.3")

    (package / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    after = engine_identity("cache-v1", package_root=package, version="1.2.3")

    assert after["fingerprint"] != before["fingerprint"]


def test_engine_identity_is_independent_of_inventory_order(tmp_path: Path, monkeypatch) -> None:
    package = _package(tmp_path)
    paths = engine_identity_module._collect_engine_files(package, max_files=512)
    expected = engine_identity("cache-v1", package_root=package, version="1.2.3")
    monkeypatch.setattr(
        engine_identity_module,
        "_collect_engine_files",
        lambda _root, *, max_files: list(reversed(paths)),
    )

    actual = engine_identity("cache-v1", package_root=package, version="1.2.3")

    assert actual == expected


@pytest.mark.parametrize(
    ("kwargs", "expected_code"),
    [
        ({"max_files": 1}, "engine_file_limit"),
        ({"max_file_bytes": 4}, "engine_file_too_large"),
        ({"max_total_bytes": 8}, "engine_total_too_large"),
    ],
)
def test_engine_identity_enforces_resource_limits(
    tmp_path: Path,
    kwargs: dict[str, int],
    expected_code: str,
) -> None:
    package = _package(tmp_path)

    with pytest.raises(EngineIdentityError) as raised:
        engine_identity("cache-v1", package_root=package, version="1.2.3", **kwargs)

    assert raised.value.code == expected_code
    assert str(package) not in str(raised.value)


def test_engine_identity_rejects_non_regular_inventory_entry(tmp_path: Path, monkeypatch) -> None:
    package = _package(tmp_path)
    directory_named_like_source = package / "not-a-file.py"
    directory_named_like_source.mkdir()
    monkeypatch.setattr(
        engine_identity_module,
        "_collect_engine_files",
        lambda _root, *, max_files: [directory_named_like_source],
    )

    with pytest.raises(EngineIdentityError) as raised:
        engine_identity("cache-v1", package_root=package, version="1.2.3")

    assert raised.value.code == "engine_non_regular"


def test_engine_identity_rejects_package_root_crossing(tmp_path: Path, monkeypatch) -> None:
    package = _package(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 2\n", encoding="utf-8")
    monkeypatch.setattr(
        engine_identity_module,
        "_collect_engine_files",
        lambda _root, *, max_files: [outside],
    )

    with pytest.raises(EngineIdentityError) as raised:
        engine_identity("cache-v1", package_root=package, version="1.2.3")

    assert raised.value.code == "engine_root_crossing"


def test_engine_identity_rejects_unreadable_source(tmp_path: Path, monkeypatch) -> None:
    package = _package(tmp_path)

    def fail_read(_path: Path, _root: Path, _limit: int) -> bytes:
        raise OSError("sensitive absolute path must not escape")

    monkeypatch.setattr(engine_identity_module, "_read_stable_file", fail_read)

    with pytest.raises(EngineIdentityError) as raised:
        engine_identity("cache-v1", package_root=package, version="1.2.3")

    assert raised.value.code == "engine_file_unreadable"
    assert "sensitive absolute path" not in str(raised.value)


def test_freshness_treats_legacy_manifest_as_engine_changed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    output = tmp_path / "graph-out"
    root.mkdir()
    output.mkdir()
    (output / ".graphite_manifest.json").write_text(
        json.dumps({"root": root.name, "files": []}),
        encoding="utf-8",
    )

    result = check_graph_freshness(root, Config(output_dir=output))

    assert result == {
        "stale": True,
        "reason": "engine_changed",
        "added": [],
        "changed": [],
        "removed": [],
    }


def test_freshness_prioritizes_engine_mismatch_when_repository_is_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "project"
    output = tmp_path / "graph-out"
    root.mkdir()
    output.mkdir()
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    cfg = Config(output_dir=output)
    files = collect_files(root, cfg)
    (output / ".graphite_manifest.json").write_text(
        json.dumps(
            {
                "root": root.name,
                "files": [{"rel_path": item.rel_path, "hash": item.content_hash} for item in files],
                "engine": {
                    "version": "old",
                    "cache_version": cfg.cache_version,
                    "schema_version": "1",
                    "fingerprint": "0" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        freshness_module,
        "engine_identity",
        lambda _cache_version: {
            "version": "new",
            "cache_version": cfg.cache_version,
            "schema_version": "1",
            "fingerprint": "1" * 64,
        },
    )

    result = check_graph_freshness(root, cfg)

    assert result == {
        "stale": True,
        "reason": "engine_changed",
        "added": [],
        "changed": [],
        "removed": [],
    }
