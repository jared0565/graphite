"""Provider-neutral lifecycle authority contract tests."""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from graphite.routing.contracts import ProviderId
from graphite.routing.lifecycle import (
    CompatibilityProbeLevel,
    IdentityChange,
    LifecycleProviderId,
    LifecycleReasonCode,
    ProviderCompatibilityPolicy,
    ProviderLifecycleEvent,
    ProviderLifecycleState,
    ProviderRuntimeIdentity,
    RuntimeKind,
    _PROVIDER_RUNTIME_KINDS,
    assess_identity_change,
    classify_identity_change,
)


def _identity(**changes: object) -> ProviderRuntimeIdentity:
    values: dict[str, object] = {
        "provider": LifecycleProviderId.CLAUDE_CODE,
        "runtime_kind": RuntimeKind.LOCAL_CLI,
        "version": "2.1.215",
        "runtime_digest": "a" * 64,
        "model_identity_digest": None,
        "routing_policy_digest": None,
        "capabilities": ("auth-status-json", "structured-output"),
        "policy_version": "1.0.0",
        "observed_at": 1_721_347_200,
    }
    values.update(changes)
    return ProviderRuntimeIdentity(**values)


def _policy(**changes: object) -> ProviderCompatibilityPolicy:
    values: dict[str, object] = {
        "provider": LifecycleProviderId.CLAUDE_CODE,
        "runtime_kind": RuntimeKind.LOCAL_CLI,
        "policy_version": "1.0.0",
        "minimum_version": "2.1.0",
        "maximum_version_exclusive": "3.0.0",
        "required_capabilities": ("auth-status-json", "structured-output"),
    }
    values.update(changes)
    return ProviderCompatibilityPolicy(**values)


def test_provider_ids_and_runtime_kinds_cover_all_approved_adapters() -> None:
    assert tuple(item.value for item in LifecycleProviderId) == (
        "claude-code",
        "codex",
        "ollama",
        "openrouter",
        "zai",
    )
    assert tuple(item.value for item in ProviderId) == (
        "claude-code",
        "codex",
        "openrouter",
        "zai",
    )
    assert tuple(item.value for item in RuntimeKind) == (
        "local-cli",
        "local-http",
        "remote-https",
    )


def test_runtime_identity_is_frozen_canonical_and_excludes_observation_time_from_digest() -> None:
    identity = _identity()
    reordered = _identity(
        capabilities=("structured-output", "auth-status-json"),
        observed_at=identity.observed_at + 60,
    )

    assert identity.digest == reordered.digest
    assert len(identity.digest) == 64
    assert identity.capabilities == ("auth-status-json", "structured-output")
    assert list(identity.to_dict()) == list(identity._public_fields)
    assert json.loads(json.dumps(identity.to_dict()))["provider"] == "claude-code"
    with pytest.raises(FrozenInstanceError):
        identity.version = "2.2.0"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"runtime_kind": RuntimeKind.REMOTE_HTTPS}, "provider_runtime_kind_invalid"),
        ({"runtime_digest": "A" * 64}, "runtime_digest_invalid"),
        ({"version": "latest"}, "runtime_version_invalid"),
        ({"capabilities": ("structured-output", "structured-output")}, "capabilities_invalid"),
        ({"observed_at": True}, "observed_at_invalid"),
        ({"policy_version": "current"}, "policy_version_invalid"),
    ],
)
def test_runtime_identity_rejects_ambiguous_or_malformed_values(
    changes: dict[str, object], code: str
) -> None:
    with pytest.raises(ValueError, match=code):
        _identity(**changes)


def test_http_provider_identities_require_model_and_routing_bindings() -> None:
    ollama = _identity(
        provider=LifecycleProviderId.OLLAMA,
        runtime_kind=RuntimeKind.LOCAL_HTTP,
        version="0.9.1",
        model_identity_digest="b" * 64,
        capabilities=("model-manifest", "version"),
    )
    openrouter = _identity(
        provider=LifecycleProviderId.OPENROUTER,
        runtime_kind=RuntimeKind.REMOTE_HTTPS,
        version="1.0.0",
        runtime_digest="c" * 64,
        model_identity_digest="d" * 64,
        routing_policy_digest="e" * 64,
        capabilities=("contract-metadata",),
    )

    assert ollama.model_identity_digest == "b" * 64
    assert openrouter.routing_policy_digest == "e" * 64
    with pytest.raises(ValueError, match="model_identity_digest_invalid"):
        _identity(provider=LifecycleProviderId.OLLAMA, runtime_kind=RuntimeKind.LOCAL_HTTP)
    with pytest.raises(ValueError, match="routing_policy_digest_invalid"):
        _identity(
            provider=LifecycleProviderId.OPENROUTER,
            runtime_kind=RuntimeKind.REMOTE_HTTPS,
            model_identity_digest="d" * 64,
        )


