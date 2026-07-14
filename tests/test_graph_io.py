"""Bounded, validated graph-read boundary tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import graphite.graph_io as graph_io_module
from graphite.cli import main
from graphite.graph_io import GraphReadError, load_validated_graph_bundle


def _valid_bundle() -> dict:
    return {
        "nodes": [
            {
                "id": "src_app",
                "kind": "file",
                "name": "app.py",
                "source_file": "src/app.py",
            }
        ],
        "edges": [],
        "clusters": [{"id": 1, "members": ["src_app"]}],
        "analysis": {},
        "metadata": {"node_count": 1, "edge_count": 0, "community_count": 1},
    }


def _write_graph(root: Path, bundle: object | None = None) -> Path:
    graph = root / "graph-out" / "graph.json"
    graph.parent.mkdir(parents=True)
    graph.write_text(json.dumps(_valid_bundle() if bundle is None else bundle), encoding="utf-8")
    return graph


def test_load_validated_graph_bundle_accepts_contained_regular_file(tmp_path: Path) -> None:
    graph_path = _write_graph(tmp_path)

    bundle, graph = load_validated_graph_bundle(graph_path, root=tmp_path)

    assert bundle == _valid_bundle()
    assert list(graph.nodes) == ["src_app"]


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"\xff\xfeprivate", "graph_invalid_utf8"),
        (b'{"nodes":', "graph_invalid_json"),
        (json.dumps({"nodes": [], "edges": "wrong", "metadata": {}}).encode(), "graph_invalid"),
    ],
)
def test_load_validated_graph_bundle_uses_fixed_parse_and_validation_errors(
    tmp_path: Path,
    payload: bytes,
    code: str,
) -> None:
    graph_path = tmp_path / "graph-out" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_bytes(payload)

    with pytest.raises(GraphReadError) as raised:
        load_validated_graph_bundle(graph_path, root=tmp_path)

    assert raised.value.code == code
    assert str(tmp_path) not in str(raised.value)
    assert payload[:8].hex() not in str(raised.value)


def test_load_validated_graph_bundle_checks_size_before_read(tmp_path: Path) -> None:
    graph_path = _write_graph(tmp_path)

    with pytest.raises(GraphReadError) as raised:
        load_validated_graph_bundle(graph_path, root=tmp_path, max_bytes=8)

    assert raised.value.code == "graph_too_large"


def test_load_validated_graph_bundle_rejects_root_crossing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = _write_graph(tmp_path / "outside")

    with pytest.raises(GraphReadError) as raised:
        load_validated_graph_bundle(outside, root=root)

    assert raised.value.code == "graph_outside_root"
    assert str(outside) not in str(raised.value)


def test_load_validated_graph_bundle_rejects_non_file(tmp_path: Path) -> None:
    graph_directory = tmp_path / "graph-out" / "graph.json"
    graph_directory.mkdir(parents=True)

    with pytest.raises(GraphReadError) as raised:
        load_validated_graph_bundle(graph_directory, root=tmp_path)

    assert raised.value.code == "graph_not_regular"


def test_load_validated_graph_bundle_rejects_symlink(tmp_path: Path) -> None:
    target = _write_graph(tmp_path, _valid_bundle())
    link = tmp_path / "linked.json"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(GraphReadError) as raised:
        load_validated_graph_bundle(link, root=tmp_path)

    assert raised.value.code == "graph_reparse"


def test_load_validated_graph_bundle_rejects_symlinked_parent(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    _write_graph(real_directory)
    linked_directory = tmp_path / "linked"
    try:
        linked_directory.symlink_to(real_directory, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")

    with pytest.raises(GraphReadError) as raised:
        load_validated_graph_bundle(
            linked_directory / "graph-out" / "graph.json",
            root=tmp_path,
        )

    assert raised.value.code == "graph_reparse"


def test_load_validated_graph_bundle_rejects_replacement_during_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph_path = _write_graph(tmp_path)
    real_fstat = os.fstat
    calls = 0

    def unstable_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        result = real_fstat(descriptor)
        if calls < 2:
            return result
        return SimpleNamespace(
            st_dev=result.st_dev,
            st_ino=result.st_ino,
            st_size=result.st_size,
            st_mtime_ns=result.st_mtime_ns + 1,
        )

    monkeypatch.setattr(graph_io_module.os, "fstat", unstable_fstat)

    with pytest.raises(GraphReadError) as raised:
        load_validated_graph_bundle(graph_path, root=tmp_path)

    assert raised.value.code == "graph_changed"


def test_loader_validates_before_building_graph(tmp_path: Path, monkeypatch) -> None:
    graph_path = _write_graph(tmp_path)
    calls: list[str] = []
    real_validate = graph_io_module.validate_graph_bundle
    real_build = graph_io_module.graph_from_json

    def validate(bundle: dict) -> dict:
        calls.append("validate")
        return real_validate(bundle)

    def build(bundle: dict):
        calls.append("build")
        return real_build(bundle)

    monkeypatch.setattr(graph_io_module, "validate_graph_bundle", validate)
    monkeypatch.setattr(graph_io_module, "graph_from_json", build)

    load_validated_graph_bundle(graph_path, root=tmp_path)

    assert calls == ["validate", "build"]


def test_query_cli_rejects_invalid_bundle_with_sanitized_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    graph_path = _write_graph(tmp_path, {"nodes": [], "edges": "private", "metadata": {}})
    monkeypatch.chdir(tmp_path)

    assert main(["query", "stats", "--graph-json", str(graph_path)]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "[graphite] error: graph unavailable: graph_invalid\n"
    assert str(tmp_path) not in output.err


def test_mcp_loader_rejects_invalid_bundle_without_leaking_details(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from graphite.mcp_server import GraphiteMCPServer

    _write_graph(tmp_path, {"nodes": [], "edges": "private", "metadata": {}})
    server = GraphiteMCPServer(tmp_path)

    assert server._load() is False
    assert server._load_error == "Graph unavailable: graph_invalid"
    assert str(tmp_path) not in server._load_error
