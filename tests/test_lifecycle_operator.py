from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from graphite.routing.contracts import Effort
from graphite.routing.lifecycle import (
    LifecycleProviderId,
    ProviderCompatibilityPolicy,
    ProviderLifecycleState,
    ProviderRuntimeIdentity,
    RuntimeKind,
)
from graphite.routing.lifecycle_operator import LifecycleOperator, LifecycleOperatorError
from graphite.routing.lifecycle_service import ProviderLifecycleService
from graphite.routing.lifecycle_storage import LifecycleStore
from graphite.routing.storage import RepositoryStore


def _identity(*, version: str = "2.1.214", observed_at: int = 100) -> ProviderRuntimeIdentity:
    return ProviderRuntimeIdentity(
        provider=LifecycleProviderId.CLAUDE_CODE,
        runtime_kind=RuntimeKind.LOCAL_CLI,
        version=version,
        runtime_digest="a" * 64,
        model_identity_digest=None,
        routing_policy_digest=None,
        capabilities=("credential_health", "structured_output", "version"),
        policy_version="1.0.0",
        observed_at=observed_at,
    )


def _policy() -> ProviderCompatibilityPolicy:
    return ProviderCompatibilityPolicy(
        provider=LifecycleProviderId.CLAUDE_CODE,
        runtime_kind=RuntimeKind.LOCAL_CLI,
        policy_version="1.0.0",
        minimum_version="0.1.0",
        maximum_version_exclusive="3.0.0",
        required_capabilities=("credential_health", "structured_output"),
    )


def _observed_root(
    tmp_path: Path, *, version: str = "2.1.214"
) -> tuple[Path, str, ProviderRuntimeIdentity]:
    root = tmp_path / "project"
    root.mkdir()
    lifecycle = LifecycleStore(root)
    lifecycle.initialize()
    routing = RepositoryStore(root)
    routing.initialize()
    identity = _identity(version=version)
    boundary = "b" * 64
    ProviderLifecycleService(lifecycle, routing).observe(
        boundary_digest=boundary,
        identity=identity,
        policy=_policy(),
    )
    return root, boundary, identity


def test_missing_operator_storage_fails_without_creating_state(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with pytest.raises(LifecycleOperatorError, match="^lifecycle_storage_missing$"):
        LifecycleOperator(root).list_observations(limit=10)

    assert not (root / ".graphite").exists()


def test_status_list_and_history_are_bounded_sanitized_and_read_only(
    tmp_path: Path,
) -> None:
    root, boundary, identity = _observed_root(tmp_path)
    lifecycle = LifecycleStore(root)
    before = lifecycle.current_observation(boundary)
    operator = LifecycleOperator(root)

    status = operator.status(boundary)
    listing = operator.list_observations(limit=10)
    history = operator.history(boundary, limit=10)

    assert status["storage_integrity"] == "ok"
    assert status["observation"]["state"] == "verification_required"
    assert status["observation"]["lifecycle_identity_digest"] == identity.digest
    assert listing == {
        "schema_version": 1,
        "count": 1,
        "observations": [status["observation"]],
    }
    assert [event["current_state"] for event in history["events"]] == [
        "discovered",
        "verification_required",
    ]
    assert history["count"] == 2
    serialized = repr((status, listing, history)).casefold()
    for forbidden in (
        str(root).casefold(),
        "executable_path",
        "endpoint_query",
        "credential",
        "diagnostic",
    ):
        assert forbidden not in serialized
    assert lifecycle.current_observation(boundary) == before


def test_operator_rejects_logically_inconsistent_identity_binding(tmp_path: Path) -> None:
    root, boundary, _identity_value = _observed_root(tmp_path)
    store = LifecycleStore(root)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            "UPDATE current_observations SET identity_digest=? WHERE boundary_digest=?",
            ("f" * 64, boundary),
        )

    with pytest.raises(LifecycleOperatorError, match="^lifecycle_storage_corrupt$"):
        LifecycleOperator(root).status(boundary)


@pytest.mark.parametrize("limit", [0, 101, True])
def test_operator_rejects_unbounded_pages(tmp_path: Path, limit: object) -> None:
    root, boundary, _identity_value = _observed_root(tmp_path)
    operator = LifecycleOperator(root)

    with pytest.raises(LifecycleOperatorError, match="^lifecycle_limit_invalid$"):
        operator.list_observations(limit=limit)  # type: ignore[arg-type]
    with pytest.raises(LifecycleOperatorError, match="^lifecycle_limit_invalid$"):
        operator.history(boundary, limit=limit)  # type: ignore[arg-type]


