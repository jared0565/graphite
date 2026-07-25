"""Provider-neutral runtime lifecycle authority contracts.

This module is intentionally pure.  It normalizes identity, compatibility, and
transition decisions without discovering executables, contacting endpoints, or
granting provider execution authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .contracts import PublicRecord

MAX_CAPABILITIES = 32
MAX_IDENTIFIER_LENGTH = 128

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SEMANTIC_VERSION = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$"
)


class RuntimeKind(StrEnum):
    LOCAL_CLI = "local-cli"
    LOCAL_HTTP = "local-http"
    REMOTE_HTTPS = "remote-https"


class LifecycleProviderId(StrEnum):
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    OLLAMA = "ollama"
    OPENROUTER = "openrouter"
    ZAI = "zai"


class ProviderLifecycleState(StrEnum):
    DISCOVERED = "discovered"
    COMPATIBLE = "compatible"
    VERIFICATION_REQUIRED = "verification_required"
    ACTIVE = "active"
    INCOMPATIBLE = "incompatible"
    UNAVAILABLE = "unavailable"


class IdentityChange(StrEnum):
    UNCHANGED = "unchanged"
    HASH_ONLY = "hash_only"
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"
    CAPABILITY = "capability"
    ENDPOINT = "endpoint"
    MODEL_DIGEST = "model_digest"
    ROUTING_POLICY = "routing_policy"
    POLICY = "policy"


class CompatibilityProbeLevel(StrEnum):
    NONE = "none"
    STANDARD = "standard"
    EXPANDED = "expanded"


class LifecycleReasonCode(StrEnum):
    DISCOVERED = "discovered"
    IDENTITY_UNCHANGED = "identity_unchanged"
    HASH_CHANGED = "hash_changed"
    PATCH_CHANGED = "patch_changed"
    MINOR_CHANGED = "minor_changed"
    MAJOR_CHANGED = "major_changed"
    CAPABILITY_CHANGED = "capability_changed"
    ENDPOINT_CHANGED = "endpoint_changed"
    MODEL_DIGEST_CHANGED = "model_digest_changed"
    ROUTING_POLICY_CHANGED = "routing_policy_changed"
    POLICY_CHANGED = "policy_changed"
    RUNTIME_MISSING = "runtime_missing"
    CREDENTIAL_UNHEALTHY = "credential_unhealthy"
    REQUIRED_CAPABILITY_MISSING = "required_capability_missing"
    POLICY_RANGE_UNSUPPORTED = "policy_range_unsupported"
    PROBE_FAILED = "probe_failed"
    COMPATIBILITY_CONFIRMED = "compatibility_confirmed"
    VERIFICATION_ACCEPTED = "verification_accepted"
    POLICY_PROMOTED = "policy_promoted"
    PATCH_CARRIED_FORWARD = "patch_carried_forward"


_PROVIDER_RUNTIME_KINDS = {
    LifecycleProviderId.CLAUDE_CODE: RuntimeKind.LOCAL_CLI,
    LifecycleProviderId.CODEX: RuntimeKind.LOCAL_CLI,
    LifecycleProviderId.OLLAMA: RuntimeKind.LOCAL_HTTP,
    LifecycleProviderId.OPENROUTER: RuntimeKind.REMOTE_HTTPS,
    LifecycleProviderId.ZAI: RuntimeKind.REMOTE_HTTPS,
}


def _enum(value: object, enum_type: type[StrEnum], *, code: str) -> StrEnum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(code) from exc


def _hash(value: object, *, code: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _HEX_64.fullmatch(value) is None:
        raise ValueError(code)
    return value


def _semantic_version(value: object, *, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_IDENTIFIER_LENGTH
        or _SEMANTIC_VERSION.fullmatch(value) is None
    ):
        raise ValueError(code)
    return value


def _identifier(value: object, *, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_IDENTIFIER_LENGTH
        or _SAFE_IDENTIFIER.fullmatch(value) is None
    ):
        raise ValueError(code)
    return value


def _timestamp(value: object, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**12:
        raise ValueError(code)
    return value


def _capabilities(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not value or len(value) > MAX_CAPABILITIES:
        raise ValueError("capabilities_invalid")
    normalized = tuple(_identifier(item, code="capabilities_invalid") for item in value)
    if len(set(normalized)) != len(normalized):
        raise ValueError("capabilities_invalid")
    return tuple(sorted(normalized))


def _canonical_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_boundary(
    provider: LifecycleProviderId, runtime_kind: RuntimeKind, *, code: str
) -> None:
    if _PROVIDER_RUNTIME_KINDS.get(provider) is not runtime_kind:
        raise ValueError(code)


def _version_triplet(value: str) -> tuple[int, int, int]:
    core = value.split("-", 1)[0].split("+", 1)[0]
    major, minor, patch = core.split(".")
    return int(major), int(minor), int(patch)


def _version_key(
    value: str,
) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
    without_build = value.split("+", 1)[0]
    core, separator, prerelease = without_build.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    if not separator:
        return major, minor, patch, 1, ()
    components: list[tuple[int, int | str]] = []
    for component in prerelease.split("."):
        components.append(
            (0, int(component)) if component.isdigit() else (1, component)
        )
    return major, minor, patch, 0, tuple(components)


@dataclass(frozen=True)
class ProviderRuntimeIdentity(PublicRecord):
    provider: LifecycleProviderId
    runtime_kind: RuntimeKind
    version: str
    runtime_digest: str
    model_identity_digest: str | None
    routing_policy_digest: str | None
    capabilities: tuple[str, ...]
    policy_version: str
    observed_at: int

    _public_fields: ClassVar[tuple[str, ...]] = (
        "provider",
        "runtime_kind",
        "version",
        "runtime_digest",
        "model_identity_digest",
        "routing_policy_digest",
        "capabilities",
        "policy_version",
        "observed_at",
    )

    def __post_init__(self) -> None:
        provider = _enum(self.provider, LifecycleProviderId, code="provider_invalid")
        runtime_kind = _enum(self.runtime_kind, RuntimeKind, code="runtime_kind_invalid")
        assert isinstance(provider, LifecycleProviderId)
        assert isinstance(runtime_kind, RuntimeKind)
        _validate_boundary(provider, runtime_kind, code="provider_runtime_kind_invalid")
        model_digest = _hash(
            self.model_identity_digest, code="model_identity_digest_invalid", optional=True
        )
        routing_digest = _hash(
            self.routing_policy_digest, code="routing_policy_digest_invalid", optional=True
        )
        if provider in {
            LifecycleProviderId.OLLAMA,
            LifecycleProviderId.OPENROUTER,
            LifecycleProviderId.ZAI,
        } and model_digest is None:
            raise ValueError("model_identity_digest_invalid")
        if provider is LifecycleProviderId.OPENROUTER and routing_digest is None:
            raise ValueError("routing_policy_digest_invalid")
        if provider is not LifecycleProviderId.OPENROUTER and routing_digest is not None:
            raise ValueError("routing_policy_digest_invalid")
        if runtime_kind is RuntimeKind.LOCAL_CLI and model_digest is not None:
            raise ValueError("model_identity_digest_invalid")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "runtime_kind", runtime_kind)
        object.__setattr__(
            self, "version", _semantic_version(self.version, code="runtime_version_invalid")
        )
        object.__setattr__(
            self, "runtime_digest", _hash(self.runtime_digest, code="runtime_digest_invalid")
        )
        object.__setattr__(self, "model_identity_digest", model_digest)
        object.__setattr__(self, "routing_policy_digest", routing_digest)
        object.__setattr__(self, "capabilities", _capabilities(self.capabilities))
        object.__setattr__(
            self,
            "policy_version",
            _semantic_version(self.policy_version, code="policy_version_invalid"),
        )
        object.__setattr__(
            self, "observed_at", _timestamp(self.observed_at, code="observed_at_invalid")
        )

    @property
    def digest(self) -> str:
        return _canonical_digest(
            {
                "provider": self.provider.value,
                "runtime_kind": self.runtime_kind.value,
                "version": self.version,
                "runtime_digest": self.runtime_digest,
                "model_identity_digest": self.model_identity_digest,
                "routing_policy_digest": self.routing_policy_digest,
                "capabilities": list(self.capabilities),
                "policy_version": self.policy_version,
            }
        )


@dataclass(frozen=True)
class ProviderCompatibilityPolicy(PublicRecord):
    provider: LifecycleProviderId
    runtime_kind: RuntimeKind
    policy_version: str
    minimum_version: str
    maximum_version_exclusive: str
    required_capabilities: tuple[str, ...]

    _public_fields: ClassVar[tuple[str, ...]] = (
        "provider",
        "runtime_kind",
        "policy_version",
        "minimum_version",
        "maximum_version_exclusive",
        "required_capabilities",
    )

    def __post_init__(self) -> None:
        provider = _enum(self.provider, LifecycleProviderId, code="provider_invalid")
        runtime_kind = _enum(self.runtime_kind, RuntimeKind, code="runtime_kind_invalid")
        assert isinstance(provider, LifecycleProviderId)
        assert isinstance(runtime_kind, RuntimeKind)
        _validate_boundary(provider, runtime_kind, code="compatibility_boundary_invalid")
        minimum = _semantic_version(
            self.minimum_version, code="compatibility_version_range_invalid"
        )
        maximum = _semantic_version(
            self.maximum_version_exclusive, code="compatibility_version_range_invalid"
        )
        if _version_key(minimum) >= _version_key(maximum):
            raise ValueError("compatibility_version_range_invalid")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "runtime_kind", runtime_kind)
        object.__setattr__(
            self,
            "policy_version",
            _semantic_version(self.policy_version, code="policy_version_invalid"),
        )
        object.__setattr__(self, "minimum_version", minimum)
        object.__setattr__(self, "maximum_version_exclusive", maximum)
        object.__setattr__(self, "required_capabilities", _capabilities(self.required_capabilities))

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_dict())

    def supports(self, identity: ProviderRuntimeIdentity) -> bool:
        if not isinstance(identity, ProviderRuntimeIdentity):
            raise ValueError("identity_invalid")
        if identity.provider is not self.provider or identity.runtime_kind is not self.runtime_kind:
            return False
        if identity.policy_version != self.policy_version:
            return False
        version = _version_key(identity.version)
        if not (
            _version_key(self.minimum_version)
            <= version
            < _version_key(self.maximum_version_exclusive)
        ):
            return False
        return set(self.required_capabilities).issubset(identity.capabilities)


@dataclass(frozen=True)
class ProviderCompatibilityAssessment(PublicRecord):
    change: IdentityChange
    state: ProviderLifecycleState
    reason: LifecycleReasonCode
    probe_level: CompatibilityProbeLevel

    _public_fields: ClassVar[tuple[str, ...]] = (
        "change",
        "state",
        "reason",
        "probe_level",
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "change", _enum(self.change, IdentityChange, code="identity_change_invalid")
        )
        object.__setattr__(
            self, "state", _enum(self.state, ProviderLifecycleState, code="lifecycle_state_invalid")
        )
        object.__setattr__(
            self, "reason", _enum(self.reason, LifecycleReasonCode, code="lifecycle_reason_invalid")
        )
        object.__setattr__(
            self,
            "probe_level",
            _enum(self.probe_level, CompatibilityProbeLevel, code="probe_level_invalid"),
        )
        if self.state is ProviderLifecycleState.ACTIVE and not (
            (
                self.change is IdentityChange.UNCHANGED
                and self.reason is LifecycleReasonCode.IDENTITY_UNCHANGED
                and self.probe_level is CompatibilityProbeLevel.NONE
            )
            or (
                self.change is IdentityChange.PATCH
                and self.reason is LifecycleReasonCode.PATCH_CARRIED_FORWARD
                and self.probe_level is CompatibilityProbeLevel.STANDARD
            )
        ):
            raise ValueError("assessment_authority_invalid")


def classify_identity_change(
    previous: ProviderRuntimeIdentity, current: ProviderRuntimeIdentity
) -> IdentityChange:
    if not isinstance(previous, ProviderRuntimeIdentity) or not isinstance(
        current, ProviderRuntimeIdentity
    ):
        raise ValueError("identity_invalid")
    if (
        previous.provider is not current.provider
        or previous.runtime_kind is not current.runtime_kind
    ):
        raise ValueError("identity_boundary_invalid")
    if previous.digest == current.digest:
        return IdentityChange.UNCHANGED
    previous_version = _version_triplet(previous.version)
    current_version = _version_triplet(current.version)
    if previous_version[0] != current_version[0]:
        return IdentityChange.MAJOR
    if previous_version[1] != current_version[1]:
        return IdentityChange.MINOR
    if previous.model_identity_digest != current.model_identity_digest:
        return IdentityChange.MODEL_DIGEST
    if previous.routing_policy_digest != current.routing_policy_digest:
        return IdentityChange.ROUTING_POLICY
    if previous.capabilities != current.capabilities:
        return IdentityChange.CAPABILITY
    if previous.policy_version != current.policy_version:
        return IdentityChange.POLICY
    if (
        previous.runtime_digest != current.runtime_digest
        and current.runtime_kind is not RuntimeKind.LOCAL_CLI
    ):
        return IdentityChange.ENDPOINT
    if previous.version != current.version or previous_version[2] != current_version[2]:
        return IdentityChange.PATCH
    if previous.runtime_digest != current.runtime_digest:
        return IdentityChange.HASH_ONLY
    raise ValueError("identity_change_unclassified")


_CHANGE_REASONS = {
    IdentityChange.UNCHANGED: LifecycleReasonCode.IDENTITY_UNCHANGED,
    IdentityChange.HASH_ONLY: LifecycleReasonCode.HASH_CHANGED,
    IdentityChange.PATCH: LifecycleReasonCode.PATCH_CHANGED,
    IdentityChange.MINOR: LifecycleReasonCode.MINOR_CHANGED,
    IdentityChange.MAJOR: LifecycleReasonCode.MAJOR_CHANGED,
    IdentityChange.CAPABILITY: LifecycleReasonCode.CAPABILITY_CHANGED,
    IdentityChange.ENDPOINT: LifecycleReasonCode.ENDPOINT_CHANGED,
    IdentityChange.MODEL_DIGEST: LifecycleReasonCode.MODEL_DIGEST_CHANGED,
    IdentityChange.ROUTING_POLICY: LifecycleReasonCode.ROUTING_POLICY_CHANGED,
    IdentityChange.POLICY: LifecycleReasonCode.POLICY_CHANGED,
}

_STANDARD_CHANGES = frozenset({IdentityChange.HASH_ONLY, IdentityChange.PATCH})
_EXPANDED_CHANGES = frozenset(
    {
        IdentityChange.MINOR,
        IdentityChange.CAPABILITY,
        IdentityChange.ENDPOINT,
        IdentityChange.MODEL_DIGEST,
        IdentityChange.ROUTING_POLICY,
        IdentityChange.POLICY,
    }
)


def assess_identity_change(
    previous: ProviderRuntimeIdentity | None,
    current: ProviderRuntimeIdentity,
    policy: ProviderCompatibilityPolicy,
    *,
    runtime_available: bool = True,
    credential_healthy: bool = True,
    standard_probe_passed: bool = True,
    expanded_probe_passed: bool = True,
    existing_state: ProviderLifecycleState = ProviderLifecycleState.COMPATIBLE,
) -> ProviderCompatibilityAssessment:
    if not isinstance(current, ProviderRuntimeIdentity) or not isinstance(
        policy, ProviderCompatibilityPolicy
    ):
        raise ValueError("identity_invalid")
    if current.provider is not policy.provider or current.runtime_kind is not policy.runtime_kind:
        raise ValueError("compatibility_boundary_invalid")
    existing = _enum(
        existing_state, ProviderLifecycleState, code="lifecycle_state_invalid"
    )
    assert isinstance(existing, ProviderLifecycleState)
    if not isinstance(runtime_available, bool):
        raise ValueError("runtime_available_invalid")
    if not isinstance(credential_healthy, bool):
        raise ValueError("credential_health_invalid")
    if not isinstance(standard_probe_passed, bool) or not isinstance(
        expanded_probe_passed, bool
    ):
        raise ValueError("probe_result_invalid")
    if not runtime_available:
        return ProviderCompatibilityAssessment(
            IdentityChange.UNCHANGED,
            ProviderLifecycleState.UNAVAILABLE,
            LifecycleReasonCode.RUNTIME_MISSING,
            CompatibilityProbeLevel.NONE,
        )
    if not credential_healthy:
        return ProviderCompatibilityAssessment(
            IdentityChange.UNCHANGED,
            ProviderLifecycleState.UNAVAILABLE,
            LifecycleReasonCode.CREDENTIAL_UNHEALTHY,
            CompatibilityProbeLevel.NONE,
        )
    if previous is None:
        if not policy.supports(current):
            reason = (
                LifecycleReasonCode.REQUIRED_CAPABILITY_MISSING
                if not set(policy.required_capabilities).issubset(current.capabilities)
                else LifecycleReasonCode.POLICY_RANGE_UNSUPPORTED
            )
            return ProviderCompatibilityAssessment(
                IdentityChange.UNCHANGED,
                ProviderLifecycleState.INCOMPATIBLE,
                reason,
                CompatibilityProbeLevel.STANDARD,
            )
        return ProviderCompatibilityAssessment(
            IdentityChange.UNCHANGED,
            ProviderLifecycleState.DISCOVERED,
            LifecycleReasonCode.DISCOVERED,
            CompatibilityProbeLevel.STANDARD,
        )
    change = classify_identity_change(previous, current)
    if change is IdentityChange.MAJOR:
        return ProviderCompatibilityAssessment(
            change,
            ProviderLifecycleState.INCOMPATIBLE,
            LifecycleReasonCode.MAJOR_CHANGED,
            CompatibilityProbeLevel.NONE,
        )
    if not set(policy.required_capabilities).issubset(current.capabilities):
        return ProviderCompatibilityAssessment(
            change,
            ProviderLifecycleState.INCOMPATIBLE,
            LifecycleReasonCode.REQUIRED_CAPABILITY_MISSING,
            CompatibilityProbeLevel.EXPANDED,
        )
    if not policy.supports(current):
        return ProviderCompatibilityAssessment(
            change,
            ProviderLifecycleState.INCOMPATIBLE,
            LifecycleReasonCode.POLICY_RANGE_UNSUPPORTED,
            CompatibilityProbeLevel.NONE,
        )
    if change is IdentityChange.UNCHANGED:
        return ProviderCompatibilityAssessment(
            change,
            existing,
            LifecycleReasonCode.IDENTITY_UNCHANGED,
            CompatibilityProbeLevel.NONE,
        )
    probe_level = (
        CompatibilityProbeLevel.STANDARD
        if change in _STANDARD_CHANGES
        else CompatibilityProbeLevel.EXPANDED
    )
    probe_passed = (
        standard_probe_passed
        if probe_level is CompatibilityProbeLevel.STANDARD
        else expanded_probe_passed
    )
    if not probe_passed:
        return ProviderCompatibilityAssessment(
            change,
            ProviderLifecycleState.INCOMPATIBLE,
            LifecycleReasonCode.PROBE_FAILED,
            probe_level,
        )
    if (
        change is IdentityChange.PATCH
        and current.runtime_kind is RuntimeKind.LOCAL_CLI
        and existing is ProviderLifecycleState.ACTIVE
    ):
        return ProviderCompatibilityAssessment(
            change,
            ProviderLifecycleState.ACTIVE,
            LifecycleReasonCode.PATCH_CARRIED_FORWARD,
            probe_level,
        )
    return ProviderCompatibilityAssessment(
        change,
        ProviderLifecycleState.VERIFICATION_REQUIRED,
        _CHANGE_REASONS[change],
        probe_level,
    )


_VALID_TRANSITIONS = {
    None: frozenset(
        {
            ProviderLifecycleState.DISCOVERED,
            ProviderLifecycleState.INCOMPATIBLE,
            ProviderLifecycleState.UNAVAILABLE,
        }
    ),
    ProviderLifecycleState.DISCOVERED: frozenset(
        {
            ProviderLifecycleState.COMPATIBLE,
            ProviderLifecycleState.VERIFICATION_REQUIRED,
            ProviderLifecycleState.INCOMPATIBLE,
            ProviderLifecycleState.UNAVAILABLE,
        }
    ),
    ProviderLifecycleState.COMPATIBLE: frozenset(
        {
            ProviderLifecycleState.ACTIVE,
            ProviderLifecycleState.VERIFICATION_REQUIRED,
            ProviderLifecycleState.INCOMPATIBLE,
            ProviderLifecycleState.UNAVAILABLE,
        }
    ),
    ProviderLifecycleState.VERIFICATION_REQUIRED: frozenset(
        {
            ProviderLifecycleState.ACTIVE,
            ProviderLifecycleState.INCOMPATIBLE,
            ProviderLifecycleState.UNAVAILABLE,
        }
    ),
    ProviderLifecycleState.ACTIVE: frozenset(
        {
            ProviderLifecycleState.VERIFICATION_REQUIRED,
            ProviderLifecycleState.INCOMPATIBLE,
            ProviderLifecycleState.UNAVAILABLE,
        }
    ),
    ProviderLifecycleState.INCOMPATIBLE: frozenset(
        {
            ProviderLifecycleState.VERIFICATION_REQUIRED,
            ProviderLifecycleState.UNAVAILABLE,
        }
    ),
    ProviderLifecycleState.UNAVAILABLE: frozenset(
        {
            ProviderLifecycleState.DISCOVERED,
            ProviderLifecycleState.INCOMPATIBLE,
        }
    ),
}


@dataclass(frozen=True)
class ProviderLifecycleEvent(PublicRecord):
    event_id: str
    provider: LifecycleProviderId
    runtime_kind: RuntimeKind
    previous_identity_digest: str | None
    current_identity_digest: str | None
    previous_state: ProviderLifecycleState | None
    current_state: ProviderLifecycleState
    reason: LifecycleReasonCode
    policy_version: str
    occurred_at: int

    _public_fields: ClassVar[tuple[str, ...]] = (
        "event_id",
        "provider",
        "runtime_kind",
        "previous_identity_digest",
        "current_identity_digest",
        "previous_state",
        "current_state",
        "reason",
        "policy_version",
        "occurred_at",
    )

    def __post_init__(self) -> None:
        provider = _enum(self.provider, LifecycleProviderId, code="provider_invalid")
        runtime_kind = _enum(self.runtime_kind, RuntimeKind, code="runtime_kind_invalid")
        assert isinstance(provider, LifecycleProviderId)
        assert isinstance(runtime_kind, RuntimeKind)
        _validate_boundary(provider, runtime_kind, code="provider_runtime_kind_invalid")
        previous_state = (
            None
            if self.previous_state is None
            else _enum(
                self.previous_state,
                ProviderLifecycleState,
                code="lifecycle_state_invalid",
            )
        )
        current_state = _enum(
            self.current_state, ProviderLifecycleState, code="lifecycle_state_invalid"
        )
        reason = _enum(self.reason, LifecycleReasonCode, code="lifecycle_reason_invalid")
        assert previous_state is None or isinstance(previous_state, ProviderLifecycleState)
        assert isinstance(current_state, ProviderLifecycleState)
        assert isinstance(reason, LifecycleReasonCode)
        previous_digest = _hash(
            self.previous_identity_digest,
            code="previous_identity_digest_invalid",
            optional=True,
        )
        current_digest = _hash(
            self.current_identity_digest,
            code="current_identity_digest_invalid",
            optional=True,
        )
        if previous_state is None and previous_digest is not None:
            raise ValueError("lifecycle_identity_invalid")
        if (
            previous_state is not None
            and previous_state is not ProviderLifecycleState.UNAVAILABLE
            and previous_digest is None
        ):
            raise ValueError("lifecycle_identity_invalid")
        if current_state is not ProviderLifecycleState.UNAVAILABLE and current_digest is None:
            raise ValueError("lifecycle_identity_invalid")
        if previous_state is current_state:
            identity_unchanged = previous_digest == current_digest
            if identity_unchanged is not (
                reason is LifecycleReasonCode.IDENTITY_UNCHANGED
            ):
                raise ValueError("lifecycle_transition_invalid")
            if not identity_unchanged:
                if current_state is ProviderLifecycleState.ACTIVE:
                    if reason is not LifecycleReasonCode.PATCH_CARRIED_FORWARD:
                        raise ValueError("lifecycle_transition_invalid")
                elif reason not in set(_CHANGE_REASONS.values()):
                    raise ValueError("lifecycle_transition_invalid")
        elif current_state not in _VALID_TRANSITIONS[previous_state]:
            raise ValueError("lifecycle_transition_invalid")
        if reason is LifecycleReasonCode.PATCH_CARRIED_FORWARD and not (
            previous_state is ProviderLifecycleState.ACTIVE
            and current_state is ProviderLifecycleState.ACTIVE
            and previous_digest != current_digest
        ):
            raise ValueError("lifecycle_transition_invalid")
        if (
            current_state is ProviderLifecycleState.ACTIVE
            and previous_state is not current_state
            and reason is not LifecycleReasonCode.VERIFICATION_ACCEPTED
        ):
            raise ValueError("lifecycle_transition_invalid")
        if previous_state is None:
            initial_reasons = {
                ProviderLifecycleState.DISCOVERED: frozenset({LifecycleReasonCode.DISCOVERED}),
                ProviderLifecycleState.INCOMPATIBLE: frozenset(
                    {
                        LifecycleReasonCode.REQUIRED_CAPABILITY_MISSING,
                        LifecycleReasonCode.POLICY_RANGE_UNSUPPORTED,
                        LifecycleReasonCode.PROBE_FAILED,
                    }
                ),
                ProviderLifecycleState.UNAVAILABLE: frozenset(
                    {
                        LifecycleReasonCode.RUNTIME_MISSING,
                        LifecycleReasonCode.CREDENTIAL_UNHEALTHY,
                    }
                ),
            }
            if reason not in initial_reasons[current_state]:
                raise ValueError("lifecycle_transition_invalid")
        if (
            current_state is ProviderLifecycleState.UNAVAILABLE
            and previous_state is not current_state
            and reason
            not in {
                LifecycleReasonCode.RUNTIME_MISSING,
                LifecycleReasonCode.CREDENTIAL_UNHEALTHY,
            }
        ):
            raise ValueError("lifecycle_transition_invalid")
        if (
            previous_state is ProviderLifecycleState.INCOMPATIBLE
            and current_state is ProviderLifecycleState.VERIFICATION_REQUIRED
            and reason is not LifecycleReasonCode.POLICY_PROMOTED
        ):
            raise ValueError("lifecycle_transition_invalid")
        object.__setattr__(self, "event_id", _identifier(self.event_id, code="event_id_invalid"))
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "runtime_kind", runtime_kind)
        object.__setattr__(self, "previous_identity_digest", previous_digest)
        object.__setattr__(self, "current_identity_digest", current_digest)
        object.__setattr__(self, "previous_state", previous_state)
        object.__setattr__(self, "current_state", current_state)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(
            self,
            "policy_version",
            _semantic_version(self.policy_version, code="policy_version_invalid"),
        )
        object.__setattr__(
            self, "occurred_at", _timestamp(self.occurred_at, code="occurred_at_invalid")
        )
