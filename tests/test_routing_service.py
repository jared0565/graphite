"""Offline end-to-end contracts for approval-gated CLI orchestration."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from types import SimpleNamespace
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
import pytest

import graphite.routing.service as service_module
from graphite.routing.context_builder import (
    ContextBundle,
    ManifestItem,
    OutboundManifest,
    PrivateContextItem,
)
from graphite.routing.contracts import (
    CapabilityProfile,
    CapabilitySnapshot,
    CliIdentity,
    Effort,
    PermissionMode,
    ProviderId,
    RiskTier,
)
from graphite.routing.profiles import save_capability_snapshot
from graphite.routing.lifecycle import LifecycleProviderId, ProviderCompatibilityPolicy, ProviderRuntimeIdentity, RuntimeKind
from graphite.routing.lifecycle_service import ProviderLifecycleService
from graphite.routing.lifecycle_storage import LifecycleStore
from graphite.routing.service import RoutingService, RoutingServiceError
from graphite.routing.storage import RepositoryStore

SOURCE_TEXT = "def listing_summary(items):\n    return ', '.join(items)\n"


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    return result.stdout.strip()


def _context() -> ContextBundle:
    item = ManifestItem(
        "src/listing_summary.py", len(SOURCE_TEXT), "1" * 64, "explicit_target"
    )
    return ContextBundle(
        OutboundManifest("1", (item,), len(SOURCE_TEXT), 0, "3" * 64),
        (PrivateContextItem("src/listing_summary.py", SOURCE_TEXT),),
    )


def _snapshot(now: int) -> CapabilitySnapshot:
    identity = CliIdentity(ProviderId.CODEX, "a" * 64, "0.144.1", "1.0.0")
    profile = CapabilityProfile(
        ProviderId.CODEX,
        "gpt-5.6-codex",
        "gpt-5.6-codex",
        "2026-07-18.1",
        ("code", "reasoning", "architecture"),
        262_144,
        (Effort.XHIGH,),
        RiskTier.HIGH,
        PermissionMode.WORKSPACE_WRITE,
    )
    return CapabilitySnapshot(1, now, now + 10**9, identity, profile)


def _review_snapshot(now: int) -> CapabilitySnapshot:
    identity = CliIdentity(ProviderId.CLAUDE_CODE, "b" * 64, "2.1.208", "1.0.0")
    profile = CapabilityProfile(
        ProviderId.CLAUDE_CODE,
        "sonnet",
        "claude-sonnet-4-6",
        "2026-07-18.1",
        ("code", "reasoning"),
        200_000,
        (Effort.HIGH,),
        RiskTier.HIGH,
        PermissionMode.READ_ONLY,
    )
    return CapabilitySnapshot(1, now, now + 10**9, identity, profile)


@dataclass(frozen=True)
class FakeResult:
    effective_model: str = "gpt-5.6-codex"
    message: str = "ephemeral provider summary"
    input_tokens: int = 100
    output_tokens: int = 20
    duration_seconds: float = 0.25


def _service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    validator=lambda _worktree: True,
    lifecycle: bool = False,
) -> tuple[RoutingService, Path, dict[str, object]]:
    root = tmp_path / "repo"
    target = root / "src" / "listing_summary.py"
    target.parent.mkdir(parents=True)
    target.write_text(SOURCE_TEXT, encoding="utf-8")
    (root / ".gitignore").write_text(".graphite/\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Graphite Tests")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    graph = nx.DiGraph()
    graph.add_node("listing_summary", source_file="src/listing_summary.py", community=1)
    monkeypatch.setattr(service_module, "check_graph_freshness", lambda *_: {"stale": False})
    monkeypatch.setattr(
        service_module,
        "load_validated_graph_bundle",
        lambda *_args, **_kwargs: (
            {"schema_version": "1", "nodes": [], "edges": []}, graph
        ),
    )
    monkeypatch.setattr(service_module, "build_routing_context", lambda *_: _context())
    executable = tmp_path / "bin" / "codex.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"fake codex")
    credentials = tmp_path / "codex-home"
    credentials.mkdir()
    captured: dict[str, object] = {}
    snapshot = _snapshot(int(service_module.time.time()) - 10)
    captured["snapshot"] = snapshot
    lifecycle_coordinator = None
    lifecycle_boundaries = None
    runtime_identity_loader = None
    routing_store = RepositoryStore(root)
    routing_store.initialize()
    save_capability_snapshot(routing_store, snapshot)
    if lifecycle:
        runtime_identity = ProviderRuntimeIdentity(
            LifecycleProviderId.CODEX,
            RuntimeKind.LOCAL_CLI,
            "0.144.1",
            "a" * 64,
            None,
            None,
            ("credential_health", "structured_output", "version"),
            "1.0.0",
            int(service_module.time.time()) - 9,
        )
        captured["runtime_identity"] = runtime_identity
        boundary = "9" * 64
        lifecycle_store = LifecycleStore(root)
        lifecycle_store.initialize()
        lifecycle_coordinator = ProviderLifecycleService(lifecycle_store, routing_store)
        lifecycle_coordinator.observe(
            boundary_digest=boundary,
            identity=runtime_identity,
            policy=ProviderCompatibilityPolicy(
                LifecycleProviderId.CODEX,
                RuntimeKind.LOCAL_CLI,
                "1.0.0",
                "0.100.0",
                "1.0.0",
                ("credential_health", "structured_output"),
            ),
        )
        routing_store.save_lifecycle_snapshot_binding(
            capability_snapshot_digest=snapshot.digest,
            lifecycle_identity_digest=runtime_identity.digest,
            bound_at=runtime_identity.observed_at,
        )
        lifecycle_coordinator.activate(
            boundary_digest=boundary,
            lifecycle_identity_digest=runtime_identity.digest,
            capability_snapshot_digest=snapshot.digest,
            activated_at=runtime_identity.observed_at + 1,
        )
        lifecycle_boundaries = {ProviderId.CODEX: boundary}

        def load_runtime_identity(_provider: ProviderId) -> ProviderRuntimeIdentity:
            value = captured["runtime_identity"]
            assert isinstance(value, ProviderRuntimeIdentity)
            return value

        runtime_identity_loader = load_runtime_identity
    service_ref: dict[str, RoutingService] = {}

    def execute(**kwargs: object) -> FakeResult:
        captured.update(kwargs)
        service = service_ref["service"]
        with service.store._connect() as connection:
            row = connection.execute(
                "SELECT approval_id FROM approvals WHERE status='consumed'"
            ).fetchone()
        captured["consumed_approval_id"] = row[0]
        workspace = Path(kwargs["workspace"])
        (workspace / "src" / "listing_summary.py").write_text(
            "def listing_summary(items):\n    return ' | '.join(items)\n",
            encoding="utf-8",
        )
        return FakeResult()

    service = RoutingService(
        root,
        state_dir=tmp_path / "machine",
        identity_loader=lambda _provider: snapshot.identity,
        executables={ProviderId.CODEX: executable},
        credential_homes={ProviderId.CODEX: credentials},
        executors={ProviderId.CODEX: execute},
        validator=validator,
        lifecycle_service=lifecycle_coordinator,
        lifecycle_boundaries=lifecycle_boundaries,
        runtime_identity_loader=runtime_identity_loader,
    )
    service_ref["service"] = service
    service.store.initialize()
    return service, root, captured


def test_a_git_failure_keeps_its_cause_all_the_way_to_the_routing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The routing error is what reaches a CI log, and it is raised `from None`
    from a `DiffPolicyError` that was itself raised `from None`. Without an
    explicit hand-off the diagnostic dies at the second hop and the log shows a
    bare `git_unavailable` -- which is exactly what graphite#37's one sighting
    shows, and why it could not be taken further."""
    from graphite.routing.diff_policy import DiffPolicyError

    service, _root, _extras = _service(tmp_path, monkeypatch)

    def _explode(*_arguments: object, **_keywords: object):
        raise DiffPolicyError("git_unavailable", "GitLaunchError")

    monkeypatch.setattr(service_module, "collect_diff_evidence", _explode)
    prepared = service.prepare(_recommend(service))

    with pytest.raises(RoutingServiceError) as excinfo:
        service.run_approved(prepared, approval_granted=True)

    assert excinfo.value.code == "git_unavailable", "the failure taxonomy must not shift"
    assert "GitLaunchError" in str(excinfo.value)


