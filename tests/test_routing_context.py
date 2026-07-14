"""Bounded routing context and outbound manifest tests."""
from __future__ import annotations

import json
from pathlib import Path

import networkx as nx

from graphite.routing.context_builder import build_routing_context
from graphite.routing.contracts import TaskRequest
from graphite.routing.settings import RoutingSettings


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _request(root: Path, targets=("src/app.py",), policy="source_allowed") -> TaskRequest:
    return TaskRequest(
        objective="Add a validated feature",
        repository_root=root,
        targets=targets,
        max_input_tokens=8_000,
        max_output_tokens=2_000,
        data_policy=policy,
    )


def _graph() -> nx.DiGraph:
    graph = nx.DiGraph()
    graph.add_node("app", source_file="src/app.py", kind="file")
    graph.add_node("service", source_file="src/service.py", kind="file")
    graph.add_node("test", source_file="tests/test_app.py", kind="file")
    graph.add_edge("app", "service", relation="imports")
    graph.add_edge("test", "app", relation="imports")
    return graph


def test_context_starts_with_targets_then_adds_bounded_graph_neighbors(tmp_path: Path) -> None:
    _write(tmp_path / "src/app.py", "from .service import run\n")
    _write(tmp_path / "src/service.py", "def run(): return 1\n")
    _write(tmp_path / "tests/test_app.py", "def test_app(): assert True\n")

    bundle = build_routing_context(_request(tmp_path), _graph(), RoutingSettings())
    public = bundle.manifest.to_dict()

    assert [item["path"] for item in public["items"]] == [
        "src/app.py",
        "src/service.py",
        "tests/test_app.py",
    ]
    assert public["items"][0]["reason"] == "explicit_target"
    assert {item.path for item in bundle.private_items} == {
        "src/app.py",
        "src/service.py",
        "tests/test_app.py",
    }
    serialized = json.dumps(public)
    assert str(tmp_path) not in serialized
    assert "def run" not in serialized


def test_context_excludes_sensitive_generated_binary_and_configured_paths(tmp_path: Path) -> None:
    _write(tmp_path / "src/app.py", "print('safe')\n")
    _write(tmp_path / ".env", "TOKEN=secret\n")
    _write(tmp_path / "keys/client.pem", "-----BEGIN PRIVATE KEY-----\n")
    _write(tmp_path / "graph-out/graph.json", "{}")
    _write(tmp_path / ".git/config", "private")
    _write(tmp_path / "src/binary.py", b"safe\x00private")
    _write(tmp_path / "src/excluded.py", "private")
    request = _request(
        tmp_path,
        targets=(
            "src/app.py",
            ".env",
            "keys/client.pem",
            "graph-out/graph.json",
            ".git/config",
            "src/binary.py",
            "src/excluded.py",
        ),
    )

    bundle = build_routing_context(
        request,
        _graph(),
        RoutingSettings(),
        exclusions=("src/excluded.py",),
    )

    assert [item.path for item in bundle.private_items] == ["src/app.py"]
    assert bundle.manifest.excluded_count >= 6


def test_metadata_only_policy_never_returns_private_source(tmp_path: Path) -> None:
    _write(tmp_path / "src/app.py", "print('private')\n")

    bundle = build_routing_context(
        _request(tmp_path, policy="metadata_only"),
        _graph(),
        RoutingSettings(),
    )

    assert bundle.private_items == ()
    assert bundle.manifest.items[0].path == "src/app.py"


def test_context_caps_are_deterministic(tmp_path: Path) -> None:
    for name in ("a.py", "b.py", "c.py"):
        _write(tmp_path / "src" / name, f"# {name}\n")
    request = _request(tmp_path, targets=("src/c.py", "src/b.py", "src/a.py"))
    settings = RoutingSettings(max_context_files=2, max_context_bytes=16_384)

    first = build_routing_context(request, nx.DiGraph(), settings)
    second = build_routing_context(request, nx.DiGraph(), settings)

    assert first.manifest.to_dict() == second.manifest.to_dict()
    assert [item.path for item in first.private_items] == ["src/a.py", "src/b.py"]
    assert first.manifest.excluded_count == 1
