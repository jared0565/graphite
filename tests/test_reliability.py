"""Reliability and artifact integrity tests for Graphite."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.cli import main
from graphite.io import atomic_write_json, atomic_write_text
from graphite.validation import validate_graph_bundle


def _valid_bundle() -> dict:
    return {
        "nodes": [
            {"id": "src_app", "kind": "file", "name": "app.ts", "source_file": "src/app.ts"},
            {"id": "src_store", "kind": "file", "name": "store.ts", "source_file": "src/store.ts"},
        ],
        "edges": [
            {"source": "src_app", "target": "src_store", "relation": "imports", "source_file": "src/app.ts"}
        ],
        "clusters": [{"id": 1, "members": ["src_app", "src_store"]}],
        "analysis": {},
        "metadata": {"node_count": 2, "edge_count": 1, "community_count": 1},
    }


def test_validate_graph_bundle_accepts_valid_bundle() -> None:
    report = validate_graph_bundle(_valid_bundle())

    assert report["ok"] is True
    assert report["error_count"] == 0
    assert report["node_count"] == 2
    assert report["edge_count"] == 1


def test_validate_graph_bundle_rejects_unknown_edge_target_and_absolute_paths() -> None:
    bundle = _valid_bundle()
    bundle["nodes"][0]["source_file"] = "C:/Users/example/secret.ts"
    bundle["edges"][0]["target"] = "missing_node"
    bundle["metadata"]["edge_count"] = 99

    report = validate_graph_bundle(bundle)
    codes = {issue["code"] for issue in report["errors"]}

    assert report["ok"] is False
    assert "absolute_source_file" in codes
    assert "edge_target_unknown" in codes
    assert "metadata_edge_count" in codes


def test_validate_cli_returns_zero_for_valid_graph(tmp_path: Path, capsys) -> None:
    graph = tmp_path / "graph.json"
    atomic_write_json(graph, _valid_bundle())

    result = main(["validate", "--graph-json", str(graph), "--json"])
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["ok"] is True


def test_validate_cli_returns_nonzero_for_invalid_graph(tmp_path: Path, capsys) -> None:
    graph = tmp_path / "graph.json"
    bundle = _valid_bundle()
    bundle["edges"][0]["source"] = "missing_node"
    atomic_write_json(graph, bundle)

    result = main(["validate", "--graph-json", str(graph)])
    output = capsys.readouterr().out

    assert result == 1
    assert "graph invalid" in output
    assert "edge_source_unknown" in output


def test_validate_cli_truncates_error_list_and_marks_dropped_count(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    import argparse

    from graphite import cli

    graph = tmp_path / "graph.json"
    atomic_write_json(graph, _valid_bundle())

    report = {
        "ok": False,
        "node_count": 2,
        "edge_count": 1,
        "warning_count": 0,
        "error_count": 13,
        "errors": [{"code": f"c{i}", "message": "m", "path": "p"} for i in range(13)],
        "warnings": [],
    }
    monkeypatch.setattr(cli, "validate_graph_bundle", lambda bundle: report)

    result = cli.cmd_validate(argparse.Namespace(graph_json=str(graph), json=False))
    output = capsys.readouterr().out

    assert result == 1
    assert "  ... 3 more" in output
    assert output.count("m [p]") == 10


def test_atomic_write_text_replaces_existing_file_and_removes_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    atomic_write_text(target, "old")
    atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.glob(".artifact.txt.*.tmp")) == []


def test_build_writes_validation_artifact(tmp_path: Path, monkeypatch, capsys) -> None:
    src = tmp_path / "src" / "app.ts"
    src.parent.mkdir(parents=True)
    src.write_text("export const app = 1;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = main(["--output-dir", "graph-out", "build", "."])
    capsys.readouterr()
    validation = json.loads((tmp_path / "graph-out" / ".graphite_validation.json").read_text(encoding="utf-8"))

    assert result == 0
    assert validation["ok"] is True


def test_build_manifest_records_engine_identity(tmp_path: Path, monkeypatch, capsys, assert_json_omits) -> None:
    src = tmp_path / "src" / "app.py"
    src.parent.mkdir(parents=True)
    src.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = main(["--output-dir", "graph-out", "build", "."])
    capsys.readouterr()
    manifest = json.loads(
        (tmp_path / "graph-out" / ".graphite_manifest.json").read_text(encoding="utf-8")
    )

    assert result == 0
    assert set(manifest["engine"]) == {
        "version",
        "cache_version",
        "schema_version",
        "fingerprint",
    }
    assert len(manifest["engine"]["fingerprint"]) == 64
    assert_json_omits(Path(__file__).parents[1], manifest["engine"])
