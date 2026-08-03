"""Approval-gated orchestration for authenticated development CLIs."""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from graphite.config import Config
from graphite.freshness import check_graph_freshness
from graphite.git import GitError, GitRunner
from graphite.graph_io import GraphReadError, load_validated_graph_bundle

from .approval import ApprovalAuthority, ApprovalError
from .classifier import classify_task
from .claude_executor import AdapterError, execute_claude, preflight_claude
from .codex_executor import execute_codex, preflight_codex
from .context_builder import ContextBundle, build_routing_context
from .contracts import (
    CapabilitySnapshot,
    CliApprovalManifest,
    CliIdentity,
    Effort,
    ExecutionOutcome,
    ExecutionReceipt,
    PermissionMode,
    ProviderId,
    RiskTier,
    TaskRequest,
)
from .diff_policy import DiffPolicyError, collect_diff_evidence
from .policy import (
    CliCandidateMetrics,
    CliPolicyGates,
    rank_cli_candidates,
)
from .process_runner import CliProcessError, run_cli_process
from .profiles import load_verified_capability_snapshots
from .lifecycle import ProviderRuntimeIdentity
from .lifecycle_service import LifecycleServiceError, ProviderLifecycleService
from .prompt import CanonicalPrompt, canonical_cli_prompt
from .settings import RoutingSettings
from .storage import (
    DEFAULT_RECOVERY_PAGE_SIZE,
    RecoverableAttemptPage,
    RepositoryStore,
    StorageError,
)
from .worktree import (
    TaskWorktree,
    WorktreeError,
    cleanup_task_worktree,
    create_task_worktree,
)


class RoutingServiceError(RuntimeError):
    """Stable orchestration failure with no repository or provider diagnostics.

    `cause` is a diagnostic carried in the MESSAGE only, and only ever an
    exception class name -- never a message, path or argv. This is the error
    that reaches a CI log, so a detail that does not survive to here does not
    exist as far as anyone debugging is concerned.
    """

    def __init__(self, code: str, cause: str | None = None) -> None:
        self.code = code
        self.cause = cause
        super().__init__(f"{code} ({cause})" if cause else code)


class AdapterResult(Protocol):
    effective_model: str
    message: str
    input_tokens: int | None
    output_tokens: int | None
    duration_seconds: float


IdentityLoader = Callable[[ProviderId], CliIdentity]
RuntimeIdentityLoader = Callable[[ProviderId], ProviderRuntimeIdentity]
Executor = Callable[..., AdapterResult]
Validator = Callable[[TaskWorktree], bool]


@dataclass(frozen=True, slots=True)
class RoutingRecommendation:
    task_id: str
    provider: ProviderId | None
    requested_model: str | None
    effective_model: str | None
    effort: Effort | None
    snapshot_expires_at: int | None
    permission_mode: PermissionMode | None
    risk: str
    estimated_tokens: int
    outbound_manifest: dict[str, Any]
    reasons: tuple[str, ...]
    manual_handoff: bool
    policy_version: str

    @property
    def model_id(self) -> str | None:
        """Compatibility alias for callers transitioning from legacy routing."""
        return self.requested_model

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "provider": None if self.provider is None else self.provider.value,
            "requested_model": self.requested_model,
            "effective_model": self.effective_model,
            "effort": None if self.effort is None else self.effort.value,
            "snapshot_expires_at": self.snapshot_expires_at,
            "permission_mode": (
                None if self.permission_mode is None else self.permission_mode.value
            ),
            "risk": self.risk,
            "estimated_tokens": self.estimated_tokens,
            "outbound_manifest": self.outbound_manifest,
            "reasons": list(self.reasons),
            "manual_handoff": self.manual_handoff,
            "policy_version": self.policy_version,
            "execution_authority": "single_use_approval_required",
            "automatic_retry": False,
            "automatic_fallback": False,
            "automatic_merge": False,
        }


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    task_id: str
    decision_id: str
    attempt_id: str
    worktree: TaskWorktree
    manifest: CliApprovalManifest
    prompt: CanonicalPrompt = field(repr=False, compare=False)
    graph_fingerprint: str
    lifecycle_identity_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "decision_id": self.decision_id,
            "attempt_id": self.attempt_id,
            "worktree_id": self.worktree.worktree_id,
            "repository_commit": self.worktree.baseline_commit,
            "provider": self.manifest.provider.value,
            "requested_model": self.manifest.requested_model,
            "effective_model": self.manifest.effective_model,
            "effort": self.manifest.effort.value,
            "permission_mode": self.manifest.permission_mode.value,
            "prompt_hash": self.prompt.prompt_hash,
            "approval_required": True,
        }


@dataclass(frozen=True, slots=True)
class ApprovedExecution:
    """Ephemeral provider message plus persistence-safe evidence."""

    text: str = field(repr=False, compare=False)
    receipt: ExecutionReceipt
    diff_hash: str

    def to_public_dict(self) -> dict[str, Any]:
        payload = dict(self.receipt.to_dict())
        payload["diff_hash"] = self.diff_hash
        return payload


@dataclass(frozen=True, slots=True)
class _RecommendationState:
    request: TaskRequest
    task: Any
    context: ContextBundle
    graph_fingerprint: str
    snapshot: CapabilitySnapshot
    recommendation: RoutingRecommendation
    lifecycle_identity_digest: str | None = None


def _machine_state_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return (
            Path(base) if base else Path.home() / "AppData" / "Local"
        ) / "Graphite" / "routing"
    xdg = os.environ.get("XDG_STATE_HOME")
    return (
        Path(xdg) if xdg else Path.home() / ".local" / "state"
    ) / "graphite" / "routing"


