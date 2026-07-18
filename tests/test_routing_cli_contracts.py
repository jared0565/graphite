"""Provider-neutral CLI identity and authority contract tests."""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from graphite.routing.contracts import (
    CapabilityProfile,
    CapabilitySnapshot,
    CliApprovalManifest,
    CliIdentity,
    Effort,
    ExecutionOutcome,
    ExecutionReceipt,
    PermissionMode,
    ProviderId,
    RiskTier,
)


def _identity(**changes: object) -> CliIdentity:
    values: dict[str, object] = {
        "provider": ProviderId.CLAUDE_CODE,
        "executable_sha256": "a" * 64,
        "cli_version": "2.1.208",
        "adapter_protocol_version": "1.0.0",
    }
    values.update(changes)
    return CliIdentity(**values)


def _profile(**changes: object) -> CapabilityProfile:
    values: dict[str, object] = {
        "provider": ProviderId.CLAUDE_CODE,
        "requested_model": "sonnet",
        "effective_model": "claude-sonnet-5",
        "profile_version": "2026-07-18.1",
        "capabilities": ("code", "reasoning"),
        "context_window_tokens": 200_000,
        "supported_efforts": (Effort.LOW, Effort.MEDIUM, Effort.HIGH),
        "risk_ceiling": RiskTier.MEDIUM,
        "permission_mode": PermissionMode.WORKSPACE_WRITE,
    }
    values.update(changes)
    return CapabilityProfile(**values)


def _snapshot(**changes: object) -> CapabilitySnapshot:
    values: dict[str, object] = {
        "schema_version": 1,
        "verified_at": 1_700_000_000,
        "expires_at": 1_700_086_400,
        "identity": _identity(),
        "profile": _profile(),
    }
    values.update(changes)
    return CapabilitySnapshot(**values)


def test_cli_contracts_are_frozen_and_snapshot_digest_is_canonical() -> None:
    snapshot = _snapshot()
    reordered = _snapshot(profile=_profile(capabilities=("reasoning", "code")))

    assert snapshot.digest == reordered.digest
    assert len(snapshot.digest) == 64
    assert json.loads(json.dumps(snapshot.to_dict()))["identity"]["provider"] == "claude-code"
    with pytest.raises(FrozenInstanceError):
        snapshot.expires_at = 1


@pytest.mark.parametrize(
    ("factory", "changes", "code"),
    [
        (_identity, {"cli_version": "latest"}, "cli_version_invalid"),
        (_identity, {"executable_sha256": "A" * 64}, "executable_sha256_invalid"),
        (_profile, {"effective_model": ""}, "effective_model_invalid"),
        (_profile, {"supported_efforts": (Effort.HIGH, Effort.HIGH)}, "effort_invalid"),
        (_profile, {"capabilities": ("code", "code")}, "capabilities_invalid"),
        (_snapshot, {"expires_at": 1_700_000_000}, "snapshot_time_invalid"),
    ],
)
def test_cli_contracts_reject_ambiguous_or_malformed_identity(
    factory, changes: dict[str, object], code: str
) -> None:
    with pytest.raises(ValueError, match=code):
        factory(**changes)


def test_cli_approval_manifest_serializes_only_bound_public_identity() -> None:
    manifest = CliApprovalManifest(
        approval_id="approval-cli-1",
        task_id="task-1",
        decision_id="decision-1",
        provider=ProviderId.CLAUDE_CODE,
        requested_model="sonnet",
        effective_model="claude-sonnet-5",
        effort=Effort.HIGH,
        cli_executable_sha256="a" * 64,
        cli_version="2.1.208",
        adapter_protocol_version="1.0.0",
        capability_snapshot_digest="b" * 64,
        graph_fingerprint="c" * 64,
        context_manifest_hash="d" * 64,
        repository_commit="e" * 40,
        worktree_id="worktree-1",
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        max_input_tokens=8_000,
        max_output_tokens=2_000,
        policy_version="1",
        issued_at=100,
        expires_at=200,
        nonce="nonce-cli-1",
    )

    public = manifest.to_dict()

    assert list(public) == list(manifest._public_fields)
    assert public["provider"] == "claude-code"
    assert public["permission_mode"] == "workspace-write"
    serialized = json.dumps(public)
    for forbidden in ("api_key", "password", "source", "response", "F:\\"):
        assert forbidden not in serialized.casefold()


def test_execution_receipt_carries_only_sanitized_cli_and_diff_evidence() -> None:
    receipt = ExecutionReceipt(
        execution_id="execution-cli-1",
        approval_id="approval-cli-1",
        model_id="sonnet",
        effort=Effort.HIGH,
        outcome=ExecutionOutcome.SUCCEEDED,
        input_tokens=1_000,
        output_tokens=200,
        latency_ms=2_500,
        prompt_hash="a" * 64,
        response_hash="b" * 64,
        failure_reason=None,
        provider=ProviderId.CLAUDE_CODE,
        effective_model="claude-sonnet-5",
        cli_version="2.1.208",
        changed_file_count=2,
        changed_byte_count=512,
        validation_outcome="passed",
    )

    public = receipt.to_dict()

    assert public["provider"] == "claude-code"
    assert public["effective_model"] == "claude-sonnet-5"
    assert public["changed_file_count"] == 2
    assert public["validation_outcome"] == "passed"
    with pytest.raises(ValueError, match="validation_outcome_invalid"):
        ExecutionReceipt(**{**public, "validation_outcome": "model_claimed_pass"})