def _recommend(service: RoutingService):
    return service.recommend(
        objective="Implement the isolated formatting helper",
        targets=("src/listing_summary.py",),
    )


def test_recommendation_uses_only_verified_cli_snapshot_and_is_public(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, _ = _service(tmp_path, monkeypatch)
    recommendation = _recommend(service)
    public = recommendation.to_dict()
    serialized = json.dumps(public, sort_keys=True)
    assert public["provider"] == "codex"
    assert public["requested_model"] == public["effective_model"] == "gpt-5.6-codex"
    assert public["effort"] == "xhigh"
    assert public["permission_mode"] == "workspace-write"
    assert recommendation.manual_handoff is False
    assert SOURCE_TEXT not in serialized
    assert str(root.resolve()) not in serialized
    assert "ollama" not in serialized.casefold()


def test_prepare_run_binds_prompt_worktree_cli_and_consumed_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, captured = _service(tmp_path, monkeypatch)
    recommendation = _recommend(service)
    prepared = service.prepare(recommendation)
    snapshot = captured["snapshot"]
    assert prepared.worktree.root != root
    assert prepared.manifest.repository_commit == _git(root, "rev-parse", "HEAD")
    assert prepared.manifest.capability_snapshot_digest == snapshot.digest
    assert SOURCE_TEXT not in repr(prepared)
    with pytest.raises(RoutingServiceError, match="^transition_replay$"):
        service.prepare(recommendation)

    result = service.run_approved(prepared, approval_granted=True)
    assert captured["prompt"] == prepared.prompt.body
    assert captured["consumed_approval_id"] == prepared.manifest.approval_id
    assert result.receipt.provider is ProviderId.CODEX
    assert result.receipt.validation_outcome == "passed"
    assert result.receipt.changed_file_count == 1
    assert result.text not in repr(result)
    persisted = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert result.text.encode() not in persisted
    with pytest.raises(RoutingServiceError, match="^transition_replay$"):
        service.run_approved(prepared, approval_granted=True)


def test_run_requires_authority_before_provider_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _root, captured = _service(tmp_path, monkeypatch)
    prepared = service.prepare(_recommend(service))
    with pytest.raises(RoutingServiceError, match="^approval_required$"):
        service.run_approved(prepared, approval_granted=False)
    assert "prompt" not in captured


def test_lifecycle_enabled_run_rechecks_exact_runtime_before_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _root, captured = _service(tmp_path, monkeypatch, lifecycle=True)
    prepared = service.prepare(_recommend(service))
    runtime_identity = captured["runtime_identity"]
    captured["runtime_identity"] = ProviderRuntimeIdentity(
        runtime_identity.provider,
        runtime_identity.runtime_kind,
        runtime_identity.version,
        "f" * 64,
        runtime_identity.model_identity_digest,
        runtime_identity.routing_policy_digest,
        runtime_identity.capabilities,
        runtime_identity.policy_version,
        runtime_identity.observed_at + 2,
    )

    with pytest.raises(RoutingServiceError, match="^lifecycle_identity_changed$"):
        service.run_approved(prepared, approval_granted=True)
    assert "prompt" not in captured


def test_lifecycle_enabled_run_binds_approval_and_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _root, captured = _service(tmp_path, monkeypatch, lifecycle=True)
    prepared = service.prepare(_recommend(service))
    result = service.run_approved(prepared, approval_granted=True)
    runtime_identity = captured["runtime_identity"]
    assert isinstance(runtime_identity, ProviderRuntimeIdentity)
    assert service.store.lifecycle_approval_binding_details(
        prepared.manifest.approval_id
    ) == (prepared.manifest.capability_snapshot_digest, runtime_identity.digest)
    assert service.store.lifecycle_identity_binding(
        authority_kind="attempt", authority_id=prepared.attempt_id
    ) == runtime_identity.digest
    assert result.receipt.validation_outcome == "passed"


def test_accept_creates_commit_without_moving_source_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, _ = _service(tmp_path, monkeypatch)
    source_head = _git(root, "rev-parse", "HEAD")
    prepared = service.prepare(_recommend(service))
    service.run_approved(prepared, approval_granted=True)
    with pytest.raises(RoutingServiceError, match="^accept_authority_required$"):
        service.accept(prepared.task_id, authority_granted=False)
    accepted = service.accept(prepared.task_id, authority_granted=True)
    assert accepted["integration"] == "explicit_cherry_pick_required"
    assert accepted["commit_id"] != source_head
    assert _git(root, "rev-parse", "HEAD") == source_head


def test_failed_validation_blocks_accept_then_reject_and_cleanup_are_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _root, _ = _service(
        tmp_path, monkeypatch, validator=lambda _worktree: False
    )
    prepared = service.prepare(_recommend(service))
    result = service.run_approved(prepared, approval_granted=True)
    assert result.receipt.validation_outcome == "failed"
    with pytest.raises(RoutingServiceError, match="^validation_not_passed$"):
        service.accept(prepared.task_id, authority_granted=True)
    assert service.reject(prepared.task_id, authority_granted=True)["status"] == "rejected"
    assert prepared.worktree.root.exists()
    assert service.cleanup(prepared.task_id, authority_granted=True)["status"] == "cleaned"
    assert not prepared.worktree.root.exists()


def test_v4_attempt_and_validation_bind_provider_model_and_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _root, _ = _service(tmp_path, monkeypatch)
    prepared = service.prepare(_recommend(service))
    result = service.run_approved(prepared, approval_granted=True)
    with sqlite3.connect(service.store.path) as connection:
        attempt = connection.execute(
            "SELECT provider,requested_model,effective_model,status,execution_id "
            "FROM cli_execution_attempts"
        ).fetchone()
        validation = connection.execute(
            "SELECT diff_hash,changed_file_count,outcome FROM validation_results"
        ).fetchone()
    assert attempt == (
        "codex", "gpt-5.6-codex", "gpt-5.6-codex", "completed",
        result.receipt.execution_id,
    )
    assert validation == (result.diff_hash, 1, "passed")


def test_policy_surface_contains_no_model_inventory_refresh_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _root, _ = _service(tmp_path, monkeypatch)
    assert service.policy() == {
        "policy_version": "3",
        "requested_action": "inspect",
        "execution_authority": "single_use_approval_required",
        "automatic_execution": False,
    }


def test_review_uses_other_provider_read_only_separate_worktree_approval_and_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _root, captured = _service(tmp_path, monkeypatch)
    high_risk = service.recommend(
        objective="Fix authentication authorization security controls",
        targets=("src/listing_summary.py",),
    )
    primary = service.prepare(high_risk)
    primary_result = service.run_approved(primary, approval_granted=True)
    review_snapshot = _review_snapshot(int(service_module.time.time()) - 10)
    save_capability_snapshot(service.store, review_snapshot)
    executable = tmp_path / "bin" / "claude.exe"
    executable.write_bytes(b"fake claude")
    credentials = tmp_path / "claude-home"
    credentials.mkdir()
    review_calls: list[dict[str, object]] = []

    def review_executor(**kwargs: object):
        review_calls.append(kwargs)
        return SimpleNamespace(
            effective_model="claude-sonnet-4-6",
            message="No critical findings.",
            input_tokens=80,
            output_tokens=12,
            duration_seconds=0.2,
        )

    identities = {
        ProviderId.CODEX: captured["snapshot"].identity,
        ProviderId.CLAUDE_CODE: review_snapshot.identity,
    }
    service._identity_loader = lambda provider: identities[provider]
    service._executables[ProviderId.CLAUDE_CODE] = executable
    service._credential_homes[ProviderId.CLAUDE_CODE] = credentials
    service._executors[ProviderId.CLAUDE_CODE] = review_executor

    review = service.prepare_review(primary.task_id)
    assert review.manifest.provider is ProviderId.CLAUDE_CODE
    assert review.manifest.permission_mode is PermissionMode.READ_ONLY
    assert review.worktree.root != primary.worktree.root
    assert review.manifest.approval_id != primary.manifest.approval_id
    assert review.manifest.context_manifest_hash == primary_result.diff_hash
    result = service.run_review_approved(review, approval_granted=True)
    assert result.receipt.changed_file_count == 0
    assert review_calls[0]["permission_mode"] is PermissionMode.READ_ONLY
    with sqlite3.connect(service.store.path) as connection:
        link = connection.execute(
            "SELECT review_attempt_id,primary_attempt_id,primary_diff_hash FROM review_links"
        ).fetchone()
    assert link == (review.attempt_id, primary.attempt_id, primary_result.diff_hash)