def test_compatibility_policy_is_canonical_and_bounded() -> None:
    policy = _policy(required_capabilities=("structured-output", "auth-status-json"))

    assert policy.required_capabilities == ("auth-status-json", "structured-output")
    assert len(policy.digest) == 64
    assert policy.supports(_identity()) is True
    assert policy.supports(_identity(version="3.0.0")) is False
    assert policy.supports(_identity(version="2.1.0-beta.1")) is False
    assert policy.supports(_identity(capabilities=("structured-output",))) is False
    assert policy.supports(_identity(policy_version="1.0.1")) is False
    with pytest.raises(ValueError, match="compatibility_version_range_invalid"):
        _policy(minimum_version="3.0.0", maximum_version_exclusive="3.0.0")
    with pytest.raises(ValueError, match="compatibility_boundary_invalid"):
        _policy(provider=LifecycleProviderId.OLLAMA)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, IdentityChange.UNCHANGED),
        ({"runtime_digest": "b" * 64}, IdentityChange.HASH_ONLY),
        ({"version": "2.1.216", "runtime_digest": "b" * 64}, IdentityChange.PATCH),
        ({"version": "2.2.0", "runtime_digest": "b" * 64}, IdentityChange.MINOR),
        ({"version": "3.0.0", "runtime_digest": "b" * 64}, IdentityChange.MAJOR),
        ({"capabilities": ("auth-status-json",)}, IdentityChange.CAPABILITY),
    ],
)
def test_cli_identity_change_classification_is_deterministic(
    changes: dict[str, object], expected: IdentityChange
) -> None:
    previous = _identity()
    current = replace(previous, **changes)

    assert classify_identity_change(previous, current) is expected


@pytest.mark.parametrize(
    ("provider", "runtime_kind", "field", "expected"),
    [
        (LifecycleProviderId.OLLAMA, RuntimeKind.LOCAL_HTTP, "runtime_digest", IdentityChange.ENDPOINT),
        (LifecycleProviderId.OLLAMA, RuntimeKind.LOCAL_HTTP, "model_identity_digest", IdentityChange.MODEL_DIGEST),
        (LifecycleProviderId.OPENROUTER, RuntimeKind.REMOTE_HTTPS, "routing_policy_digest", IdentityChange.ROUTING_POLICY),
    ],
)
def test_http_identity_change_classification_distinguishes_authority_boundaries(
    provider: LifecycleProviderId,
    runtime_kind: RuntimeKind,
    field: str,
    expected: IdentityChange,
) -> None:
    base = {
        "provider": provider,
        "runtime_kind": runtime_kind,
        "version": "1.0.0",
        "runtime_digest": "a" * 64,
        "model_identity_digest": "b" * 64,
        "routing_policy_digest": "c" * 64 if provider is LifecycleProviderId.OPENROUTER else None,
        "capabilities": ("contract-metadata",),
    }
    previous = _identity(**base)
    current = replace(previous, **{field: "d" * 64})

    assert classify_identity_change(previous, current) is expected


def test_change_classification_rejects_cross_boundary_comparison() -> None:
    with pytest.raises(ValueError, match="identity_boundary_invalid"):
        classify_identity_change(
            _identity(), _identity(provider=LifecycleProviderId.CODEX)
        )


def test_change_classification_uses_highest_risk_for_simultaneous_drift() -> None:
    cli_change = classify_identity_change(
        _identity(),
        _identity(
            version="2.1.216",
            runtime_digest="b" * 64,
            capabilities=("auth-status-json", "structured-output", "tool-contract"),
        ),
    )
    ollama = _identity(
        provider=LifecycleProviderId.OLLAMA,
        runtime_kind=RuntimeKind.LOCAL_HTTP,
        version="1.0.0",
        model_identity_digest="b" * 64,
        capabilities=("contract-metadata",),
    )
    http_change = classify_identity_change(
        ollama,
        replace(ollama, version="1.0.1", runtime_digest="c" * 64),
    )

    assert cli_change is IdentityChange.CAPABILITY
    assert http_change is IdentityChange.ENDPOINT