def test_policy_inspection_reports_binding_and_nonautomatic_transition_rules(
    tmp_path: Path,
) -> None:
    root, boundary, identity = _observed_root(tmp_path)

    payload = LifecycleOperator(root).inspect_policy(boundary)

    assert payload["provider"] == "claude-code"
    assert payload["runtime_kind"] == "local-cli"
    assert payload["lifecycle_identity_digest"] == identity.digest
    assert payload["policy_version"] == "1.0.0"
    assert payload["policy_parameters_persisted"] is False
    assert payload["change_rules"] == {
        "hash_or_patch": "standard_probe_then_verification_required",
        "minor": "expanded_probe_then_verification_required",
        "major": "incompatible_pending_policy_promotion",
    }
    assert payload["automatic_activation"] is False


def test_policy_promotion_preparation_is_hash_only_and_does_not_mutate_state(
    tmp_path: Path,
) -> None:
    root, boundary, identity = _observed_root(tmp_path, version="3.0.0")
    lifecycle = LifecycleStore(root)
    before = lifecycle.current_observation(boundary)

    candidate = LifecycleOperator(root).prepare_policy_promotion(
        boundary_digest=boundary,
        lifecycle_identity_digest=identity.digest,
        proposed_policy_version="2.0.0",
        minimum_version="0.1.0",
        maximum_version_exclusive="4.0.0",
        required_capabilities=("credential_health", "structured_output"),
        prepared_at=200,
    )

    assert before is not None and before.state is ProviderLifecycleState.INCOMPATIBLE
    assert candidate["candidate_digest"]
    assert candidate["current_state"] == "incompatible"
    assert candidate["promotion_requires_separate_human_authority"] is True
    assert candidate["automatic_activation"] is False
    assert candidate["proposed_policy"]["maximum_version_exclusive"] == "4.0.0"
    assert lifecycle.current_observation(boundary) == before


def test_policy_preparation_rejects_identity_drift(tmp_path: Path) -> None:
    root, boundary, _identity_value = _observed_root(tmp_path, version="3.0.0")

    with pytest.raises(LifecycleOperatorError, match="^lifecycle_identity_changed$"):
        LifecycleOperator(root).prepare_policy_promotion(
            boundary_digest=boundary,
            lifecycle_identity_digest="f" * 64,
            proposed_policy_version="2.0.0",
            minimum_version="0.1.0",
            maximum_version_exclusive="4.0.0",
            required_capabilities=("credential_health", "structured_output"),
            prepared_at=200,
        )


def test_verification_manifest_preparation_requires_exact_verification_identity(
    tmp_path: Path,
) -> None:
    root, boundary, identity = _observed_root(tmp_path)
    lifecycle = LifecycleStore(root)
    before = lifecycle.current_observation(boundary)

    payload = LifecycleOperator(root).prepare_verification_manifest(
        boundary_digest=boundary,
        lifecycle_identity_digest=identity.digest,
        requested_model="sonnet",
        expected_effective_model="claude-sonnet-5",
        effort=Effort.HIGH,
        max_input_tokens=32_768,
        max_output_tokens=4_096,
        timeout_seconds=120,
        expires_at=500,
        fixture_repository_commit="1" * 40,
        graph_fingerprint="2" * 64,
        prompt_contract_hash="3" * 64,
        response_contract_hash="4" * 64,
        max_cost_microunits=None,
    )

    assert payload["manifest"]["lifecycle_identity_digest"] == identity.digest
    assert payload["manifest"]["max_attempts"] == 1
    assert payload["manifest"]["fallback_enabled"] is False
    assert payload["manifest"]["no_resume"] is True
    assert payload["manifest"]["no_substitution"] is True
    assert payload["manifest_digest"]
    assert payload["execution_performed"] is False
    assert payload["activation_performed"] is False
    assert lifecycle.current_observation(boundary) == before


def test_verification_manifest_rejects_nonmatching_identity(tmp_path: Path) -> None:
    root, boundary, _identity_value = _observed_root(tmp_path)

    with pytest.raises(LifecycleOperatorError, match="^lifecycle_identity_changed$"):
        LifecycleOperator(root).prepare_verification_manifest(
            boundary_digest=boundary,
            lifecycle_identity_digest="f" * 64,
            requested_model="sonnet",
            expected_effective_model="claude-sonnet-5",
            effort=Effort.HIGH,
            max_input_tokens=32_768,
            max_output_tokens=4_096,
            timeout_seconds=120,
            expires_at=500,
            fixture_repository_commit="1" * 40,
            graph_fingerprint="2" * 64,
            prompt_contract_hash="3" * 64,
            response_contract_hash="4" * 64,
            max_cost_microunits=None,
        )
