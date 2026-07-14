"""Offline routing-service and persistence-boundary contract tests."""
from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import networkx as nx
import pytest

import graphite.routing.service as service_module
from graphite.routing.context_builder import (
    ContextBundle,
    ManifestItem,
    OutboundManifest,
    PrivateContextItem,
)
from graphite.routing.contracts import Effort, ExecutionOutcome, ExecutionReceipt
from graphite.routing.registry import BUNDLED_PROFILES, InventoryModel, RegistrySnapshot
from graphite.routing.service import RoutingService


MODEL_DIGESTS = {
    "kimi-k2.7-code:cloud": "a" * 64,
    "minimax-m2.7:cloud": "b" * 64,
    "nemotron-3-super:cloud": "c" * 64,
    "minimax-m3:cloud": "d" * 64,
}
SOURCE_TEXT = "def listing_summary(items):\n    return ', '.join(items)\n"


def _context() -> ContextBundle:
    items = (
        ManifestItem("src/listing_summary.py", len(SOURCE_TEXT), "1" * 64, "explicit_target"),
        ManifestItem("tests/test_listing_summary.py", 24, "2" * 64, "dependency_neighbor"),
    )
    manifest = OutboundManifest("1", items, len(SOURCE_TEXT) + 24, 0, "3" * 64)
    return ContextBundle(
        manifest,
        (
            PrivateContextItem("src/listing_summary.py", SOURCE_TEXT),
            PrivateContextItem("tests/test_listing_summary.py", "def test_summary(): pass\n"),
        ),
    )


def _inventory() -> RegistrySnapshot:
    models = [
        InventoryModel(
            model_id,
            digest,
            BUNDLED_PROFILES[model_id].profile.context_window_tokens,
            BUNDLED_PROFILES[model_id].profile.capabilities,
        )
        for model_id, digest in MODEL_DIGESTS.items()
    ]
    models.append(InventoryModel("grok-untrusted:cloud", "e" * 64, 262_144, ("code",)))
    return RegistrySnapshot("1", 1, 10**12, tuple(models))


def _service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[RoutingService, Path]:
    root = tmp_path / "repo"
    target = root / "src" / "listing_summary.py"
    target.parent.mkdir(parents=True)
    target.write_text(SOURCE_TEXT, encoding="utf-8")
    graph = nx.DiGraph()
    graph.add_node("listing_summary", source_file="src/listing_summary.py", community=1)
    graph.add_node("test_listing_summary", source_file="tests/test_listing_summary.py", community=1)
    graph.add_edge("test_listing_summary", "listing_summary")
    monkeypatch.setattr(service_module, "check_graph_freshness", lambda *_: {"stale": False})
    monkeypatch.setattr(
        service_module,
        "load_validated_graph_bundle",
        lambda *_args, **_kwargs: ({"schema_version": "1", "nodes": [], "edges": []}, graph),
    )
    monkeypatch.setattr(service_module, "build_routing_context", lambda *_: _context())
    monkeypatch.setattr(service_module, "load_cached_registry", lambda *_args, **_kwargs: _inventory())

    def reject_socket(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("recommendation_must_remain_offline")

    monkeypatch.setattr(socket, "socket", reject_socket)
    return RoutingService(root, state_dir=tmp_path / "machine"), root


def test_recommendation_is_offline_allowlisted_and_public(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _service(tmp_path, monkeypatch)

    recommendation = service.recommend(
        objective="Review this isolated formatting helper",
        targets=("src/listing_summary.py",),
    )

    public = recommendation.to_dict()
    serialized = json.dumps(public, sort_keys=True)
    assert recommendation.model_id == "kimi-k2.7-code:cloud"
    assert recommendation.effort == "default"
    assert recommendation.policy_version == "2"
    assert "grok-untrusted:cloud" not in serialized
    assert not list(root.rglob("events.sqlite3"))
    assert SOURCE_TEXT not in serialized
    assert str(root.resolve()) not in serialized
    forbidden_keys = {"source", "prompt", "response", "digest", "database", "database_location"}

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {key for child in value.values() for key in keys(child)}
        if isinstance(value, list):
            return {key for child in value for key in keys(child)}
        return set()

    assert keys(public).isdisjoint(forbidden_keys)


def test_execution_returns_ephemeral_text_and_persists_only_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _service(tmp_path, monkeypatch)
    recommendation = service.recommend(
        objective="Review this isolated formatting helper",
        targets=("src/listing_summary.py",),
    )
    secret_text = "BOUNDED EPHEMERAL SUGGESTION"
    receipt = ExecutionReceipt(
        execution_id="exec-1",
        approval_id="approval-1",
        model_id="kimi-k2.7-code:cloud",
        effort=Effort.DEFAULT,
        outcome=ExecutionOutcome.SUCCEEDED,
        input_tokens=20,
        output_tokens=4,
        latency_ms=10,
        prompt_hash="4" * 64,
        response_hash="5" * 64,
        failure_reason=None,
    )
    monkeypatch.setattr(
        service_module,
        "execute_ollama",
        lambda **_kwargs: SimpleNamespace(text=secret_text, receipt=receipt),
    )

    result = service.execute_approved(recommendation)

    assert result.text == secret_text
    assert result.receipt.outcome is ExecutionOutcome.SUCCEEDED
    public = result.to_public_dict()
    assert public == receipt.to_dict()
    assert secret_text not in json.dumps(public, sort_keys=True)
    persisted = b"".join(path.read_bytes() for path in root.rglob("*") if path.is_file())
    assert secret_text.encode() not in persisted
