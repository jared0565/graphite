"""Offline routing-service and persistence-boundary contract tests."""
from __future__ import annotations

import json
import socket
import sqlite3
from dataclasses import replace
from pathlib import Path

import networkx as nx
import pytest

import graphite.routing.service as service_module
from graphite.routing.approval import ApprovalError
from graphite.routing.context_builder import (
    ContextBundle,
    ManifestItem,
    OutboundManifest,
    PrivateContextItem,
)
from graphite.routing.contracts import Effort, ExecutionOutcome, ExecutionReceipt
from graphite.routing.ollama_executor import (
    ExecutionResult,
    ExecutorError,
    canonical_provider_request,
)
from graphite.routing.registry import BUNDLED_PROFILES, InventoryModel, RegistrySnapshot
from graphite.routing.service import RoutingService
from graphite.routing.storage import StorageError


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


def _successful_result(
    kwargs: dict[str, object], text: str, *, execution_id: str
) -> ExecutionResult:
    manifest = kwargs["current_manifest"]
    authority = kwargs["authority"]
    settings = kwargs["settings"]
    authority.consume(
        kwargs["signed_approval"],
        manifest,
        repository_quota_tokens=settings.repository_quota_tokens,
        machine_quota_tokens=settings.machine_quota_tokens,
    )
    request = canonical_provider_request(
        manifest=manifest,
        context=kwargs["context"],
        objective=kwargs["objective"],
    )
    return ExecutionResult(
        text,
        ExecutionReceipt(
            execution_id=execution_id,
            approval_id=manifest.approval_id,
            model_id=manifest.model_id,
            effort=manifest.effort,
            outcome=ExecutionOutcome.SUCCEEDED,
            input_tokens=20,
            output_tokens=4,
            latency_ms=10,
            prompt_hash=request.prompt_hash,
            response_hash=service_module.hashlib.sha256(text.encode()).hexdigest(),
            failure_reason=None,
        ),
    )


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
    service, _root = _service(tmp_path, monkeypatch)
    recommendation = service.recommend(
        objective="Review this isolated formatting helper",
        targets=("src/listing_summary.py",),
    )
    secret_text = "BOUNDED EPHEMERAL SUGGESTION"
    captured: dict[str, object] = {}

    def execute(**kwargs: object) -> ExecutionResult:
        captured.update(kwargs)
        return _successful_result(kwargs, secret_text, execution_id="exec-1")

    monkeypatch.setattr(service_module, "execute_ollama", execute)

    result = service.execute_approved(recommendation)

    assert result.text == secret_text
    assert secret_text not in repr(result)
    assert result.receipt.outcome is ExecutionOutcome.SUCCEEDED
    public = result.to_public_dict()
    assert public == result.receipt.to_dict()
    assert secret_text not in json.dumps(public, sort_keys=True)
    persisted = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert secret_text.encode() not in persisted
    with sqlite3.connect(service.store.path) as connection:
        execution = connection.execute(
            "SELECT model_id, effort, status, actual_input_tokens, actual_output_tokens "
            "FROM executions"
        ).fetchone()
        receipt_row = connection.execute(
            "SELECT approval_id, model_id, effort, outcome, input_tokens, output_tokens "
            "FROM execution_receipts"
        ).fetchone()
        attempt = connection.execute(
            "SELECT status, execution_id FROM execution_attempts"
        ).fetchone()
    assert execution == ("kimi-k2.7-code:cloud", "default", "succeeded", 20, 4)
    assert receipt_row[1:] == ("kimi-k2.7-code:cloud", "default", "succeeded", 20, 4)
    assert receipt_row[0] == captured["current_manifest"].approval_id
    assert attempt == ("completed", "exec-1")
    assert service.store.approval_status(result.receipt.approval_id) == "consumed"
    with pytest.raises(ApprovalError, match="approval_reused"):
        captured["authority"].consume(
            captured["signed_approval"],
            captured["current_manifest"],
            repository_quota_tokens=captured["settings"].repository_quota_tokens,
            machine_quota_tokens=captured["settings"].machine_quota_tokens,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda receipt, _manifest: replace(receipt, approval_id="approval-other"),
        lambda receipt, _manifest: replace(receipt, model_id="minimax-m2.7:cloud"),
        lambda receipt, _manifest: replace(receipt, effort=Effort.LOW),
        lambda receipt, _manifest: replace(
            receipt, outcome=ExecutionOutcome.PROVIDER_FAILED
        ),
        lambda receipt, manifest: replace(
            receipt, input_tokens=manifest.max_input_tokens + 1
        ),
        lambda receipt, manifest: replace(
            receipt, output_tokens=manifest.max_output_tokens + 1
        ),
        lambda receipt, _manifest: replace(receipt, response_hash="f" * 64),
        lambda receipt, _manifest: replace(receipt, prompt_hash="f" * 64),
    ],
)
def test_executor_receipt_mismatch_fails_closed_without_success_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation
) -> None:
    service, _root = _service(tmp_path, monkeypatch)
    recommendation = service.recommend(
        objective="Review this isolated formatting helper",
        targets=("src/listing_summary.py",),
    )
    text = "PRIVATE PROVIDER BODY"

    def execute(**kwargs: object) -> ExecutionResult:
        manifest = kwargs["current_manifest"]
        result = _successful_result(kwargs, text, execution_id="exec-invalid")
        return ExecutionResult(text, mutation(result.receipt, manifest))

    monkeypatch.setattr(service_module, "execute_ollama", execute)
    with pytest.raises(ExecutorError, match="^executor_receipt_invalid$") as caught:
        service.execute_approved(recommendation)
    assert str(caught.value) == "executor_receipt_invalid"
    assert text not in str(caught.value)
    with sqlite3.connect(service.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM execution_receipts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM execution_evidence").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM execution_attempts").fetchone()[0] == "failed"