@pytest.mark.parametrize(
    ("current", "kwargs", "state", "reason", "probe"),
    [
        (_identity(runtime_digest="b" * 64), {}, ProviderLifecycleState.VERIFICATION_REQUIRED, LifecycleReasonCode.HASH_CHANGED, CompatibilityProbeLevel.STANDARD),
        (_identity(version="2.2.0", runtime_digest="b" * 64), {}, ProviderLifecycleState.VERIFICATION_REQUIRED, LifecycleReasonCode.MINOR_CHANGED, CompatibilityProbeLevel.EXPANDED),
        (_identity(version="3.0.0", runtime_digest="b" * 64), {}, ProviderLifecycleState.INCOMPATIBLE, LifecycleReasonCode.MAJOR_CHANGED, CompatibilityProbeLevel.NONE),
        (_identity(), {"runtime_available": False}, ProviderLifecycleState.UNAVAILABLE, LifecycleReasonCode.RUNTIME_MISSING, CompatibilityProbeLevel.NONE),
        (_identity(), {"credential_healthy": False}, ProviderLifecycleState.UNAVAILABLE, LifecycleReasonCode.CREDENTIAL_UNHEALTHY, CompatibilityProbeLevel.NONE),
        (_identity(capabilities=("structured-output",)), {}, ProviderLifecycleState.INCOMPATIBLE, LifecycleReasonCode.REQUIRED_CAPABILITY_MISSING, CompatibilityProbeLevel.EXPANDED),
    ],
)
def test_policy_assessment_never_activates_changed_identity(
    current: ProviderRuntimeIdentity,
    kwargs: dict[str, object],
    state: ProviderLifecycleState,
    reason: LifecycleReasonCode,
    probe: CompatibilityProbeLevel,
) -> None:
    assessment = assess_identity_change(_identity(), current, _policy(), **kwargs)

    assert assessment.state is state
    assert assessment.reason is reason
    assert assessment.probe_level is probe
    assert assessment.state is not ProviderLifecycleState.ACTIVE


def test_expanded_probe_failure_is_incompatible_and_sanitized() -> None:
    assessment = assess_identity_change(
        _identity(),
        _identity(version="2.2.0", runtime_digest="b" * 64),
        _policy(),
        expanded_probe_passed=False,
    )

    assert assessment.state is ProviderLifecycleState.INCOMPATIBLE
    assert assessment.reason is LifecycleReasonCode.PROBE_FAILED
    assert "diagnostic" not in json.dumps(assessment.to_dict()).casefold()


def test_unchanged_active_identity_retains_state_without_granting_new_authority() -> None:
    assessment = assess_identity_change(
        _identity(),
        _identity(observed_at=1_721_347_260),
        _policy(),
        existing_state=ProviderLifecycleState.ACTIVE,
    )
    changed = assess_identity_change(
        _identity(),
        _identity(runtime_digest="b" * 64),
        _policy(),
        existing_state=ProviderLifecycleState.ACTIVE,
    )

    assert assessment.state is ProviderLifecycleState.ACTIVE
    assert assessment.reason is LifecycleReasonCode.IDENTITY_UNCHANGED
    assert changed.state is ProviderLifecycleState.VERIFICATION_REQUIRED


def test_lifecycle_event_allows_only_explicit_safe_transitions() -> None:
    event = ProviderLifecycleEvent(
        event_id="event-1",
        provider=LifecycleProviderId.CLAUDE_CODE,
        runtime_kind=RuntimeKind.LOCAL_CLI,
        previous_identity_digest="a" * 64,
        current_identity_digest="b" * 64,
        previous_state=ProviderLifecycleState.ACTIVE,
        current_state=ProviderLifecycleState.VERIFICATION_REQUIRED,
        reason=LifecycleReasonCode.PATCH_CHANGED,
        policy_version="1.0.0",
        occurred_at=1_721_347_260,
    )

    assert event.to_dict()["current_state"] == "verification_required"
    with pytest.raises(FrozenInstanceError):
        event.reason = LifecycleReasonCode.IDENTITY_UNCHANGED
    with pytest.raises(ValueError, match="lifecycle_transition_invalid"):
        replace(
            event,
            previous_state=ProviderLifecycleState.DISCOVERED,
            current_state=ProviderLifecycleState.ACTIVE,
            reason=LifecycleReasonCode.VERIFICATION_ACCEPTED,
        )
    with pytest.raises(ValueError, match="lifecycle_transition_invalid"):
        replace(
            event,
            previous_state=ProviderLifecycleState.VERIFICATION_REQUIRED,
            current_state=ProviderLifecycleState.ACTIVE,
            reason=LifecycleReasonCode.PATCH_CHANGED,
        )
    with pytest.raises(ValueError, match="lifecycle_transition_invalid"):
        replace(
            event,
            previous_state=None,
            previous_identity_digest=None,
            current_state=ProviderLifecycleState.DISCOVERED,
            reason=LifecycleReasonCode.PATCH_CHANGED,
        )
    with pytest.raises(ValueError, match="lifecycle_transition_invalid"):
        replace(
            event,
            current_state=ProviderLifecycleState.UNAVAILABLE,
            reason=LifecycleReasonCode.PATCH_CHANGED,
        )