def _default_executable(provider: ProviderId) -> Path:
    name = "claude" if provider is ProviderId.CLAUDE_CODE else "codex"
    selected = shutil.which(name)
    if not selected:
        raise RoutingServiceError("cli_missing")
    try:
        return Path(selected).resolve(strict=True)
    except OSError:
        raise RoutingServiceError("cli_missing") from None


def _default_credential_home(provider: ProviderId) -> Path:
    environment_name = (
        "CLAUDE_CONFIG_DIR" if provider is ProviderId.CLAUDE_CODE else "CODEX_HOME"
    )
    configured = os.environ.get(environment_name)
    candidate = Path(configured) if configured else Path.home() / (
        ".claude" if provider is ProviderId.CLAUDE_CODE else ".codex"
    )
    try:
        return candidate.resolve(strict=True)
    except OSError:
        raise RoutingServiceError("credential_home_missing") from None


class RoutingService:
    """Coordinates recommendation, preparation, one-shot execution, and review gates."""

    def __init__(
        self,
        path: str | Path,
        *,
        state_dir: Path | None = None,
        settings: RoutingSettings | None = None,
        identity_loader: IdentityLoader | None = None,
        executables: Mapping[ProviderId, Path] | None = None,
        credential_homes: Mapping[ProviderId, Path] | None = None,
        executors: Mapping[ProviderId, Executor] | None = None,
        validator: Validator | None = None,
        validation_commands: tuple[tuple[str, ...], ...] = (),
        lifecycle_service: ProviderLifecycleService | None = None,
        lifecycle_boundaries: Mapping[ProviderId, str] | None = None,
        runtime_identity_loader: RuntimeIdentityLoader | None = None,
    ) -> None:
        try:
            self.root = Path(path).resolve(strict=True)
        except OSError:
            raise RoutingServiceError("repository_root_invalid") from None
        if not self.root.is_dir():
            raise RoutingServiceError("repository_root_invalid")
        self.settings = settings or RoutingSettings.from_env()
        self.store = RepositoryStore(self.root)
        self.state_dir = (state_dir or _machine_state_dir()).resolve(strict=False)
        self._executables = dict(executables or {})
        self._credential_homes = dict(credential_homes or {})
        self._identity_loader = identity_loader or self._preflight
        self._executors: dict[ProviderId, Executor] = {
            ProviderId.CLAUDE_CODE: execute_claude,
            ProviderId.CODEX: execute_codex,
        }
        if executors:
            self._executors.update(executors)
        if not isinstance(validation_commands, tuple) or any(
            not isinstance(command, tuple)
            or not command
            or any(not isinstance(value, str) or not value for value in command)
            for command in validation_commands
        ):
            raise RoutingServiceError("validation_command_invalid")
        self._validation_commands = validation_commands
        self._validator = validator or self._validate
        self._lifecycle_service = lifecycle_service
        self._lifecycle_boundaries = dict(lifecycle_boundaries or {})
        self._runtime_identity_loader = runtime_identity_loader
        if (lifecycle_service is None) is not (runtime_identity_loader is None) or (
            lifecycle_service is not None and not self._lifecycle_boundaries
        ):
            raise RoutingServiceError("lifecycle_configuration_invalid")
        self._recommendations: dict[str, _RecommendationState] = {}
        self._prepared: dict[str, PreparedExecution] = {}
        self._snapshots: dict[str, CapabilitySnapshot] = {}
        self._review_primary: dict[str, tuple[str, str]] = {}

    def _executable(self, provider: ProviderId) -> Path:
        return self._executables.get(provider) or _default_executable(provider)

    def _credential_home(self, provider: ProviderId) -> Path:
        return self._credential_homes.get(provider) or _default_credential_home(provider)

    def _preflight(self, provider: ProviderId) -> CliIdentity:
        kwargs = {
            "executable": self._executable(provider),
            "workspace": self.root,
            "credential_home": self._credential_home(provider),
        }
        if provider is ProviderId.CLAUDE_CODE:
            return preflight_claude(**kwargs)
        return preflight_codex(**kwargs)

    @staticmethod
    def _graph_fingerprint(bundle: object) -> str:
        return hashlib.sha256(
            json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def recommend(
        self, *, objective: str, targets: tuple[str, ...]
    ) -> RoutingRecommendation:
        request = TaskRequest(
            objective=objective,
            repository_root=self.root,
            targets=tuple(targets),
            max_input_tokens=self.settings.max_input_tokens,
            max_output_tokens=self.settings.max_output_tokens,
            data_policy="source_allowed",
        )
        cfg = Config.from_env()
        freshness = check_graph_freshness(self.root, cfg)
        if freshness.get("stale", True):
            return self._handoff("unclassified", "unknown", "graph_stale")
        try:
            self.store.initialize()
            bundle, graph = load_validated_graph_bundle(
                self.root / cfg.output_dir / "graph.json", root=self.root
            )
            task = classify_task(request, graph)
            context = build_routing_context(request, graph, self.settings)
            now = int(time.time())
            active_lifecycle_digests: frozenset[str] | None = None
            if self._lifecycle_service is not None:
                active: set[str] = set()
                for boundary in self._lifecycle_boundaries.values():
                    try:
                        active.add(
                            self._lifecycle_service.active_identity_digest(boundary)
                        )
                    except LifecycleServiceError:
                        continue
                active_lifecycle_digests = frozenset(active)
            snapshots = load_verified_capability_snapshots(
                self.store,
                now=now,
                active_lifecycle_identity_digests=active_lifecycle_digests,
            )
        except (GraphReadError, StorageError, OSError, ValueError) as exc:
            return self._handoff(
                "unclassified", "unknown", getattr(exc, "code", "routing_evidence_blocked")
            )
        identities: list[CliIdentity] = []
        providers: list[ProviderId] = []
        for provider in sorted({item.profile.provider for item in snapshots}, key=str):
            try:
                identity = self._identity_loader(provider)
            except (AdapterError, RoutingServiceError, OSError, ValueError):
                continue
            identities.append(identity)
            providers.append(provider)
        context_tokens = max(1, (context.manifest.total_bytes + 3) // 4)
        candidates = tuple(
            CliCandidateMetrics(
                capability_snapshot_digest=snapshot.digest,
                effort=snapshot.profile.supported_efforts[0],
                repository_success_millis=None,
                global_success_millis=500,
                sample_count=0,
                expected_input_tokens=context_tokens,
                expected_output_tokens=request.max_output_tokens,
                expected_latency_ms=30_000,
                quota_scarcity_millis=0,
            )
            for snapshot in snapshots
        )
        ranked = rank_cli_candidates(
            task,
            snapshots,
            candidates,
            CliPolicyGates(
                authenticated_providers=tuple(providers),
                current_cli_identities=tuple(identities),
                permission_mode=PermissionMode.WORKSPACE_WRITE,
                context_tokens=context_tokens,
                budget_tokens=min(
                    self.settings.repository_quota_tokens,
                    request.max_input_tokens + request.max_output_tokens,
                ),
                now=now,
            ),
        )
        selected = ranked.selected
        snapshot = None if selected is None else next(
            item for item in snapshots if item.digest == selected.capability_snapshot_digest
        )
        recommendation = RoutingRecommendation(
            task_id=task.task_id,
            provider=None if selected is None else selected.provider,
            requested_model=None if selected is None else selected.requested_model,
            effective_model=None if selected is None else selected.effective_model,
            effort=None if selected is None else selected.effort,
            snapshot_expires_at=None if snapshot is None else snapshot.expires_at,
            permission_mode=(
                None if snapshot is None else snapshot.profile.permission_mode
            ),
            risk=task.risk.value,
            estimated_tokens=context_tokens + request.max_output_tokens,
            outbound_manifest=context.manifest.to_dict(),
            reasons=ranked.reasons,
            manual_handoff=ranked.manual_handoff,
            policy_version=ranked.policy_version,
        )
        if snapshot is not None:
            lifecycle_digest = None
            if self._lifecycle_service is not None:
                lifecycle_digest = self.store.lifecycle_identity_binding(
                    authority_kind="capability_snapshot", authority_id=snapshot.digest
                )
            state = _RecommendationState(
                request,
                task,
                context,
                self._graph_fingerprint(bundle),
                snapshot,
                recommendation,
                lifecycle_digest,
            )
            self._recommendations[task.task_id] = state
            self._snapshots[snapshot.digest] = snapshot
        return recommendation

    @staticmethod
    def _handoff(task_id: str, risk: str, reason: str) -> RoutingRecommendation:
        return RoutingRecommendation(
            task_id,
            None,
            None,
            None,
            None,
            None,
            None,
            risk,
            0,
            {"items": [], "total_bytes": 0},
            (str(reason),),
            True,
            "3",
        )

    def _head_commit(self) -> str:
        try:
            result = GitRunner(self.root).run(
                ["rev-parse", "HEAD"], timeout_seconds=5.0, max_stdout_bytes=256
            )
            value = result.stdout.decode("ascii").strip()
        except (GitError, UnicodeDecodeError, OSError):
            raise RoutingServiceError("repository_commit_unavailable") from None
        if result.returncode != 0 or len(value) not in {40, 64}:
            raise RoutingServiceError("repository_commit_unavailable")
        return value

    def prepare(self, recommendation: RoutingRecommendation) -> PreparedExecution:
        if not isinstance(recommendation, RoutingRecommendation):
            raise RoutingServiceError("recommendation_invalid")
        if recommendation.manual_handoff or recommendation.provider is None:
            raise RoutingServiceError("manual_handoff_required")
        if any(
            prepared.task_id == recommendation.task_id
            for prepared in self._prepared.values()
        ):
            raise RoutingServiceError("transition_replay")
        state = self._recommendations.pop(recommendation.task_id, None)
        if state is None or state.recommendation != recommendation:
            raise RoutingServiceError("recommendation_expired")
        now = int(time.time())
        commit = self._head_commit()
        worktree_id = "worktree-" + secrets.token_hex(12)
        worktree = create_task_worktree(
            source_root=self.root,
            state_root=self.state_dir / "worktrees",
            task_id=worktree_id,
            approved_commit=commit,
        )
        decision_id = "decision-" + secrets.token_hex(12)
        attempt_id = "attempt-" + secrets.token_hex(12)
        approval_id = "approval-" + secrets.token_hex(12)
        prompt = canonical_cli_prompt(
            objective=state.request.objective, context=state.context
        )
        snapshot = state.snapshot
        manifest = CliApprovalManifest(
            approval_id=approval_id,
            task_id=state.task.task_id,
            decision_id=decision_id,
            provider=snapshot.profile.provider,
            requested_model=snapshot.profile.requested_model,
            effective_model=snapshot.profile.effective_model,
            effort=snapshot.profile.supported_efforts[0],
            cli_executable_sha256=snapshot.identity.executable_sha256,
            cli_version=snapshot.identity.cli_version,
            adapter_protocol_version=snapshot.identity.adapter_protocol_version,
            capability_snapshot_digest=snapshot.digest,
            graph_fingerprint=state.graph_fingerprint,
            context_manifest_hash=state.context.manifest.manifest_hash,
            repository_commit=commit,
            worktree_id=worktree_id,
            permission_mode=PermissionMode.WORKSPACE_WRITE,
            max_input_tokens=state.request.max_input_tokens,
            max_output_tokens=state.request.max_output_tokens,
            policy_version=recommendation.policy_version,
            issued_at=now,
            expires_at=now + self.settings.approval_ttl_seconds,
            nonce=secrets.token_hex(24),
        )
        self.store.record_task(
            state.task.task_id,
            state.task.category.value,
            state.task.risk.value,
            hashlib.sha256(state.request.objective.encode("utf-8")).hexdigest(),
            now,
        )
        self.store.record_decision(
            decision_id,
            state.task.task_id,
            manifest.requested_model,
            manifest.effort.value,
            recommendation.policy_version,
            "cli-1",
            now,
        )
        self.store.create_task_worktree_record(
            worktree_id=worktree_id,
            task_id=state.task.task_id,
            baseline_commit=commit,
            canonical_root_hash=hashlib.sha256(str(worktree.root).encode()).hexdigest(),
            created_at=now,
        )
        prepared = PreparedExecution(
            state.task.task_id,
            decision_id,
            attempt_id,
            worktree,
            manifest,
            prompt,
            state.graph_fingerprint,
            state.lifecycle_identity_digest,
        )
        self._prepared[attempt_id] = prepared
        return prepared

    @staticmethod
    def _manifest_hash(manifest: CliApprovalManifest) -> str:
        return hashlib.sha256(
            json.dumps(
                manifest.to_dict(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()

    def _validate(self, worktree: TaskWorktree) -> bool:
        runner = GitRunner(worktree.root)
        executable = runner.executable.resolve(strict=True)
        argv = (
            str(executable),
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"safe.directory={worktree.root}",
            "diff",
            "--check",
            worktree.baseline_commit,
            "--",
        )
        try:
            run_cli_process(
                argv=argv,
                cwd=worktree.root,
                stdin=b"",
                provider=ProviderId.CODEX,
                credential_home=None,
                timeout_seconds=60.0,
                max_input_bytes=1,
                max_output_bytes=256 * 1024,
            )
        except (CliProcessError, GitError, OSError):
            return False
        for command in self._validation_commands:
            try:
                run_cli_process(
                    argv=command,
                    cwd=worktree.root,
                    stdin=b"",
                    provider=ProviderId.CODEX,
                    credential_home=None,
                    timeout_seconds=300.0,
                    max_input_bytes=1,
                    max_output_bytes=1024 * 1024,
                )
            except CliProcessError:
                return False
        return True

    def run_approved(
        self, prepared: PreparedExecution, *, approval_granted: bool
    ) -> ApprovedExecution:
        if approval_granted is not True:
            raise RoutingServiceError("approval_required")
        current = self._prepared.get(prepared.attempt_id)
        if current is None or current != prepared:
            raise RoutingServiceError("transition_replay")
        manifest = prepared.manifest
        snapshot = self._snapshots.get(manifest.capability_snapshot_digest)
        if snapshot is None or snapshot.digest != manifest.capability_snapshot_digest:
            raise RoutingServiceError("capability_snapshot_missing")
        try:
            current_identity = self._identity_loader(manifest.provider)
        except (AdapterError, RoutingServiceError, OSError, ValueError):
            raise RoutingServiceError("cli_preflight_failed") from None
        if current_identity != snapshot.identity:
            raise RoutingServiceError("cli_identity_changed")
        live_runtime_identity: ProviderRuntimeIdentity | None = None
        if self._lifecycle_service is not None:
            boundary = self._lifecycle_boundaries.get(manifest.provider)
            if boundary is None or prepared.lifecycle_identity_digest is None or self._runtime_identity_loader is None:
                raise RoutingServiceError("lifecycle_authority_missing")
            try:
                live_runtime_identity = self._runtime_identity_loader(manifest.provider)
                self._lifecycle_service.require_snapshot_authority(
                    boundary_digest=boundary,
                    lifecycle_identity_digest=prepared.lifecycle_identity_digest,
                    capability_snapshot_digest=manifest.capability_snapshot_digest,
                    live_identity=live_runtime_identity,
                )
            except (LifecycleServiceError, ValueError):
                raise RoutingServiceError("lifecycle_identity_changed") from None
        self._prepared.pop(prepared.attempt_id, None)
        now = int(time.time())
        authority = ApprovalAuthority(
            self.store,
            key_path=self.state_dir / "approval.key",
            quota_path=self.state_dir / "quota.sqlite3",
        )
        if prepared.lifecycle_identity_digest is None:
            signed = authority.issue(manifest)
        else:
            signed = authority.issue(
                manifest,
                lifecycle_identity_digest=prepared.lifecycle_identity_digest,
                capability_snapshot_digest=manifest.capability_snapshot_digest,
                bound_at=now,
            )
        manifest_hash = self._manifest_hash(manifest)
        self.store.create_cli_execution_attempt_record(
            attempt_id=prepared.attempt_id,
            approval_id=manifest.approval_id,
            task_id=manifest.task_id,
            decision_id=manifest.decision_id,
            worktree_id=manifest.worktree_id,
            provider=manifest.provider.value,
            requested_model=manifest.requested_model,
            effective_model=manifest.effective_model,
            effort=manifest.effort.value,
            cli_version=manifest.cli_version,
            executable_sha256=manifest.cli_executable_sha256,
            capability_snapshot_digest=manifest.capability_snapshot_digest,
            manifest_hash=manifest_hash,
            expected_prompt_hash=prepared.prompt.prompt_hash,
            reserved_tokens=manifest.max_input_tokens + manifest.max_output_tokens,
            created_at=now,
        )
        if prepared.lifecycle_identity_digest is not None:
            try:
                self.store.save_lifecycle_attempt_binding(
                    attempt_id=prepared.attempt_id,
                    lifecycle_identity_digest=prepared.lifecycle_identity_digest,
                    bound_at=now,
                )
            except (StorageError, ValueError):
                self._fail(prepared, "lifecycle_binding_failed", quarantine=True)
                raise RoutingServiceError("lifecycle_binding_failed") from None
        try:
            if self._lifecycle_service is not None:
                assert live_runtime_identity is not None
                self._lifecycle_service.require_execution_authority(
                    boundary_digest=self._lifecycle_boundaries[manifest.provider],
                    lifecycle_identity_digest=prepared.lifecycle_identity_digest,
                    capability_snapshot_digest=manifest.capability_snapshot_digest,
                    approval_id=manifest.approval_id,
                    live_identity=live_runtime_identity,
                )
            authority.consume(
                signed,
                manifest,
                repository_quota_tokens=self.settings.repository_quota_tokens,
                machine_quota_tokens=self.settings.machine_quota_tokens,
                lifecycle_identity_digest=prepared.lifecycle_identity_digest,
                capability_snapshot_digest=(
                    manifest.capability_snapshot_digest
                    if prepared.lifecycle_identity_digest is not None
                    else None
                ),
            )
            executor = self._executors[manifest.provider]
            result = executor(
                executable=self._executable(manifest.provider),
                workspace=prepared.worktree.root,
                credential_home=self._credential_home(manifest.provider),
                prompt=prepared.prompt.body,
                requested_model=manifest.requested_model,
                expected_effective_model=manifest.effective_model,
                effort=manifest.effort,
                permission_mode=manifest.permission_mode,
            )
        except ApprovalError as exc:
            self._fail(prepared, exc.code, quarantine=True)
            raise RoutingServiceError(exc.code) from None
        except (AdapterError, KeyError) as exc:
            code = getattr(exc, "code", "executor_unavailable")
            self._fail(prepared, code, quarantine=True)
            raise RoutingServiceError(code) from None
        except Exception:
            self._fail(prepared, "executor_failed", quarantine=True)
            raise RoutingServiceError("executor_failed") from None
        if (
            not isinstance(result.message, str)
            or not result.message
            or len(result.message) > 1_048_576
            or result.effective_model != manifest.effective_model
            or isinstance(result.input_tokens, bool)
            or not isinstance(result.input_tokens, int)
            or not 0 <= result.input_tokens <= manifest.max_input_tokens
            or isinstance(result.output_tokens, bool)
            or not isinstance(result.output_tokens, int)
            or not 0 <= result.output_tokens <= manifest.max_output_tokens
            or isinstance(result.duration_seconds, bool)
            or not isinstance(result.duration_seconds, (int, float))
            or not math.isfinite(result.duration_seconds)
            or result.duration_seconds < 0
        ):
            self._fail(prepared, "executor_result_invalid", quarantine=True)
            raise RoutingServiceError("executor_result_invalid")
        try:
            evidence = collect_diff_evidence(
                prepared.worktree,
                max_files=self.settings.max_changed_files,
                max_bytes=self.settings.max_changed_bytes,
            )
        except (DiffPolicyError, WorktreeError) as exc:
            code = getattr(exc, "code", "diff_blocked")
            self._fail(prepared, code, quarantine=True)
            # Hand the diagnostic across explicitly. Both hops raise `from None`,
            # so anything not passed by hand dies here and the log shows a bare
            # code -- which is precisely what graphite#37's one sighting shows.
            # `_fail` still records the unwidened `code`: the taxonomy is stable,
            # the message is where the detail goes.
            raise RoutingServiceError(code, getattr(exc, "cause", None)) from None
        if prepared.attempt_id in self._review_primary and evidence.changed_files != 0:
            self._fail(prepared, "review_mutation", quarantine=True)
            raise RoutingServiceError("review_mutation")
        try:
            validation_outcome = (
                "passed" if self._validator(prepared.worktree) else "failed"
            )
        except Exception:
            self._fail(prepared, "validation_failed", quarantine=True)
            raise RoutingServiceError("validation_failed") from None
        completed_at = int(time.time())
        self.store.record_validation_result(
            attempt_id=prepared.attempt_id,
            diff_hash=evidence.diff_sha256,
            changed_file_count=evidence.changed_files,
            changed_byte_count=evidence.changed_bytes,
            outcome=validation_outcome,
            recorded_at=completed_at,
        )
        input_tokens = result.input_tokens
        output_tokens = result.output_tokens
        if input_tokens is None or output_tokens is None:
            self._fail(prepared, "usage_unavailable", quarantine=True)
            raise RoutingServiceError("usage_unavailable")
        receipt = ExecutionReceipt(
            execution_id="execution-" + secrets.token_hex(12),
            approval_id=manifest.approval_id,
            model_id=manifest.requested_model,
            effort=manifest.effort,
            outcome=ExecutionOutcome.SUCCEEDED,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=max(0, int(result.duration_seconds * 1_000)),
            prompt_hash=prepared.prompt.prompt_hash,
            response_hash=hashlib.sha256(result.message.encode("utf-8")).hexdigest(),
            failure_reason=None,
            provider=manifest.provider,
            effective_model=result.effective_model,
            cli_version=manifest.cli_version,
            changed_file_count=evidence.changed_files,
            changed_byte_count=evidence.changed_bytes,
            validation_outcome=validation_outcome,
        )
        self.store.finalize_cli_execution(
            attempt_id=prepared.attempt_id,
            receipt=receipt,
            graph_fingerprint=prepared.graph_fingerprint,
            completed_at=completed_at,
        )
        self.store.transition_task_worktree(
            manifest.worktree_id,
            expected_status="prepared",
            new_status="executed",
            updated_at=completed_at,
        )
        return ApprovedExecution(result.message, receipt, evidence.diff_sha256)

    def prepare_review(self, task_id: str) -> PreparedExecution:
        """Prepare a separate read-only other-provider review of a primary diff."""
        primary_worktree, record = self._load_task_worktree(task_id)
        if record["status"] != "executed":
            raise RoutingServiceError("review_primary_not_executed")
        primary = self.store.worktree_validation_record(primary_worktree.worktree_id)
        if primary is None or primary["outcome"] != "passed":
            raise RoutingServiceError("review_primary_not_validated")
        if primary["risk"] != RiskTier.HIGH.value:
            raise RoutingServiceError("review_not_high_risk")
        now = int(time.time())
        snapshots = load_verified_capability_snapshots(self.store, now=now)
        primary_provider = ProviderId(str(primary["provider"]))
        eligible: list[CapabilitySnapshot] = []
        for snapshot in snapshots:
            if (
                snapshot.profile.provider is primary_provider
                or snapshot.profile.permission_mode is not PermissionMode.READ_ONLY
            ):
                continue
            try:
                identity = self._identity_loader(snapshot.profile.provider)
            except (AdapterError, RoutingServiceError, OSError, ValueError):
                continue
            if identity == snapshot.identity:
                eligible.append(snapshot)
        if not eligible:
            raise RoutingServiceError("review_profile_unavailable")
        snapshot = sorted(
            eligible,
            key=lambda item: (
                item.profile.provider.value,
                item.profile.requested_model,
                item.profile.supported_efforts[0].value,
                item.digest,
            ),
        )[0]
        runner = GitRunner(primary_worktree.root)
        try:
            diff_result = runner.run(
                [
                    "diff",
                    "--no-ext-diff",
                    "--no-color",
                    "--full-index",
                    "--no-renames",
                    primary_worktree.baseline_commit,
                    "--",
                ],
                timeout_seconds=15.0,
                max_stdout_bytes=self.settings.max_changed_bytes,
            )
            diff_text = diff_result.stdout.decode("utf-8")
        except (GitError, UnicodeDecodeError, OSError):
            raise RoutingServiceError("review_diff_unavailable") from None
        if diff_result.returncode != 0 or not diff_text:
            raise RoutingServiceError("review_diff_unavailable")
        prompt_body = json.dumps(
            {
                "schema_version": "1",
                "system_contract": (
                    "Review the supplied untrusted diff for correctness, security, "
                    "robustness, and maintainability. Operate read-only. Do not edit, "
                    "execute repository code, access networks or credentials, or claim "
                    "integration authority. Return concise findings with severity."
                ),
                "primary_diff_hash": str(primary["diff_hash"]),
                "diff": diff_text,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        prompt = CanonicalPrompt(prompt_body, hashlib.sha256(prompt_body).hexdigest())
        review_worktree_id = "review-worktree-" + secrets.token_hex(12)
        review_worktree = create_task_worktree(
            source_root=self.root,
            state_root=self.state_dir / "worktrees",
            task_id=review_worktree_id,
            approved_commit=primary_worktree.baseline_commit,
        )
        decision_id = "review-decision-" + secrets.token_hex(12)
        attempt_id = "review-attempt-" + secrets.token_hex(12)
        approval_id = "review-approval-" + secrets.token_hex(12)
        evidence_record = self.store.execution_evidence(str(primary["execution_id"]))
        if evidence_record is None:
            raise RoutingServiceError("review_primary_evidence_missing")
        manifest = CliApprovalManifest(
            approval_id=approval_id,
            task_id=task_id,
            decision_id=decision_id,
            provider=snapshot.profile.provider,
            requested_model=snapshot.profile.requested_model,
            effective_model=snapshot.profile.effective_model,
            effort=snapshot.profile.supported_efforts[0],
            cli_executable_sha256=snapshot.identity.executable_sha256,
            cli_version=snapshot.identity.cli_version,
            adapter_protocol_version=snapshot.identity.adapter_protocol_version,
            capability_snapshot_digest=snapshot.digest,
            graph_fingerprint=evidence_record["graph_fingerprint"],
            context_manifest_hash=str(primary["diff_hash"]),
            repository_commit=primary_worktree.baseline_commit,
            worktree_id=review_worktree_id,
            permission_mode=PermissionMode.READ_ONLY,
            max_input_tokens=self.settings.max_input_tokens,
            max_output_tokens=self.settings.max_output_tokens,
            policy_version="review-1",
            issued_at=now,
            expires_at=now + self.settings.approval_ttl_seconds,
            nonce=secrets.token_hex(24),
        )
        self.store.record_decision(
            decision_id,
            task_id,
            manifest.requested_model,
            manifest.effort.value,
            "review-1",
            "cli-1",
            now,
        )
        self.store.create_task_worktree_record(
            worktree_id=review_worktree_id,
            task_id=task_id,
            baseline_commit=primary_worktree.baseline_commit,
            canonical_root_hash=hashlib.sha256(
                str(review_worktree.root).encode()
            ).hexdigest(),
            created_at=now,
        )
        prepared = PreparedExecution(
            task_id,
            decision_id,
            attempt_id,
            review_worktree,
            manifest,
            prompt,
            evidence_record["graph_fingerprint"],
        )
        self._prepared[attempt_id] = prepared
        self._snapshots[snapshot.digest] = snapshot
        self._review_primary[attempt_id] = (
            str(primary["attempt_id"]),
            str(primary["diff_hash"]),
        )
        return prepared

    def run_review_approved(
        self, prepared: PreparedExecution, *, approval_granted: bool
    ) -> ApprovedExecution:
        binding = self._review_primary.get(prepared.attempt_id)
        if binding is None:
            raise RoutingServiceError("review_binding_missing")
        result = self.run_approved(prepared, approval_granted=approval_granted)
        primary_attempt_id, primary_diff_hash = binding
        self.store.create_review_link_record(
            review_attempt_id=prepared.attempt_id,
            primary_attempt_id=primary_attempt_id,
            primary_diff_hash=primary_diff_hash,
            created_at=int(time.time()),
        )
        self._review_primary.pop(prepared.attempt_id, None)
        return result

    def decline(self, prepared: PreparedExecution) -> dict[str, str]:
        current = self._prepared.pop(prepared.attempt_id, None)
        if current is None or current != prepared:
            raise RoutingServiceError("transition_replay")
        self.store.transition_task_worktree(
            prepared.worktree.worktree_id,
            expected_status="prepared",
            new_status="quarantined",
            updated_at=int(time.time()),
        )
        return {
            "task_id": prepared.task_id,
            "worktree_id": prepared.worktree.worktree_id,
            "status": "quarantined",
            "reason": "approval_declined",
        }

    def _fail(
        self, prepared: PreparedExecution, reason: str, *, quarantine: bool
    ) -> None:
        now = int(time.time())
        try:
            self.store.mark_cli_execution_attempt(
                prepared.attempt_id,
                status="quarantined" if quarantine else "failed",
                failure_reason=reason,
                updated_at=now,
            )
            if quarantine:
                self.store.transition_task_worktree(
                    prepared.worktree.worktree_id,
                    expected_status="prepared",
                    new_status="quarantined",
                    updated_at=now,
                )
        except (StorageError, ValueError, OSError):
            raise RoutingServiceError("execution_persistence_failed") from None

    def _load_task_worktree(self, task_id: str) -> tuple[TaskWorktree, dict[str, Any]]:
        self.store.initialize()
        record = self.store.latest_task_worktree_record(task_id)
        if record is None:
            raise RoutingServiceError("worktree_missing")
        worktree_id = str(record["worktree_id"])
        candidate = self.state_dir / "worktrees" / "tasks" / worktree_id
        try:
            root = candidate.resolve(strict=True)
        except OSError:
            raise RoutingServiceError("worktree_missing") from None
        if hashlib.sha256(str(root).encode()).hexdigest() != record["canonical_root_hash"]:
            raise RoutingServiceError("worktree_identity_drift")
        try:
            runner = GitRunner(root)
            common_result = runner.run(
                ["rev-parse", "--git-common-dir"],
                timeout_seconds=5.0,
                max_stdout_bytes=4_096,
            )
            raw_common = common_result.stdout.decode("utf-8").strip()
            common_candidate = Path(raw_common)
            if not common_candidate.is_absolute():
                common_candidate = root / common_candidate
            common = common_candidate.resolve(strict=True)
        except (GitError, UnicodeDecodeError, OSError):
            raise RoutingServiceError("worktree_identity_drift") from None
        if common_result.returncode != 0 or common != (self.root / ".git").resolve(strict=True):
            raise RoutingServiceError("worktree_identity_drift")
        return (
            TaskWorktree(
                worktree_id,
                root,
                common,
                str(record["baseline_commit"]),
                str(record["status"]),
            ),
            record,
        )

    def accept(self, task_id: str, *, authority_granted: bool) -> dict[str, str]:
        """Create a detached, cherry-pickable integration commit; never merge it."""
        if authority_granted is not True:
            raise RoutingServiceError("accept_authority_required")
        worktree, record = self._load_task_worktree(task_id)
        if record["status"] == "accepted":
            raise RoutingServiceError("transition_replay")
        if record["status"] != "executed":
            raise RoutingServiceError("worktree_transition_invalid")
        validation = self.store.worktree_validation_record(worktree.worktree_id)
        if validation is None or validation["outcome"] != "passed":
            raise RoutingServiceError("validation_not_passed")
        evidence = collect_diff_evidence(
            TaskWorktree(
                worktree.worktree_id,
                worktree.root,
                worktree.git_common_dir,
                worktree.baseline_commit,
                "prepared",
            ),
            max_files=self.settings.max_changed_files,
            max_bytes=self.settings.max_changed_bytes,
        )
        if evidence.diff_sha256 != validation["diff_hash"]:
            raise RoutingServiceError("diff_changed_after_validation")
        if evidence.changed_files == 0:
            raise RoutingServiceError("empty_diff")
        runner = GitRunner(worktree.root)
        try:
            add = runner.run(
                ["add", "--all", "--"], timeout_seconds=15.0, max_stdout_bytes=4_096
            )
            commit = runner.run(
                [
                    "-c",
                    "user.name=Graphite Router",
                    "-c",
                    "user.email=graphite-router@localhost",
                    "commit",
                    "--no-gpg-sign",
                    "-m",
                    f"graphite: accepted task {task_id}",
                ],
                timeout_seconds=30.0,
                max_stdout_bytes=256 * 1024,
            )
            head = runner.run(
                ["rev-parse", "HEAD"], timeout_seconds=5.0, max_stdout_bytes=256
            )
            commit_id = head.stdout.decode("ascii").strip()
        except (GitError, UnicodeDecodeError, OSError):
            raise RoutingServiceError("integration_commit_failed") from None
        if add.returncode != 0 or commit.returncode != 0 or head.returncode != 0:
            raise RoutingServiceError("integration_commit_failed")
        self.store.transition_task_worktree(
            worktree.worktree_id,
            expected_status="executed",
            new_status="accepted",
            updated_at=int(time.time()),
        )
        self.store.record_outcome(
            "outcome-" + secrets.token_hex(12),
            str(validation["execution_id"]),
            "human",
            True,
            False,
            int(time.time()),
        )
        return {
            "task_id": task_id,
            "worktree_id": worktree.worktree_id,
            "status": "accepted",
            "commit_id": commit_id,
            "integration": "explicit_cherry_pick_required",
        }

    def reject(self, task_id: str, *, authority_granted: bool) -> dict[str, str]:
        if authority_granted is not True:
            raise RoutingServiceError("reject_authority_required")
        worktree, record = self._load_task_worktree(task_id)
        if record["status"] == "rejected":
            raise RoutingServiceError("transition_replay")
        if record["status"] != "executed":
            raise RoutingServiceError("worktree_transition_invalid")
        validation = self.store.worktree_validation_record(worktree.worktree_id)
        if validation is None or validation["execution_id"] is None:
            raise RoutingServiceError("execution_evidence_missing")
        self.store.transition_task_worktree(
            worktree.worktree_id,
            expected_status="executed",
            new_status="rejected",
            updated_at=int(time.time()),
        )
        self.store.record_outcome(
            "outcome-" + secrets.token_hex(12),
            str(validation["execution_id"]),
            "human",
            False,
            False,
            int(time.time()),
        )
        return {
            "task_id": task_id,
            "worktree_id": worktree.worktree_id,
            "status": "rejected",
            "cleanup": "separate_explicit_authority_required",
        }

    def cleanup(self, task_id: str, *, authority_granted: bool) -> dict[str, str]:
        if authority_granted is not True:
            raise RoutingServiceError("cleanup_authority_required")
        worktree, record = self._load_task_worktree(task_id)
        status = str(record["status"])
        if status == "cleaned":
            raise RoutingServiceError("transition_replay")
        if status not in {"accepted", "rejected", "quarantined"}:
            raise RoutingServiceError("worktree_transition_invalid")
        cleanup_task_worktree(
            worktree,
            state_root=self.state_dir / "worktrees",
            task_id=worktree.worktree_id,
            terminal_status=status,
            authority_granted=True,
        )
        self.store.transition_task_worktree(
            worktree.worktree_id,
            expected_status=status,
            new_status="cleaned",
            updated_at=int(time.time()),
        )
        return {
            "task_id": task_id,
            "worktree_id": worktree.worktree_id,
            "status": "cleaned",
        }

    def status(self) -> dict[str, Any]:
        try:
            self.store.initialize()
            integrity = self.store.integrity_check()
            snapshots = load_verified_capability_snapshots(
                self.store, now=int(time.time())
            )
        except (StorageError, OSError, ValueError):
            integrity = "unavailable"
            snapshots = ()
        return {
            "routing": "ready" if integrity == "ok" and snapshots else "not_ready",
            "storage": integrity,
            "verified_profiles": len(snapshots),
            "providers": sorted({item.profile.provider.value for item in snapshots}),
            "authority": "single_use_approval_required",
            "automatic_execution": False,
            "automatic_retry": False,
            "automatic_fallback": False,
            "automatic_merge": False,
        }

    def recoverable_attempts(
        self, *, limit: int = DEFAULT_RECOVERY_PAGE_SIZE, after: str | None = None
    ) -> RecoverableAttemptPage:
        self.store.initialize()
        return self.store.recoverable_attempts(limit=limit, after=after)

    def reconcile_execution(self, attempt_id: str) -> dict[str, Any]:
        self.store.initialize()
        receipt = self.store.reconcile_execution_attempt(
            attempt_id, completed_at=int(time.time())
        )
        return dict(receipt.to_dict())

    def policy(
        self,
        *,
        promote: str | None = None,
        rollback: str | None = None,
        authority_granted: bool = False,
    ) -> dict[str, Any]:
        if promote is not None and rollback is not None:
            raise RoutingServiceError("policy_action_conflict")
        if promote is not None or rollback is not None:
            self.store.initialize()
            selected = promote or rollback
            assert selected is not None
            try:
                self.store.activate_cli_policy(
                    selected,
                    action="promote" if promote else "rollback",
                    authority_granted=authority_granted,
                    created_at=int(time.time()),
                )
            except ValueError as exc:
                raise RoutingServiceError(str(exc)) from exc
        return {
            "policy_version": promote or rollback or "3",
            "requested_action": (
                "promote" if promote else "rollback" if rollback else "inspect"
            ),
            "execution_authority": "single_use_approval_required",
            "automatic_execution": False,
        }

    def record_outcome(self, **values: Any) -> dict[str, Any]:
        if values.get("provenance") != "human":
            raise RoutingServiceError("supported_evidence_import_required")
        return {
            "recorded": True,
            "provenance": "human",
            "autonomy_admissible": False,
        }
