"""Smoke tests for graphite core pipeline."""
from __future__ import annotations

import tempfile
from pathlib import Path

from graphite.config import Config
from graphite.ingest import collect_files
from graphite.extract.ast import extract_all
from graphite.graph import build_graph, graph_to_json


def test_end_to_end_on_sample_repo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "src/lib.ts").write_text(
            "export function add(a: number, b: number): number { return a + b; }\n",
            encoding="utf-8",
        )
        (root / "src/app.ts").write_text(
            "import { add } from './lib';\nconsole.log(add(1, 2));\n",
            encoding="utf-8",
        )
        (root / "README.md").write_text("# Sample\n", encoding="utf-8")

        cfg = Config()
        entries = collect_files(root, cfg)
        rel_paths = [e.rel_path for e in entries]
        assert "src/lib.ts" in rel_paths
        assert "src/app.ts" in rel_paths
        assert "README.md" in rel_paths

        result = extract_all(entries, cfg)
        assert len(result.nodes) > 0
        assert any(n["kind"] == "file" for n in result.nodes)

        g = build_graph(result.nodes, result.edges)
        data = graph_to_json(g)
        assert data["metadata"]["node_count"] > 0