def test_lifecycle_transition_matrix_accepts_every_documented_transition() -> None:
    transitions = (
        (None, ProviderLifecycleState.DISCOVERED, LifecycleReasonCode.DISCOVERED),
        (None, ProviderLifecycleState.INCOMPATIBLE, LifecycleReasonCode.PROBE_FAILED),
        (None, ProviderLifecycleState.UNAVAILABLE, LifecycleReasonCode.RUNTIME_MISSING),
        (ProviderLifecycleState.DISCOVERED, ProviderLifecycleState.COMPATIBLE, LifecycleReasonCode.COMPATIBILITY_CONFIRMED),
        (ProviderLifecycleState.DISCOVERED, ProviderLifecycleState.VERIFICATION_REQUIRED, LifecycleReasonCode.PATCH_CHANGED),
        (ProviderLifecycleState.DISCOVERED, ProviderLifecycleState.INCOMPATIBLE, LifecycleReasonCode.PROBE_FAILED),
        (ProviderLifecycleState.DISCOVERED, ProviderLifecycleState.UNAVAILABLE, LifecycleReasonCode.RUNTIME_MISSING),
        (ProviderLifecycleState.COMPATIBLE, ProviderLifecycleState.ACTIVE, LifecycleReasonCode.VERIFICATION_ACCEPTED),
        (ProviderLifecycleState.COMPATIBLE, ProviderLifecycleState.VERIFICATION_REQUIRED, LifecycleReasonCode.PATCH_CHANGED),
        (ProviderLifecycleState.COMPATIBLE, ProviderLifecycleState.INCOMPATIBLE, LifecycleReasonCode.PROBE_FAILED),
        (ProviderLifecycleState.COMPATIBLE, ProviderLifecycleState.UNAVAILABLE, LifecycleReasonCode.RUNTIME_MISSING),
        (ProviderLifecycleState.VERIFICATION_REQUIRED, ProviderLifecycleState.ACTIVE, LifecycleReasonCode.VERIFICATION_ACCEPTED),
        (ProviderLifecycleState.VERIFICATION_REQUIRED, ProviderLifecycleState.INCOMPATIBLE, LifecycleReasonCode.PROBE_FAILED),
        (ProviderLifecycleState.VERIFICATION_REQUIRED, ProviderLifecycleState.UNAVAILABLE, LifecycleReasonCode.RUNTIME_MISSING),
        (ProviderLifecycleState.ACTIVE, ProviderLifecycleState.VERIFICATION_REQUIRED, LifecycleReasonCode.PATCH_CHANGED),
        (ProviderLifecycleState.ACTIVE, ProviderLifecycleState.INCOMPATIBLE, LifecycleReasonCode.MAJOR_CHANGED),
        (ProviderLifecycleState.ACTIVE, ProviderLifecycleState.UNAVAILABLE, LifecycleReasonCode.RUNTIME_MISSING),
        (ProviderLifecycleState.INCOMPATIBLE, ProviderLifecycleState.VERIFICATION_REQUIRED, LifecycleReasonCode.POLICY_PROMOTED),
        (ProviderLifecycleState.INCOMPATIBLE, ProviderLifecycleState.UNAVAILABLE, LifecycleReasonCode.RUNTIME_MISSING),
        (ProviderLifecycleState.UNAVAILABLE, ProviderLifecycleState.DISCOVERED, LifecycleReasonCode.DISCOVERED),
        (ProviderLifecycleState.UNAVAILABLE, ProviderLifecycleState.INCOMPATIBLE, LifecycleReasonCode.PROBE_FAILED),
    )
    for index, (previous_state, current_state, reason) in enumerate(transitions):
        event = ProviderLifecycleEvent(
            event_id=f"event-{index}",
            provider=LifecycleProviderId.CLAUDE_CODE,
            runtime_kind=RuntimeKind.LOCAL_CLI,
            previous_identity_digest=None if previous_state is None else "a" * 64,
            current_identity_digest="b" * 64,
            previous_state=previous_state,
            current_state=current_state,
            reason=reason,
            policy_version="1.0.0",
            occurred_at=1_721_347_260 + index,
        )

        assert event.current_state is current_state

    for index, state in enumerate(ProviderLifecycleState, start=len(transitions)):
        event = ProviderLifecycleEvent(
            event_id=f"event-{index}",
            provider=LifecycleProviderId.CLAUDE_CODE,
            runtime_kind=RuntimeKind.LOCAL_CLI,
            previous_identity_digest="a" * 64,
            current_identity_digest="a" * 64,
            previous_state=state,
            current_state=state,
            reason=LifecycleReasonCode.IDENTITY_UNCHANGED,
            policy_version="1.0.0",
            occurred_at=1_721_347_260 + index,
        )

        assert event.previous_state is event.current_state