def test_provider_failure_leaves_sanitized_durable_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _root = _service(tmp_path, monkeypatch)
    recommendation = service.recommend(
        objective="Review this isolated formatting helper",
        targets=("src/listing_summary.py",),
    )

    def fail(**_kwargs: object) -> ExecutionResult:
        raise ExecutorError("provider_unavailable")

    monkeypatch.setattr(service_module, "execute_ollama", fail)
    with pytest.raises(ExecutorError, match="^provider_unavailable$"):
        service.execute_approved(recommendation)
    with sqlite3.connect(service.store.path) as connection:
        attempt = connection.execute(
            "SELECT status, failure_reason FROM execution_attempts"
        ).fetchone()
        approval = connection.execute("SELECT status FROM approvals").fetchone()[0]
    assert attempt == ("failed", "provider_unavailable")
    assert approval == "issued"


def test_unconsumed_success_receipt_is_rejected_as_authority_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _root = _service(tmp_path, monkeypatch)
    recommendation = service.recommend(
        objective="Review this isolated formatting helper",
        targets=("src/listing_summary.py",),
    )
    text = "UNAUTHORIZED SUCCESS BODY"

    def execute(**kwargs: object) -> ExecutionResult:
        manifest = kwargs["current_manifest"]
        request = canonical_provider_request(
            manifest=manifest,
            context=kwargs["context"],
            objective=kwargs["objective"],
        )
        return ExecutionResult(
            text,
            ExecutionReceipt(
                "exec-issued", manifest.approval_id, manifest.model_id, manifest.effort,
                ExecutionOutcome.SUCCEEDED, 2, 1, 5, request.prompt_hash,
                service_module.hashlib.sha256(text.encode()).hexdigest(), None,
            ),
        )

    monkeypatch.setattr(service_module, "execute_ollama", execute)
    with pytest.raises(StorageError, match="^approval_not_consumed$"):
        service.execute_approved(recommendation)
    with sqlite3.connect(service.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM execution_receipts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM execution_evidence").fetchone()[0] == 0
        assert connection.execute(
            "SELECT status, failure_reason FROM execution_attempts"
        ).fetchone() == ("failed", "approval_not_consumed")
        assert connection.execute("SELECT status FROM approvals").fetchone()[0] == "issued"


def test_completion_failure_leaves_recoverable_trace_without_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _service(tmp_path, monkeypatch)
    recommendation = service.recommend(
        objective="Review this isolated formatting helper",
        targets=("src/listing_summary.py",),
    )
    text = "UNPERSISTED PRIVATE BODY"
    captured: dict[str, ExecutionReceipt] = {}

    def execute(**kwargs: object) -> ExecutionResult:
        result = _successful_result(kwargs, text, execution_id="exec-1")
        captured["receipt"] = result.receipt
        return result

    monkeypatch.setattr(service_module, "execute_ollama", execute)
    monkeypatch.setattr(
        service.store,
        "finalize_execution_attempt",
        lambda **_kwargs: (_ for _ in ()).throw(StorageError("storage_unavailable")),
    )
    with pytest.raises(StorageError, match="^execution_persistence_failed$"):
        service.execute_approved(recommendation)
    with sqlite3.connect(service.store.path) as connection:
        attempt = connection.execute(
            "SELECT attempt_id, status, failure_reason, max_input_tokens, "
            "max_output_tokens, expected_prompt_hash FROM execution_attempts"
        ).fetchone()
        assert attempt[1:5] == (
            "persistence_failed", "execution_persistence_failed",
            service.settings.max_input_tokens, service.settings.max_output_tokens,
        )
        assert attempt[5] == canonical_provider_request(
            manifest=service_module.ApprovalManifest(
                approval_id="placeholder", task_id="placeholder", decision_id="placeholder",
                graph_fingerprint="a" * 64, context_manifest_hash=_context().manifest.manifest_hash,
                inventory_digest=MODEL_DIGESTS["kimi-k2.7-code:cloud"],
                model_id="kimi-k2.7-code:cloud", effort=Effort.DEFAULT,
                max_input_tokens=service.settings.max_input_tokens,
                max_output_tokens=service.settings.max_output_tokens,
                policy_version="2",
                issued_at=1, expires_at=2, nonce="placeholder",
            ),
            context=_context(), objective="Review this isolated formatting helper",
        ).prompt_hash
        assert connection.execute(
            "SELECT execution_id, prompt_hash, response_hash FROM staged_execution_receipts"
        ).fetchone()[0] == "exec-1"
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    with pytest.raises(StorageError, match="^execution_attempt_conflict$"):
        service.store.stage_execution_receipt(
            attempt_id=attempt[0],
            receipt=replace(captured["receipt"], response_hash="f" * 64),
            inventory_digest=MODEL_DIGESTS["kimi-k2.7-code:cloud"],
            staged_at=100,
        )
    persisted = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert text.encode() not in persisted
    fresh = RoutingService(root, state_dir=tmp_path / "machine")
    assert fresh.recoverable_attempt_ids() == (attempt[0],)
    first = fresh.reconcile_execution(attempt[0])
    second = fresh.reconcile_execution(attempt[0])
    assert first == second
    assert first["execution_id"] == "exec-1"
    assert fresh.recoverable_attempt_ids() == ()
    with sqlite3.connect(fresh.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM execution_receipts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM execution_evidence").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM staged_execution_receipts").fetchone()[0] == 0


def test_fallback_staging_failure_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _root = _service(tmp_path, monkeypatch)
    recommendation = service.recommend(
        objective="Review this isolated formatting helper",
        targets=("src/listing_summary.py",),
    )
    text = "NEVER PERSIST THIS FALLBACK BODY"
    monkeypatch.setattr(
        service_module, "execute_ollama",
        lambda **kwargs: _successful_result(kwargs, text, execution_id="exec-fallback"),
    )
    monkeypatch.setattr(
        service.store, "finalize_execution_attempt",
        lambda **_kwargs: (_ for _ in ()).throw(StorageError("storage_unavailable")),
    )
    monkeypatch.setattr(
        service.store, "stage_execution_receipt",
        lambda **_kwargs: (_ for _ in ()).throw(StorageError("storage_unavailable")),
    )
    with pytest.raises(StorageError, match="^execution_persistence_failed$") as caught:
        service.execute_approved(recommendation)
    assert text not in str(caught.value)
    persisted = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert text.encode() not in persisted
