"""Routing orchestration boundary; CLI code never imports provider transport."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphite.config import Config
from graphite.freshness import check_graph_freshness
from graphite.graph_io import GraphReadError, load_validated_graph_bundle

from .approval import ApprovalAuthority
from .classifier import classify_task
from .context_builder import ContextBundle, build_routing_context
from .contracts import ApprovalManifest, Effort, ExecutionReceipt, TaskRequest
from .ollama_executor import execute_ollama
from .policy import CandidateMetrics, PolicyGates, rank_candidates
from .registry import (
    BUNDLED_PROFILES,
    RegistryError,
    RegistrySnapshot,
    load_cached_registry,
    refresh_model_inventory,
)
from .settings import RoutingSettings
from .storage import RepositoryStore, StorageError


@dataclass(frozen=True)
class RoutingRecommendation:
    task_id: str
    model_id: str | None
    effort: str | None
    risk: str
    estimated_tokens: int
    outbound_manifest: dict[str, Any]
    reasons: tuple[str, ...]
    recommended_channels: tuple[str, ...]
    manual_handoff: bool
    policy_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "model_id": self.model_id,
            "effort": self.effort,
            "risk": self.risk,
            "estimated_tokens": self.estimated_tokens,
            "outbound_manifest": self.outbound_manifest,
            "reasons": list(self.reasons),
            "recommended_channels": list(self.recommended_channels),
            "manual_handoff": self.manual_handoff,
            "policy_version": self.policy_version,
            "execution_authority": "single_use_approval_required",
        }


@dataclass(frozen=True)
class ApprovedExecution:
    """Ephemeral provider text paired with its persistence-safe receipt."""

    text: str
    receipt: ExecutionReceipt

    def to_public_dict(self) -> dict[str, Any]:
        return dict(self.receipt.to_dict())


@dataclass(frozen=True)
class _Prepared:
    request: TaskRequest
    task: Any
    context: ContextBundle
    snapshot: RegistrySnapshot
    graph_fingerprint: str
    recommendation: RoutingRecommendation


def _machine_state_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "Graphite" / "routing"
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "state") / "graphite" / "routing"


class RoutingService:
    """Coordinates deterministic recommendation and explicitly approved execution."""

    def __init__(self, path: str | Path, *, state_dir: Path | None = None) -> None:
        self.root = Path(path).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("repository_root_invalid")
        self.settings = RoutingSettings.from_env()
        self.store = RepositoryStore(self.root)
        self.state_dir = (state_dir or _machine_state_dir()).resolve(strict=False)
        self._prepared: dict[str, _Prepared] = {}

    def recommend(self, *, objective: str, targets: tuple[str, ...]) -> RoutingRecommendation:
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
        graph_path = self.root / cfg.output_dir / "graph.json"
        try:
            bundle, graph = load_validated_graph_bundle(graph_path, root=self.root)
            task = classify_task(request, graph)
            context = build_routing_context(request, graph, self.settings)
            snapshot = load_cached_registry(self.store, now=int(time.time()))
        except (GraphReadError, RegistryError, StorageError, OSError, ValueError) as exc:
            code = getattr(exc, "code", "routing_evidence_blocked")
            return self._handoff("unclassified", "unknown", str(code))
        context_tokens = max(1, (context.manifest.total_bytes + 3) // 4)
        candidates = tuple(
            CandidateMetrics(
                model_id=model.model_id,
                effort=Effort.DEFAULT,
                repository_success_millis=None,
                global_success_millis=500,
                expected_input_tokens=context_tokens,
                expected_output_tokens=request.max_output_tokens,
                expected_latency_ms=30_000,
                retry_rate_millis=0,
                escalation_rate_millis=0,
                quota_scarcity_millis=0,
            )
            for model in snapshot.models
            if model.model_id in BUNDLED_PROFILES
        )
        ranked = rank_candidates(
            task,
            snapshot,
            candidates,
            PolicyGates(
                graph_valid=True,
                graph_fresh=True,
                registry_fresh=True,
                data_policy_allowed=True,
                storage_available=True,
                task_evaluated=False,
                context_tokens=context_tokens,
                budget_tokens=min(
                    self.settings.repository_quota_tokens,
                    request.max_input_tokens + request.max_output_tokens,
                ),
                current_date=time.strftime("%Y-%m-%d", time.gmtime()),
            ),
        )
        selected = ranked.selected
        graph_fingerprint = hashlib.sha256(
            json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        recommendation = RoutingRecommendation(
            task_id=task.task_id,
            model_id=None if selected is None else selected.model_id,
            effort=None if selected is None else selected.effort.value,
            risk=task.risk.value,
            estimated_tokens=context_tokens + request.max_output_tokens,
            outbound_manifest=context.manifest.to_dict(),
            reasons=ranked.reasons,
            recommended_channels=ranked.recommended_channels,
            manual_handoff=ranked.manual_handoff,
            policy_version=ranked.policy_version,
        )
        self._prepared[task.task_id] = _Prepared(
            request, task, context, snapshot, graph_fingerprint, recommendation
        )
        return recommendation

    def _handoff(self, task_id: str, risk: str, reason: str) -> RoutingRecommendation:
        return RoutingRecommendation(
            task_id, None, None, risk, 0, {"items": [], "total_bytes": 0},
            (reason,), ("claude_code", "codex"), True, "1",
        )

    def execute_approved(self, recommendation: RoutingRecommendation) -> ApprovedExecution:
        if recommendation.manual_handoff or recommendation.model_id is None:
            raise ValueError("manual_handoff_required")
        prepared = self._prepared.pop(recommendation.task_id, None)
        if prepared is None or prepared.recommendation != recommendation:
            raise ValueError("recommendation_expired")
        self.store.initialize()
        now = int(time.time())
        decision_id = "decision-" + secrets.token_hex(12)
        approval_id = "approval-" + secrets.token_hex(12)
        self.store.record_task(
            prepared.task.task_id,
            prepared.task.category.value,
            prepared.task.risk.value,
            hashlib.sha256(prepared.request.objective.encode("utf-8")).hexdigest(),
            now,
        )
        self.store.record_decision(
            decision_id, prepared.task.task_id, recommendation.model_id,
            recommendation.effort, recommendation.policy_version, "1", now,
        )
        manifest = ApprovalManifest(
            approval_id=approval_id,
            task_id=prepared.task.task_id,
            decision_id=decision_id,
            graph_fingerprint=prepared.graph_fingerprint,
            context_manifest_hash=prepared.context.manifest.manifest_hash,
            model_id=recommendation.model_id,
            effort=Effort(recommendation.effort),
            max_input_tokens=prepared.request.max_input_tokens,
            max_output_tokens=prepared.request.max_output_tokens,
            policy_version=recommendation.policy_version,
            issued_at=now,
            expires_at=now + self.settings.approval_ttl_seconds,
            nonce=secrets.token_hex(24),
        )
        authority = ApprovalAuthority(
            self.store,
            key_path=self.state_dir / "approval.key",
            quota_path=self.state_dir / "quota.sqlite3",
        )
        signed = authority.issue(manifest)
        inventory = next(
            model for model in prepared.snapshot.models if model.model_id == recommendation.model_id
        )
        result = execute_ollama(
            authority=authority,
            signed_approval=signed,
            current_manifest=manifest,
            context=prepared.context,
            objective=prepared.request.objective,
            expected_digest=inventory.digest,
            settings=self.settings,
        )
        receipt = result.receipt
        self.store.insert_execution(
            execution_id=receipt.execution_id,
            idempotency_key=receipt.execution_id,
            task_id=prepared.task.task_id,
            decision_id=decision_id,
            approval_id=approval_id,
            model_id=receipt.model_id,
            effort=receipt.effort.value,
            status=receipt.outcome.value,
            reserved_tokens=manifest.max_input_tokens + manifest.max_output_tokens,
            created_at=now,
        )
        self.store.link_execution_evidence(
            receipt.execution_id, prepared.task.task_id, decision_id,
            prepared.graph_fingerprint,
        )
        return ApprovedExecution(text=result.text, receipt=receipt)

    def status(self) -> dict[str, Any]:
        try:
            integrity = self.store.integrity_check()
        except (StorageError, OSError):
            integrity = "unavailable"
        return {
            "routing": "ready" if integrity == "ok" else "not_initialized",
            "storage": integrity,
            "authority": "single_use_approval_required",
            "automatic_execution": False,
        }

    def policy(
        self,
        *,
        refresh_models: bool = False,
        promote: str | None = None,
        rollback: str | None = None,
    ) -> dict[str, Any]:
        if refresh_models:
            self.store.initialize()
            refresh_model_inventory(self.store, now=int(time.time()))
        return {
            "policy_version": promote or rollback or "1",
            "requested_action": "promote" if promote else "rollback" if rollback else "inspect",
            "execution_authority": "single_use_approval_required",
            "automatic_execution": False,
        }

    def record_outcome(self, **values: Any) -> dict[str, Any]:
        # Human evidence is intentionally retained separately and never upgraded here.
        if values.get("provenance") != "human":
            raise ValueError("supported_evidence_import_required")
        return {"recorded": True, "provenance": "human", "autonomy_admissible": False}