def test_lifecycle_transition_matrix_rejects_every_undocumented_transition() -> None:
    valid_pairs = {
        (None, ProviderLifecycleState.DISCOVERED),
        (None, ProviderLifecycleState.INCOMPATIBLE),
        (None, ProviderLifecycleState.UNAVAILABLE),
        (ProviderLifecycleState.DISCOVERED, ProviderLifecycleState.COMPATIBLE),
        (ProviderLifecycleState.DISCOVERED, ProviderLifecycleState.VERIFICATION_REQUIRED),
        (ProviderLifecycleState.DISCOVERED, ProviderLifecycleState.INCOMPATIBLE),
        (ProviderLifecycleState.DISCOVERED, ProviderLifecycleState.UNAVAILABLE),
        (ProviderLifecycleState.COMPATIBLE, ProviderLifecycleState.ACTIVE),
        (ProviderLifecycleState.COMPATIBLE, ProviderLifecycleState.VERIFICATION_REQUIRED),
        (ProviderLifecycleState.COMPATIBLE, ProviderLifecycleState.INCOMPATIBLE),
        (ProviderLifecycleState.COMPATIBLE, ProviderLifecycleState.UNAVAILABLE),
        (ProviderLifecycleState.VERIFICATION_REQUIRED, ProviderLifecycleState.ACTIVE),
        (ProviderLifecycleState.VERIFICATION_REQUIRED, ProviderLifecycleState.INCOMPATIBLE),
        (ProviderLifecycleState.VERIFICATION_REQUIRED, ProviderLifecycleState.UNAVAILABLE),
        (ProviderLifecycleState.ACTIVE, ProviderLifecycleState.VERIFICATION_REQUIRED),
        (ProviderLifecycleState.ACTIVE, ProviderLifecycleState.INCOMPATIBLE),
        (ProviderLifecycleState.ACTIVE, ProviderLifecycleState.UNAVAILABLE),
        (ProviderLifecycleState.INCOMPATIBLE, ProviderLifecycleState.VERIFICATION_REQUIRED),
        (ProviderLifecycleState.INCOMPATIBLE, ProviderLifecycleState.UNAVAILABLE),
        (ProviderLifecycleState.UNAVAILABLE, ProviderLifecycleState.DISCOVERED),
        (ProviderLifecycleState.UNAVAILABLE, ProviderLifecycleState.INCOMPATIBLE),
    }
    states: tuple[ProviderLifecycleState | None, ...] = (None, *ProviderLifecycleState)
    for previous_state in states:
        for current_state in ProviderLifecycleState:
            if previous_state is current_state or (previous_state, current_state) in valid_pairs:
                continue
            with pytest.raises(ValueError, match="lifecycle_transition_invalid"):
                ProviderLifecycleEvent(
                    event_id="event-invalid",
                    provider=LifecycleProviderId.CLAUDE_CODE,
                    runtime_kind=RuntimeKind.LOCAL_CLI,
                    previous_identity_digest=None if previous_state is None else "a" * 64,
                    current_identity_digest="b" * 64,
                    previous_state=previous_state,
                    current_state=current_state,
                    reason=LifecycleReasonCode.PATCH_CHANGED,
                    policy_version="1.0.0",
                    occurred_at=1_721_347_260,
                )


