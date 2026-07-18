"""Immutable routing contracts and conservative settings tests."""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from graphite.config import Config
from graphite.routing.contracts import (
    ApprovalManifest,
    Effort,
    EvidenceProvenance,
    ExecutionOutcome,
    ExecutionReceipt,
    FailureReason,
    ModelProfile,
    RiskTier,
    RoutingDecision,
    TaskCategory,
    TaskProfile,
    TaskRequest,
    VerifiedOutcome,
)
from graphite.routing.settings import RoutingSettings, RoutingSettingsError


def _request(tmp_path: Path, **changes) -> TaskRequest:
    values = {
        "objective": "Add a validated endpoint",
        "repository_root": tmp_path,
        "targets": ("src/api.py",),
        "max_input_tokens": 8_000,
        "max_output_tokens": 2_000,
        "data_policy": "source_allowed",
        "category_hint": None,
    }
    values.update(changes)
    return TaskRequest(**values)


def test_routing_enums_have_stable_public_values() -> None:
    assert TaskCategory.TENANT_ISOLATION.value == "tenant_isolation"
    assert RiskTier.HIGH.value == "high"
    assert Effort.DEFAULT.value == "default"
    assert EvidenceProvenance.MACHINE_VERIFIED.value == "machine_verified"
    assert ExecutionOutcome.PROVIDER_FAILED.value == "provider_failed"
    assert FailureReason.GRAPH_STALE.value == "graph_stale"


def test_contracts_are_frozen_and_serialize_in_stable_order(tmp_path: Path) -> None:
    request = _request(tmp_path)

    with pytest.raises(FrozenInstanceError):
        request.objective = "changed"

    public = request.to_dict()
    assert list(public) == [
        "objective",
        "repository",
        "targets",
        "max_input_tokens",
        "max_output_tokens",
        "data_policy",
        "category_hint",
    ]
    assert public["repository"] == tmp_path.name
    assert str(tmp_path) not in json.dumps(public)


@pytest.mark.parametrize(
    "target",
    ["../secret.py", "C:/private/secret.py", "/private/secret.py", "src/\x00secret.py"],
)
def test_task_request_rejects_unsafe_targets(tmp_path: Path, target: str) -> None:
    with pytest.raises(ValueError, match="target_invalid"):
        _request(tmp_path, targets=(target,))


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("objective", "x" * 4_097, "objective_too_long"),
        ("targets", tuple(f"src/{index}.py" for index in range(65)), "targets_too_many"),
        ("max_input_tokens", -1, "max_input_tokens_invalid"),
        ("max_output_tokens", True, "max_output_tokens_invalid"),
        ("data_policy", "unknown", "data_policy_invalid"),
    ],
)
def test_task_request_rejects_invalid_bounds(
    tmp_path: Path,
    field: str,
    value: object,
    code: str,
) -> None:
    with pytest.raises(ValueError, match=code):
        _request(tmp_path, **{field: value})


def test_model_profile_and_decision_reject_unknown_or_nonfinite_values() -> None:
    with pytest.raises(ValueError, match="effort_invalid"):
        ModelProfile(
            model_id="kimi-k2.7-code:cloud",
            profile_version="1",
            capabilities=("code",),
            context_window_tokens=128_000,
            supported_efforts=("invented",),
            provisional=True,
        )

    with pytest.raises(ValueError, match="score_invalid"):
        RoutingDecision(
            decision_id="decision-1",
            task_id="task-1",
            selected_model="kimi-k2.7-code:cloud",
            effort=Effort.DEFAULT,
            score=float("nan"),
            confidence=0.5,
            alternatives=(),
            reasons=("cold_start",),
            policy_version="1",
            evidence_version="1",
        )


