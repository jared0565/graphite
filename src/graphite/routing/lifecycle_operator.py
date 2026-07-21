"""Read-only, bounded operator surface for provider lifecycle authority."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .contracts import Effort
from .lifecycle import ProviderCompatibilityPolicy, ProviderLifecycleState
from .lifecycle_service import LifecycleServiceError, build_verification_manifest
from .lifecycle_storage import (
    MAX_EVENT_PAGE_SIZE,
    CurrentLifecycleObservation,
    LifecycleStorageError,
    LifecycleStore,
)
from .storage import StorageError


class LifecycleOperatorError(RuntimeError):
    """A stable, sanitized lifecycle operator failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _page_limit(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_EVENT_PAGE_SIZE
    ):
        raise LifecycleOperatorError("lifecycle_limit_invalid")
    return value


def _canonical_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class LifecycleOperator:
    """Inspect lifecycle authority and prepare non-activating candidates."""

    def __init__(self, repository_root: Path | str) -> None:
        try:
            self.store = LifecycleStore(Path(repository_root))
        except (LifecycleStorageError, StorageError, ValueError, OSError) as exc:
            code = getattr(exc, "code", "lifecycle_operator_invalid")
            raise LifecycleOperatorError(code) from None

    @staticmethod
    def _public_observation(
        observation: CurrentLifecycleObservation,
    ) -> dict[str, object]:
        return {
            "boundary_digest": observation.boundary_digest,
            "provider": observation.provider.value,
            "runtime_kind": observation.runtime_kind.value,
            "state": observation.state.value,
            "lifecycle_identity_digest": (
                None if observation.identity is None else observation.identity.digest
            ),
            "policy_version": observation.policy_version,
            "observed_at": observation.observed_at,
            "updated_at": observation.updated_at,
        }

    def _current(self, boundary_digest: str) -> CurrentLifecycleObservation:
        try:
            current = self.store.read_current_observation(boundary_digest)
        except (LifecycleStorageError, ValueError) as exc:
            raise LifecycleOperatorError(
                getattr(exc, "code", "lifecycle_operator_invalid")
            ) from None
        if current is None:
            raise LifecycleOperatorError("lifecycle_observation_missing")
        return current

    def status(self, boundary_digest: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "storage_integrity": "ok",
            "observation": self._public_observation(self._current(boundary_digest)),
        }

    def list_observations(self, *, limit: int) -> dict[str, object]:
        bounded = _page_limit(limit)
        try:
            observations = self.store.read_observations(limit=bounded)
        except (LifecycleStorageError, ValueError) as exc:
            raise LifecycleOperatorError(
                getattr(exc, "code", "lifecycle_operator_invalid")
            ) from None
        public = [self._public_observation(observation) for observation in observations]
        return {"schema_version": 1, "count": len(public), "observations": public}

    def history(self, boundary_digest: str, *, limit: int) -> dict[str, object]:
        bounded = _page_limit(limit)
        try:
            events = self.store.read_events(boundary_digest, limit=bounded)
        except (LifecycleStorageError, ValueError) as exc:
            raise LifecycleOperatorError(
                getattr(exc, "code", "lifecycle_operator_invalid")
            ) from None
        public = [event.to_dict() for event in events]
        return {"schema_version": 1, "count": len(public), "events": public}

    def inspect_policy(self, boundary_digest: str) -> dict[str, object]:
        current = self._current(boundary_digest)
        return {
            "schema_version": 1,
            "boundary_digest": current.boundary_digest,
            "provider": current.provider.value,
            "runtime_kind": current.runtime_kind.value,
            "lifecycle_identity_digest": (
                None if current.identity is None else current.identity.digest
            ),
            "policy_version": current.policy_version,
            "policy_parameters_persisted": False,
            "change_rules": {
                "hash_or_patch": "standard_probe_then_verification_required",
                "minor": "expanded_probe_then_verification_required",
                "major": "incompatible_pending_policy_promotion",
            },
            "automatic_activation": False,
        }

    def prepare_policy_promotion(
        self,
        *,
        boundary_digest: str,
        lifecycle_identity_digest: str,
        proposed_policy_version: str,
        minimum_version: str,
        maximum_version_exclusive: str,
        required_capabilities: tuple[str, ...],
        prepared_at: int,
    ) -> dict[str, object]:
        current = self._current(boundary_digest)
        if current.identity is None or current.identity.digest != lifecycle_identity_digest:
            raise LifecycleOperatorError("lifecycle_identity_changed")
        if current.state is not ProviderLifecycleState.INCOMPATIBLE:
            raise LifecycleOperatorError("lifecycle_policy_promotion_not_required")
        if (
            isinstance(prepared_at, bool)
            or not isinstance(prepared_at, int)
            or not current.updated_at <= prepared_at <= 10**12
        ):
            raise LifecycleOperatorError("lifecycle_policy_candidate_invalid")
        try:
            proposed = ProviderCompatibilityPolicy(
                provider=current.provider,
                runtime_kind=current.runtime_kind,
                policy_version=proposed_policy_version,
                minimum_version=minimum_version,
                maximum_version_exclusive=maximum_version_exclusive,
                required_capabilities=required_capabilities,
            )
        except (TypeError, ValueError):
            raise LifecycleOperatorError("lifecycle_policy_candidate_invalid") from None
        candidate = {
            "boundary_digest": current.boundary_digest,
            "lifecycle_identity_digest": lifecycle_identity_digest,
            "current_state": current.state.value,
            "current_policy_version": current.policy_version,
            "proposed_policy": proposed.to_dict(),
            "prepared_at": prepared_at,
        }
        return {
            "schema_version": 1,
            **candidate,
            "candidate_digest": _canonical_digest(candidate),
            "promotion_requires_separate_human_authority": True,
            "automatic_activation": False,
        }

    def prepare_verification_manifest(
        self,
        *,
        boundary_digest: str,
        lifecycle_identity_digest: str,
        requested_model: str,
        expected_effective_model: str,
        effort: Effort,
        max_input_tokens: int,
        max_output_tokens: int,
        timeout_seconds: int,
        expires_at: int,
        fixture_repository_commit: str,
        graph_fingerprint: str,
        prompt_contract_hash: str,
        response_contract_hash: str,
        max_cost_microunits: int | None = None,
    ) -> dict[str, object]:
        current = self._current(boundary_digest)
        if current.identity is None or current.identity.digest != lifecycle_identity_digest:
            raise LifecycleOperatorError("lifecycle_identity_changed")
        try:
            manifest = build_verification_manifest(
                current,
                requested_model=requested_model,
                expected_effective_model=expected_effective_model,
                effort=effort,
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
                timeout_seconds=timeout_seconds,
                expires_at=expires_at,
                fixture_repository_commit=fixture_repository_commit,
                graph_fingerprint=graph_fingerprint,
                prompt_contract_hash=prompt_contract_hash,
                response_contract_hash=response_contract_hash,
                max_cost_microunits=max_cost_microunits,
            )
        except LifecycleServiceError as exc:
            raise LifecycleOperatorError(exc.code) from None
        return {
            "schema_version": 1,
            "manifest_digest": manifest.digest,
            "manifest": manifest.to_dict(),
            "execution_performed": False,
            "activation_performed": False,
        }