def test_lifecycle_public_records_exclude_sensitive_fields() -> None:
    records = [
        _identity(),
        _policy(),
        assess_identity_change(
            _identity(), _identity(runtime_digest="b" * 64), _policy()
        ),
    ]
    serialized = json.dumps([record.to_dict() for record in records]).casefold()

    for forbidden in (
        "api_key",
        "authorization",
        "credential_value",
        "executable_path",
        "prompt",
        "raw_stdout",
        "raw_stderr",
        "response_body",
        "source_code",
    ):
        assert forbidden not in serialized


def test_zai_is_remote_https_and_requires_model_digest_and_forbids_routing() -> None:
    assert LifecycleProviderId.ZAI.value == "zai"
    assert _PROVIDER_RUNTIME_KINDS[LifecycleProviderId.ZAI] is RuntimeKind.REMOTE_HTTPS
    # routing_policy_digest MUST be None for zai (forbidden for non-OPENROUTER)
    identity = ProviderRuntimeIdentity(
        LifecycleProviderId.ZAI, RuntimeKind.REMOTE_HTTPS, "1.0.0",
        "a" * 64, "b" * 64, None, ("remote_inference",), "1.0.0", 1_700_000_000,
    )
    assert identity.provider is LifecycleProviderId.ZAI
    # passing a routing digest for zai must fail
    with pytest.raises(ValueError, match="routing_policy_digest_invalid"):
        ProviderRuntimeIdentity(
            LifecycleProviderId.ZAI, RuntimeKind.REMOTE_HTTPS, "1.0.0",
            "a" * 64, "b" * 64, "c" * 64, ("remote_inference",), "1.0.0", 1_700_000_000,
        )
    # zai now REQUIRES a model_identity_digest (parity with OLLAMA/OPENROUTER): None must fail
    with pytest.raises(ValueError, match="model_identity_digest_invalid"):
        ProviderRuntimeIdentity(
            LifecycleProviderId.ZAI, RuntimeKind.REMOTE_HTTPS, "1.0.0",
            "a" * 64, None, None, ("remote_inference",), "1.0.0", 1_700_000_000,
        )


def test_patch_on_active_local_cli_carries_authority_forward() -> None:
    previous = _identity()
    current = _identity(
        version="2.1.216", runtime_digest="b" * 64, observed_at=1_721_433_600
    )

    assessment = assess_identity_change(
        previous, current, _policy(), existing_state=ProviderLifecycleState.ACTIVE
    )

    assert assessment.change is IdentityChange.PATCH
    assert assessment.state is ProviderLifecycleState.ACTIVE
    assert assessment.reason is LifecycleReasonCode.PATCH_CARRIED_FORWARD
    assert assessment.probe_level is CompatibilityProbeLevel.STANDARD


def test_hash_only_on_active_boundary_still_requires_verification() -> None:
    previous = _identity()
    current = _identity(runtime_digest="b" * 64, observed_at=1_721_433_600)

    assessment = assess_identity_change(
        previous, current, _policy(), existing_state=ProviderLifecycleState.ACTIVE
    )

    assert assessment.change is IdentityChange.HASH_ONLY
    assert assessment.state is ProviderLifecycleState.VERIFICATION_REQUIRED
    assert assessment.reason is LifecycleReasonCode.HASH_CHANGED


def test_patch_on_active_non_local_cli_still_requires_verification() -> None:
    previous = _identity(
        provider=LifecycleProviderId.OLLAMA,
        runtime_kind=RuntimeKind.LOCAL_HTTP,
        version="0.5.1",
        model_identity_digest="a" * 64,
        capabilities=("model-manifest", "version"),
    )
    current = _identity(
        provider=LifecycleProviderId.OLLAMA,
        runtime_kind=RuntimeKind.LOCAL_HTTP,
        version="0.5.2",
        model_identity_digest="a" * 64,
        observed_at=1_721_433_600,
        capabilities=("model-manifest", "version"),
    )
    # runtime_digest deliberately unchanged: for non-CLI runtimes a digest change
    # classifies as ENDPOINT (checked before PATCH), which would dodge the gate
    # this test exists to pin.
    policy = _policy(
        provider=LifecycleProviderId.OLLAMA,
        runtime_kind=RuntimeKind.LOCAL_HTTP,
        minimum_version="0.1.0",
        maximum_version_exclusive="1.0.0",
        required_capabilities=("model-manifest", "version"),
    )

    assessment = assess_identity_change(
        previous, current, policy, existing_state=ProviderLifecycleState.ACTIVE
    )

    assert assessment.change is IdentityChange.PATCH
    assert assessment.state is ProviderLifecycleState.VERIFICATION_REQUIRED
    assert assessment.reason is LifecycleReasonCode.PATCH_CHANGED


