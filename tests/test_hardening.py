"""Hardening tests for graph accuracy and development workflow helpers."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.cli import main
from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.analyze import analyze
from graphite.graph import build_graph
from graphite.ingest import collect_files


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_ingest_skips_generated_tooling_and_vendor_dirs(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app.ts", "export const app = 1;\n")
    _write(tmp_path / "graph-out" / "old.ts", "export const stale = 1;\n")
    _write(tmp_path / ".cache" / "graphite" / "cached.ts", "export const cached = 1;\n")
    _write(tmp_path / "tools" / "graphite" / "src" / "tool.ts", "export const tool = 1;\n")
    _write(tmp_path / "vendor" / "lib.ts", "export const vendor = 1;\n")
    _write(tmp_path / "src" / "__pycache__" / "ignored.py", "x = 1\n")

    rel_paths = [e.rel_path for e in collect_files(tmp_path, Config(include_dotfiles=True))]

    assert "src/app.ts" in rel_paths
    assert "graph-out/old.ts" not in rel_paths
    assert ".cache/graphite/cached.ts" not in rel_paths
    assert "tools/graphite/src/tool.ts" not in rel_paths
    assert "vendor/lib.ts" not in rel_paths
    assert "src/__pycache__/ignored.py" not in rel_paths


def test_ts_import_resolution_handles_index_files_and_tsconfig_paths(tmp_path: Path) -> None:
    _write(tmp_path / "tsconfig.json", json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"@lib/*": ["src/lib/*"]}}}))
    _write(tmp_path / "src" / "util" / "index.ts", "export const util = 1;\n")
    _write(tmp_path / "src" / "lib" / "service.ts", "export const service = 1;\n")
    _write(
        tmp_path / "src" / "app.ts",
        "import { util } from './util';\nimport { service } from '@lib/service';\nexport function run() { return util + service; }\n",
    )

    entries = collect_files(tmp_path, Config())
    result = extract_all(entries, Config(workers=1, typescript_resolver="disabled", cache_dir=tmp_path / ".cache" / "graphite"))
    imports = {(e["source"], e["target"], e["relation"], e.get("confidence")) for e in result.edges if e["relation"] == "imports"}

    assert ("src_app", "src_util_index", "imports", "EXACT_IMPORT") in imports
    assert ("src_app", "src_lib_service", "imports", "EXACT_IMPORT") in imports


def test_ts_call_noise_filter_keeps_local_calls_and_drops_builtin_member_calls(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "app.ts",
        "const items = [1, 2];\nfunction localHelper() { return 1; }\nexport function run() { items.map(x => x); JSON.parse('{}'); console.log(localHelper()); }\n",
    )

    entries = collect_files(tmp_path, Config())
    result = extract_all(entries, Config(workers=1, typescript_resolver="disabled", cache_dir=tmp_path / ".cache" / "graphite"))
    call_targets = {e["target"] for e in result.edges if e["relation"] == "calls"}

    assert "src_app_localhelper" in call_targets
    assert "src_app_items_map" not in call_targets
    assert "src_app_json_parse" not in call_targets
    assert "src_app_console_log" not in call_targets


def test_check_reports_fresh_and_then_stale_changed_file(tmp_path: Path, monkeypatch, capsys) -> None:
    _write(tmp_path / "src" / "app.ts", "export const app = 1;\n")
    monkeypatch.chdir(tmp_path)

    assert main(["build", "."]) == 0
    capsys.readouterr()

    assert main(["check", ".", "--json"]) == 0
    fresh = json.loads(capsys.readouterr().out)
    assert fresh["stale"] is False

    _write(tmp_path / "src" / "app.ts", "export const app = 2;\n")
    assert main(["check", ".", "--json"]) == 1
    stale = json.loads(capsys.readouterr().out)
    assert stale["stale"] is True
    assert "src/app.ts" in stale["changed"]


def test_impact_suggests_reverse_dependencies_and_likely_tests(tmp_path: Path, monkeypatch, capsys) -> None:
    _write(tmp_path / "src" / "store.ts", "export function readStore() { return 1; }\n")
    _write(tmp_path / "src" / "jobs.ts", "import { readStore } from './store';\nexport function runJob() { return readStore(); }\n")
    _write(tmp_path / "tests" / "store.test.ts", "import { readStore } from '../src/store';\ntest('store', () => readStore());\n")
    _write(tmp_path / "tests" / "jobs.test.ts", "import { runJob } from '../src/jobs';\ntest('jobs', () => runJob());\n")
    monkeypatch.chdir(tmp_path)

    assert main(["build", "."]) == 0
    capsys.readouterr()

    assert main(["impact", "src/store.ts", "--graph-json", "graph-out/graph.json", "--json"]) == 0
    impact = json.loads(capsys.readouterr().out)

    assert "src/jobs.ts" in impact["impacted_files"]
    assert "tests/store.test.ts" in impact["likely_tests"]
    assert "tests/jobs.test.ts" in impact["likely_tests"]


def test_analysis_rankings_ignore_external_import_nodes() -> None:
    graph = build_graph(
        nodes=[
            {"id": "src_app", "kind": "file", "name": "app.ts", "source_file": "src/app.ts"},
            {"id": "src_store", "kind": "file", "name": "store.ts", "source_file": "src/store.ts"},
            {"id": "src_store_read", "kind": "function", "name": "read", "source_file": "src/store.ts"},
        ],
        edges=[
            {"source": "src_app", "target": "vitest", "relation": "imports", "source_file": "src/app.ts", "confidence": "EXTERNAL_IMPORT"},
            {"source": "src_app", "target": "src_store", "relation": "imports", "source_file": "src/app.ts", "confidence": "EXACT_IMPORT"},
            {"source": "src_store", "target": "src_store_read", "relation": "contains", "source_file": "src/store.ts"},
        ],
    )

    report = analyze(graph, top_n=5)

    assert "vitest" not in {item["id"] for item in report["god_nodes"]}
    assert "vitest" not in {item["id"] for item in report["orphans"]}
    assert all(item["target"] != "vitest" for item in report["surprising_connections"])