def test_all_public_records_are_json_safe_and_exclude_sensitive_content(tmp_path: Path) -> None:
    profile = TaskProfile(
        task_id="task-1",
        category=TaskCategory.FEATURE,
        risk=RiskTier.MEDIUM,
        complexity=3,
        impact_radius=5,
        risk_flags=("authorization_adjacent",),
        context_requirements=("target", "dependencies"),
        verification_requirements=("tests",),
    )
    model = ModelProfile(
        model_id="kimi-k2.7-code:cloud",
        profile_version="1",
        capabilities=("code",),
        context_window_tokens=128_000,
        supported_efforts=(Effort.DEFAULT,),
        provisional=True,
    )
    decision = RoutingDecision(
        decision_id="decision-1",
        task_id="task-1",
        selected_model=model.model_id,
        effort=Effort.DEFAULT,
        score=750,
        confidence=0.5,
        alternatives=("glm-5:cloud/default",),
        reasons=("cold_start",),
        policy_version="1",
        evidence_version="1",
    )
    approval = ApprovalManifest(
        approval_id="approval-1",
        task_id="task-1",
        decision_id=decision.decision_id,
        graph_fingerprint="a" * 64,
        context_manifest_hash="b" * 64,
        inventory_digest="e" * 64,
        model_id=model.model_id,
        effort=Effort.DEFAULT,
        max_input_tokens=8_000,
        max_output_tokens=2_000,
        policy_version="1",
        issued_at=1_700_000_000,
        expires_at=1_700_000_300,
        nonce="nonce-1",
    )
    receipt = ExecutionReceipt(
        execution_id="execution-1",
        approval_id=approval.approval_id,
        model_id=model.model_id,
        effort=Effort.DEFAULT,
        outcome=ExecutionOutcome.SUCCEEDED,
        input_tokens=100,
        output_tokens=50,
        latency_ms=200,
        prompt_hash="c" * 64,
        response_hash="d" * 64,
        failure_reason=None,
    )
    outcome = VerifiedOutcome(
        outcome_id="outcome-1",
        execution_id=receipt.execution_id,
        provenance=EvidenceProvenance.MACHINE_VERIFIED,
        build_passed=True,
        tests_passed=True,
        security_passed=True,
        human_accepted=None,
        repair_count=0,
        escalated=False,
        reverted=False,
        severe_failure=False,
        recorded_at=1_700_000_400,
    )

    records = [_request(tmp_path), profile, model, decision, approval, receipt, outcome]
    serialized = json.dumps([record.to_dict() for record in records])

    assert str(tmp_path) not in serialized
    for forbidden in ("api_key", "password", "raw_prompt", "source_code", "response_body"):
        assert forbidden not in serialized.casefold()


def test_routing_settings_use_only_route_prefix_and_are_not_exported_as_secrets() -> None:
    settings = RoutingSettings.from_env(
        {
            "GRAPHITE_ROUTE_MAX_CONTEXT_BYTES": "131072",
            "GRAPHITE_ROUTE_AGGREGATE_OPT_IN": "true",
            "GRAPHITE_LLM_TIMEOUT": "9999",
        }
    )

    assert settings.max_context_bytes == 131_072
    assert settings.aggregate_opt_in is True
    assert settings.request_timeout_seconds != 9_999
    assert Config().routing_settings().to_dict()["aggregate_opt_in"] is False
    assert not any(key.startswith("routing") for key in Config().to_dict())


@pytest.mark.parametrize(
    ("name", "value", "code"),
    [
        ("GRAPHITE_ROUTE_MAX_CONTEXT_BYTES", "0", "max_context_bytes_invalid"),
        ("GRAPHITE_ROUTE_REQUEST_TIMEOUT_SECONDS", "nan", "request_timeout_seconds_invalid"),
        ("GRAPHITE_ROUTE_MAX_CONCURRENCY", "2", "max_concurrency_invalid"),
        ("GRAPHITE_ROUTE_SHADOW_RATE_PERCENT", "11", "shadow_rate_percent_invalid"),
        ("GRAPHITE_ROUTE_APPROVAL_TTL_SECONDS", "999999", "approval_ttl_seconds_invalid"),
        ("GRAPHITE_ROUTE_MAX_CHANGED_FILES", "0", "max_changed_files_invalid"),
        ("GRAPHITE_ROUTE_MAX_CHANGED_BYTES", "999999999", "max_changed_bytes_invalid"),
    ],
)
def test_routing_settings_reject_security_widening(name: str, value: str, code: str) -> None:
    with pytest.raises(RoutingSettingsError, match=code):
        RoutingSettings.from_env({name: value})