def test_patch_from_verification_required_does_not_resurrect_authority() -> None:
    previous = _identity()
    current = _identity(
        version="2.1.216", runtime_digest="b" * 64, observed_at=1_721_433_600
    )

    assessment = assess_identity_change(
        previous,
        current,
        _policy(),
        existing_state=ProviderLifecycleState.VERIFICATION_REQUIRED,
    )

    assert assessment.state is ProviderLifecycleState.VERIFICATION_REQUIRED
    assert assessment.reason is LifecycleReasonCode.PATCH_CHANGED


def test_patch_with_failed_standard_probe_is_incompatible_not_carried() -> None:
    previous = _identity()
    current = _identity(
        version="2.1.216", runtime_digest="b" * 64, observed_at=1_721_433_600
    )

    assessment = assess_identity_change(
        previous,
        current,
        _policy(),
        standard_probe_passed=False,
        existing_state=ProviderLifecycleState.ACTIVE,
    )

    assert assessment.state is ProviderLifecycleState.INCOMPATIBLE
    assert assessment.reason is LifecycleReasonCode.PROBE_FAILED


def test_patch_outside_policy_range_is_incompatible_not_carried() -> None:
    previous = _identity()
    current = _identity(
        version="2.1.216", runtime_digest="b" * 64, observed_at=1_721_433_600
    )
    policy = _policy(maximum_version_exclusive="2.1.216")

    assessment = assess_identity_change(
        previous, current, policy, existing_state=ProviderLifecycleState.ACTIVE
    )

    assert assessment.state is ProviderLifecycleState.INCOMPATIBLE
    assert assessment.reason is LifecycleReasonCode.POLICY_RANGE_UNSUPPORTED


def test_event_active_to_active_identity_change_requires_carry_reason() -> None:
    ProviderLifecycleEvent(
        "carry-event-1",
        LifecycleProviderId.CLAUDE_CODE,
        RuntimeKind.LOCAL_CLI,
        "a" * 64,
        "b" * 64,
        ProviderLifecycleState.ACTIVE,
        ProviderLifecycleState.ACTIVE,
        LifecycleReasonCode.PATCH_CARRIED_FORWARD,
        "1.0.0",
        1_721_433_600,
    )
    with pytest.raises(ValueError, match="lifecycle_transition_invalid"):
        ProviderLifecycleEvent(
            "carry-event-2",
            LifecycleProviderId.CLAUDE_CODE,
            RuntimeKind.LOCAL_CLI,
            "a" * 64,
            "b" * 64,
            ProviderLifecycleState.ACTIVE,
            ProviderLifecycleState.ACTIVE,
            LifecycleReasonCode.PATCH_CHANGED,
            "1.0.0",
            1_721_433_600,
        )


def test_event_carry_reason_is_rejected_outside_active_to_active_change() -> None:
    with pytest.raises(ValueError, match="lifecycle_transition_invalid"):
        ProviderLifecycleEvent(
            "carry-event-3",
            LifecycleProviderId.CLAUDE_CODE,
            RuntimeKind.LOCAL_CLI,
            "a" * 64,
            "b" * 64,
            ProviderLifecycleState.VERIFICATION_REQUIRED,
            ProviderLifecycleState.VERIFICATION_REQUIRED,
            LifecycleReasonCode.PATCH_CARRIED_FORWARD,
            "1.0.0",
            1_721_433_600,
        )
    with pytest.raises(ValueError, match="lifecycle_transition_invalid"):
        ProviderLifecycleEvent(
            "carry-event-4",
            LifecycleProviderId.CLAUDE_CODE,
            RuntimeKind.LOCAL_CLI,
            "a" * 64,
            "a" * 64,
            ProviderLifecycleState.ACTIVE,
            ProviderLifecycleState.ACTIVE,
            LifecycleReasonCode.PATCH_CARRIED_FORWARD,
            "1.0.0",
            1_721_433_600,
        )
