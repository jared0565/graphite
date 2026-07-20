"""Immutable public contracts for development routing."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

MAX_OBJECTIVE_LENGTH = 4_096
MAX_TARGETS = 64
MAX_REASON_COUNT = 32
MAX_ALTERNATIVES = 8
MAX_OUTBOUND_ITEMS = 64
MAX_STRING_LENGTH = 256

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SEMANTIC_VERSION = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$"
)
_DATA_POLICIES = frozenset({"metadata_only", "source_allowed"})
_VALIDATION_OUTCOMES = frozenset({"passed", "failed", "blocked", "not_run"})


class TaskCategory(StrEnum):
    DOCUMENTATION = "documentation"
    ISOLATED_CODE = "isolated_code"
    FEATURE = "feature"
    REFACTOR = "refactor"
    ARCHITECTURE = "architecture"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    TENANT_ISOLATION = "tenant_isolation"
    MIGRATION = "migration"
    DEPLOYMENT = "deployment"
    INFRASTRUCTURE = "infrastructure"
    CONCURRENCY = "concurrency"
    FINANCIAL = "financial"
    LEGAL = "legal"
    UNKNOWN = "unknown"


class RiskTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Effort(StrEnum):
    DEFAULT = "default"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ProviderId(StrEnum):
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    OPENROUTER = "openrouter"


class PermissionMode(StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class EvidenceProvenance(StrEnum):
    MACHINE_VERIFIED = "machine_verified"
    CI_IMPORTED = "ci_imported"
    HUMAN = "human"
    PAIRWISE = "pairwise"
    REVERSION = "reversion"
    AMBIGUOUS = "ambiguous"


class ExecutionOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    DECLINED = "declined"
    BLOCKED = "blocked"
    PROVIDER_FAILED = "provider_failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class FailureReason(StrEnum):
    GRAPH_MISSING = "graph_missing"
    GRAPH_STALE = "graph_stale"
    GRAPH_INVALID = "graph_invalid"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    REGISTRY_MISSING = "registry_missing"
    REGISTRY_STALE = "registry_stale"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_CHANGED = "model_changed"
    EFFORT_UNSUPPORTED = "effort_unsupported"
    CONTEXT_REJECTED = "context_rejected"
    BUDGET_EXHAUSTED = "budget_exhausted"
    APPROVAL_DECLINED = "approval_declined"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_INVALID = "approval_invalid"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_PROTOCOL = "provider_protocol"
    RESPONSE_LIMIT = "response_limit"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


def _bounded_text(value: object, *, code: str, maximum: int = MAX_STRING_LENGTH) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > maximum:
        raise ValueError(code)
    return value


def _bounded_identifier(value: object, *, code: str) -> str:
    text = _bounded_text(value, code=code)
    if any(character.isspace() for character in text):
        raise ValueError(code)
    return text


def _bounded_integer(value: object, *, code: str, minimum: int = 0, maximum: int = 10**12) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise ValueError(code)
    return value


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 1_024:
        raise ValueError("target_invalid")
    normalized = value.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (
        _WINDOWS_ABSOLUTE.match(value)
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("target_invalid")
    return PurePosixPath(*parts).as_posix()


def _enum(value: object, enum_type: type[StrEnum], *, code: str) -> StrEnum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(code) from exc


def _strings(values: object, *, code: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or len(values) > maximum:
        raise ValueError(code)
    return tuple(_bounded_text(value, code=code) for value in values)


def _hash(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not _HEX_64.fullmatch(value):
        raise ValueError(code)
    return value


def _semantic_version(value: object, *, code: str) -> str:
    text = _bounded_identifier(value, code=code)
    if not _SEMANTIC_VERSION.fullmatch(text):
        raise ValueError(code)
    return text


def _git_object(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not _GIT_OBJECT.fullmatch(value):
        raise ValueError(code)
    return value


class PublicRecord:
    _public_fields: ClassVar[tuple[str, ...]] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field_name in self._public_fields:
            value = getattr(self, field_name)
            if isinstance(value, StrEnum):
                result[field_name] = value.value
            elif isinstance(value, tuple):
                result[field_name] = [item.value if isinstance(item, StrEnum) else item for item in value]
            else:
                result[field_name] = value
        return result


@dataclass(frozen=True)
class TaskRequest(PublicRecord):
    objective: str
    repository_root: Path
    targets: tuple[str, ...]
    max_input_tokens: int
    max_output_tokens: int
    data_policy: str
    category_hint: TaskCategory | None = None

    _public_fields = (
        "objective",
        "repository",
        "targets",
        "max_input_tokens",
        "max_output_tokens",
        "data_policy",
        "category_hint",
    )

    def __post_init__(self) -> None:
        if not isinstance(self.objective, str) or not self.objective or "\x00" in self.objective:
            raise ValueError("objective_invalid")
        if len(self.objective) > MAX_OBJECTIVE_LENGTH:
            raise ValueError("objective_too_long")
        objective = self.objective
        if not isinstance(self.repository_root, Path) or not self.repository_root.is_absolute():
            raise ValueError("repository_root_invalid")
        if not isinstance(self.targets, (tuple, list)):
            raise ValueError("targets_invalid")
        if len(self.targets) > MAX_TARGETS:
            raise ValueError("targets_too_many")
        targets = tuple(_relative_path(target) for target in self.targets)
        input_tokens = _bounded_integer(
            self.max_input_tokens,
            code="max_input_tokens_invalid",
            minimum=1,
            maximum=262_144,
        )
        output_tokens = _bounded_integer(
            self.max_output_tokens,
            code="max_output_tokens_invalid",
            minimum=1,
            maximum=32_768,
        )
        if self.data_policy not in _DATA_POLICIES:
            raise ValueError("data_policy_invalid")
        category = None
        if self.category_hint is not None:
            category = _enum(self.category_hint, TaskCategory, code="category_hint_invalid")
        object.__setattr__(self, "objective", objective)
        object.__setattr__(self, "repository_root", self.repository_root.resolve())
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "max_input_tokens", input_tokens)
        object.__setattr__(self, "max_output_tokens", output_tokens)
        object.__setattr__(self, "category_hint", category)

    @property
    def repository(self) -> str:
        return self.repository_root.name


@dataclass(frozen=True)
class TaskProfile(PublicRecord):
    task_id: str
    category: TaskCategory
    risk: RiskTier
    complexity: int
    impact_radius: int
    risk_flags: tuple[str, ...]
    context_requirements: tuple[str, ...]
    verification_requirements: tuple[str, ...]

    _public_fields = (
        "task_id",
        "category",
        "risk",
        "complexity",
        "impact_radius",
        "risk_flags",
        "context_requirements",
        "verification_requirements",
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _bounded_identifier(self.task_id, code="task_id_invalid"))
        object.__setattr__(self, "category", _enum(self.category, TaskCategory, code="category_invalid"))
        object.__setattr__(self, "risk", _enum(self.risk, RiskTier, code="risk_invalid"))
        object.__setattr__(self, "complexity", _bounded_integer(self.complexity, code="complexity_invalid", maximum=10))
        object.__setattr__(self, "impact_radius", _bounded_integer(self.impact_radius, code="impact_radius_invalid", maximum=100_000))
        object.__setattr__(self, "risk_flags", _strings(self.risk_flags, code="risk_flags_invalid", maximum=MAX_REASON_COUNT))
        object.__setattr__(self, "context_requirements", _strings(self.context_requirements, code="context_requirements_invalid", maximum=MAX_REASON_COUNT))
        object.__setattr__(self, "verification_requirements", _strings(self.verification_requirements, code="verification_requirements_invalid", maximum=MAX_REASON_COUNT))


@dataclass(frozen=True)
class ModelProfile(PublicRecord):
    model_id: str
    profile_version: str
    capabilities: tuple[str, ...]
    context_window_tokens: int
    supported_efforts: tuple[Effort, ...]
    provisional: bool

    _public_fields = (
        "model_id",
        "profile_version",
        "capabilities",
        "context_window_tokens",
        "supported_efforts",
        "provisional",
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", _bounded_identifier(self.model_id, code="model_id_invalid"))
        object.__setattr__(self, "profile_version", _bounded_identifier(self.profile_version, code="profile_version_invalid"))
        object.__setattr__(self, "capabilities", _strings(self.capabilities, code="capabilities_invalid", maximum=32))
        object.__setattr__(self, "context_window_tokens", _bounded_integer(self.context_window_tokens, code="context_window_tokens_invalid", minimum=1, maximum=10_000_000))
        if not isinstance(self.supported_efforts, (tuple, list)) or not self.supported_efforts:
            raise ValueError("effort_invalid")
        efforts = tuple(_enum(item, Effort, code="effort_invalid") for item in self.supported_efforts)
        if len(set(efforts)) != len(efforts):
            raise ValueError("effort_invalid")
        if not isinstance(self.provisional, bool):
            raise ValueError("provisional_invalid")
        object.__setattr__(self, "supported_efforts", efforts)


@dataclass(frozen=True)
class CliIdentity(PublicRecord):
    provider: ProviderId
    executable_sha256: str
    cli_version: str
    adapter_protocol_version: str

    _public_fields = (
        "provider",
        "executable_sha256",
        "cli_version",
        "adapter_protocol_version",
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _enum(self.provider, ProviderId, code="provider_invalid"))
        object.__setattr__(
            self,
            "executable_sha256",
            _hash(self.executable_sha256, code="executable_sha256_invalid"),
        )
        object.__setattr__(
            self, "cli_version", _semantic_version(self.cli_version, code="cli_version_invalid")
        )
        object.__setattr__(
            self,
            "adapter_protocol_version",
            _semantic_version(
                self.adapter_protocol_version, code="adapter_protocol_version_invalid"
            ),
        )


@dataclass(frozen=True)
class CapabilityProfile(PublicRecord):
    provider: ProviderId
    requested_model: str
    effective_model: str
    profile_version: str
    capabilities: tuple[str, ...]
    context_window_tokens: int
    supported_efforts: tuple[Effort, ...]
    risk_ceiling: RiskTier
    permission_mode: PermissionMode

    _public_fields = (
        "provider",
        "requested_model",
        "effective_model",
        "profile_version",
        "capabilities",
        "context_window_tokens",
        "supported_efforts",
        "risk_ceiling",
        "permission_mode",
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _enum(self.provider, ProviderId, code="provider_invalid"))
        object.__setattr__(
            self,
            "requested_model",
            _bounded_identifier(self.requested_model, code="requested_model_invalid"),
        )
        object.__setattr__(
            self,
            "effective_model",
            _bounded_identifier(self.effective_model, code="effective_model_invalid"),
        )
        object.__setattr__(
            self,
            "profile_version",
            _bounded_identifier(self.profile_version, code="profile_version_invalid"),
        )
        capabilities = _strings(self.capabilities, code="capabilities_invalid", maximum=32)
        if not capabilities or len(set(capabilities)) != len(capabilities):
            raise ValueError("capabilities_invalid")
        object.__setattr__(self, "capabilities", tuple(sorted(capabilities)))
        object.__setattr__(
            self,
            "context_window_tokens",
            _bounded_integer(
                self.context_window_tokens,
                code="context_window_tokens_invalid",
                minimum=1,
                maximum=10_000_000,
            ),
        )
        if not isinstance(self.supported_efforts, (tuple, list)) or not self.supported_efforts:
            raise ValueError("effort_invalid")
        efforts = tuple(_enum(item, Effort, code="effort_invalid") for item in self.supported_efforts)
        if len(set(efforts)) != len(efforts):
            raise ValueError("effort_invalid")
        object.__setattr__(self, "supported_efforts", tuple(sorted(efforts, key=lambda item: item.value)))
        object.__setattr__(
            self, "risk_ceiling", _enum(self.risk_ceiling, RiskTier, code="risk_ceiling_invalid")
        )
        object.__setattr__(
            self,
            "permission_mode",
            _enum(self.permission_mode, PermissionMode, code="permission_mode_invalid"),
        )


@dataclass(frozen=True)
class CapabilitySnapshot(PublicRecord):
    schema_version: int
    verified_at: int
    expires_at: int
    identity: CliIdentity
    profile: CapabilityProfile

    _public_fields = ("schema_version", "verified_at", "expires_at", "identity", "profile")

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            _bounded_integer(self.schema_version, code="snapshot_schema_invalid", minimum=1, maximum=10),
        )
        verified = _bounded_integer(self.verified_at, code="snapshot_time_invalid")
        expires = _bounded_integer(self.expires_at, code="snapshot_time_invalid")
        if expires <= verified:
            raise ValueError("snapshot_time_invalid")
        if not isinstance(self.identity, CliIdentity) or not isinstance(self.profile, CapabilityProfile):
            raise ValueError("snapshot_identity_invalid")
        if self.identity.provider is not self.profile.provider:
            raise ValueError("snapshot_identity_invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verified_at": self.verified_at,
            "expires_at": self.expires_at,
            "identity": self.identity.to_dict(),
            "profile": self.profile.to_dict(),
        }

    @property
    def digest(self) -> str:
        payload = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class RoutingDecision(PublicRecord):
    decision_id: str
    task_id: str
    selected_model: str | None
    effort: Effort | None
    score: int | float
    confidence: float
    alternatives: tuple[str, ...]
    reasons: tuple[str, ...]
    policy_version: str
    evidence_version: str

    _public_fields = (
        "decision_id",
        "task_id",
        "selected_model",
        "effort",
        "score",
        "confidence",
        "alternatives",
        "reasons",
        "policy_version",
        "evidence_version",
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", _bounded_identifier(self.decision_id, code="decision_id_invalid"))
        object.__setattr__(self, "task_id", _bounded_identifier(self.task_id, code="task_id_invalid"))
        if self.selected_model is not None:
            object.__setattr__(self, "selected_model", _bounded_identifier(self.selected_model, code="model_id_invalid"))
        effort = None if self.effort is None else _enum(self.effort, Effort, code="effort_invalid")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)) or not math.isfinite(self.score) or not 0 <= self.score <= 1_000:
            raise ValueError("score_invalid")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)) or not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence_invalid")
        object.__setattr__(self, "effort", effort)
        object.__setattr__(self, "alternatives", _strings(self.alternatives, code="alternatives_invalid", maximum=MAX_ALTERNATIVES))
        object.__setattr__(self, "reasons", _strings(self.reasons, code="reasons_invalid", maximum=MAX_REASON_COUNT))
        object.__setattr__(self, "policy_version", _bounded_identifier(self.policy_version, code="policy_version_invalid"))
        object.__setattr__(self, "evidence_version", _bounded_identifier(self.evidence_version, code="evidence_version_invalid"))


@dataclass(frozen=True)
class ApprovalManifest(PublicRecord):
    approval_id: str
    task_id: str
    decision_id: str
    graph_fingerprint: str
    context_manifest_hash: str
    inventory_digest: str
    model_id: str
    effort: Effort
    max_input_tokens: int
    max_output_tokens: int
    policy_version: str
    issued_at: int
    expires_at: int
    nonce: str

    _public_fields = (
        "approval_id",
        "task_id",
        "decision_id",
        "graph_fingerprint",
        "context_manifest_hash",
        "inventory_digest",
        "model_id",
        "effort",
        "max_input_tokens",
        "max_output_tokens",
        "policy_version",
        "issued_at",
        "expires_at",
        "nonce",
    )

    def __post_init__(self) -> None:
        for name in ("approval_id", "task_id", "decision_id", "model_id", "policy_version", "nonce"):
            object.__setattr__(self, name, _bounded_identifier(getattr(self, name), code=f"{name}_invalid"))
        object.__setattr__(self, "graph_fingerprint", _hash(self.graph_fingerprint, code="graph_fingerprint_invalid"))
        object.__setattr__(self, "context_manifest_hash", _hash(self.context_manifest_hash, code="context_manifest_hash_invalid"))
        object.__setattr__(self, "inventory_digest", _hash(self.inventory_digest, code="inventory_digest_invalid"))
        object.__setattr__(self, "effort", _enum(self.effort, Effort, code="effort_invalid"))
        object.__setattr__(self, "max_input_tokens", _bounded_integer(self.max_input_tokens, code="max_input_tokens_invalid", minimum=1, maximum=262_144))
        object.__setattr__(self, "max_output_tokens", _bounded_integer(self.max_output_tokens, code="max_output_tokens_invalid", minimum=1, maximum=32_768))
        issued = _bounded_integer(self.issued_at, code="issued_at_invalid")
        expires = _bounded_integer(self.expires_at, code="expires_at_invalid")
        if expires <= issued:
            raise ValueError("expires_at_invalid")


@dataclass(frozen=True)
class CliApprovalManifest(PublicRecord):
    approval_id: str
    task_id: str
    decision_id: str
    provider: ProviderId
    requested_model: str
    effective_model: str
    effort: Effort
    cli_executable_sha256: str
    cli_version: str
    adapter_protocol_version: str
    capability_snapshot_digest: str
    graph_fingerprint: str
    context_manifest_hash: str
    repository_commit: str
    worktree_id: str
    permission_mode: PermissionMode
    max_input_tokens: int
    max_output_tokens: int
    policy_version: str
    issued_at: int
    expires_at: int
    nonce: str

    _public_fields = (
        "approval_id",
        "task_id",
        "decision_id",
        "provider",
        "requested_model",
        "effective_model",
        "effort",
        "cli_executable_sha256",
        "cli_version",
        "adapter_protocol_version",
        "capability_snapshot_digest",
        "graph_fingerprint",
        "context_manifest_hash",
        "repository_commit",
        "worktree_id",
        "permission_mode",
        "max_input_tokens",
        "max_output_tokens",
        "policy_version",
        "issued_at",
        "expires_at",
        "nonce",
    )

    def __post_init__(self) -> None:
        for name in ("approval_id", "task_id", "decision_id", "policy_version", "nonce"):
            object.__setattr__(
                self, name, _bounded_identifier(getattr(self, name), code=f"{name}_invalid")
            )
        object.__setattr__(self, "provider", _enum(self.provider, ProviderId, code="provider_invalid"))
        for name in ("requested_model", "effective_model", "worktree_id"):
            object.__setattr__(
                self, name, _bounded_identifier(getattr(self, name), code=f"{name}_invalid")
            )
        object.__setattr__(self, "effort", _enum(self.effort, Effort, code="effort_invalid"))
        object.__setattr__(
            self,
            "cli_executable_sha256",
            _hash(self.cli_executable_sha256, code="cli_executable_sha256_invalid"),
        )
        object.__setattr__(
            self, "cli_version", _semantic_version(self.cli_version, code="cli_version_invalid")
        )
        object.__setattr__(
            self,
            "adapter_protocol_version",
            _semantic_version(
                self.adapter_protocol_version, code="adapter_protocol_version_invalid"
            ),
        )
        for name in (
            "capability_snapshot_digest",
            "graph_fingerprint",
            "context_manifest_hash",
        ):
            object.__setattr__(self, name, _hash(getattr(self, name), code=f"{name}_invalid"))
        object.__setattr__(
            self,
            "repository_commit",
            _git_object(self.repository_commit, code="repository_commit_invalid"),
        )
        object.__setattr__(
            self,
            "permission_mode",
            _enum(self.permission_mode, PermissionMode, code="permission_mode_invalid"),
        )
        object.__setattr__(
            self,
            "max_input_tokens",
            _bounded_integer(
                self.max_input_tokens,
                code="max_input_tokens_invalid",
                minimum=1,
                maximum=262_144,
            ),
        )
        object.__setattr__(
            self,
            "max_output_tokens",
            _bounded_integer(
                self.max_output_tokens,
                code="max_output_tokens_invalid",
                minimum=1,
                maximum=32_768,
            ),
        )
        issued = _bounded_integer(self.issued_at, code="issued_at_invalid")
        expires = _bounded_integer(self.expires_at, code="expires_at_invalid")
        if expires <= issued:
            raise ValueError("expires_at_invalid")


@dataclass(frozen=True)
class ExecutionReceipt(PublicRecord):
    execution_id: str
    approval_id: str
    model_id: str
    effort: Effort
    outcome: ExecutionOutcome
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    prompt_hash: str
    response_hash: str | None
    failure_reason: FailureReason | None
    provider: ProviderId | None = None
    effective_model: str | None = None
    cli_version: str | None = None
    changed_file_count: int | None = None
    changed_byte_count: int | None = None
    validation_outcome: str | None = None

    _public_fields = (
        "execution_id",
        "approval_id",
        "model_id",
        "effort",
        "outcome",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "prompt_hash",
        "response_hash",
        "failure_reason",
        "provider",
        "effective_model",
        "cli_version",
        "changed_file_count",
        "changed_byte_count",
        "validation_outcome",
    )

    def __post_init__(self) -> None:
        for name in ("execution_id", "approval_id", "model_id"):
            object.__setattr__(self, name, _bounded_identifier(getattr(self, name), code=f"{name}_invalid"))
        object.__setattr__(self, "effort", _enum(self.effort, Effort, code="effort_invalid"))
        object.__setattr__(self, "outcome", _enum(self.outcome, ExecutionOutcome, code="outcome_invalid"))
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _bounded_integer(value, code=f"{name}_invalid", maximum=10_000_000))
        object.__setattr__(self, "latency_ms", _bounded_integer(self.latency_ms, code="latency_ms_invalid", maximum=86_400_000))
        object.__setattr__(self, "prompt_hash", _hash(self.prompt_hash, code="prompt_hash_invalid"))
        if self.response_hash is not None:
            object.__setattr__(self, "response_hash", _hash(self.response_hash, code="response_hash_invalid"))
        if self.failure_reason is not None:
            object.__setattr__(self, "failure_reason", _enum(self.failure_reason, FailureReason, code="failure_reason_invalid"))
        if self.provider is not None:
            object.__setattr__(self, "provider", _enum(self.provider, ProviderId, code="provider_invalid"))
        if self.effective_model is not None:
            object.__setattr__(
                self,
                "effective_model",
                _bounded_identifier(self.effective_model, code="effective_model_invalid"),
            )
        if self.cli_version is not None:
            object.__setattr__(
                self, "cli_version", _semantic_version(self.cli_version, code="cli_version_invalid")
            )
        for name in ("changed_file_count", "changed_byte_count"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self, name, _bounded_integer(value, code=f"{name}_invalid", maximum=10_000_000)
                )
        if self.validation_outcome is not None and self.validation_outcome not in _VALIDATION_OUTCOMES:
            raise ValueError("validation_outcome_invalid")


@dataclass(frozen=True)
class VerifiedOutcome(PublicRecord):
    outcome_id: str
    execution_id: str
    provenance: EvidenceProvenance
    build_passed: bool | None
    tests_passed: bool | None
    security_passed: bool | None
    human_accepted: bool | None
    repair_count: int
    escalated: bool
    reverted: bool
    severe_failure: bool
    recorded_at: int

    _public_fields = (
        "outcome_id",
        "execution_id",
        "provenance",
        "build_passed",
        "tests_passed",
        "security_passed",
        "human_accepted",
        "repair_count",
        "escalated",
        "reverted",
        "severe_failure",
        "recorded_at",
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome_id", _bounded_identifier(self.outcome_id, code="outcome_id_invalid"))
        object.__setattr__(self, "execution_id", _bounded_identifier(self.execution_id, code="execution_id_invalid"))
        object.__setattr__(self, "provenance", _enum(self.provenance, EvidenceProvenance, code="provenance_invalid"))
        for name in ("build_passed", "tests_passed", "security_passed", "human_accepted"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{name}_invalid")
        object.__setattr__(self, "repair_count", _bounded_integer(self.repair_count, code="repair_count_invalid", maximum=1_000))
        for name in ("escalated", "reverted", "severe_failure"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name}_invalid")
        object.__setattr__(self, "recorded_at", _bounded_integer(self.recorded_at, code="recorded_at_invalid"))
