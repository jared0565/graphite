"""Deterministic task classification from requests and bounded graph evidence."""
from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import Any, Final

from .contracts import RiskTier, TaskCategory, TaskProfile, TaskRequest

MAX_IMPACT_NODES: Final = 10_000

_CATEGORY_RULES: tuple[tuple[TaskCategory, tuple[str, ...]], ...] = (
    (TaskCategory.TENANT_ISOLATION, ("tenant isolation", "cross-tenant", "multi-tenant")),
    (TaskCategory.AUTHORIZATION, ("authorization", "authorisation", "permission", "role access")),
    (TaskCategory.AUTHENTICATION, ("authentication", "login", "sign in", "oauth", "session")),
    (TaskCategory.MIGRATION, ("database migration", "schema migration", "rollback migration")),
    (TaskCategory.DEPLOYMENT, ("deployment", "deploy", "release pipeline", "production release")),
    (TaskCategory.INFRASTRUCTURE, ("infrastructure", "terraform", "kubernetes", "cloud provision")),
    (TaskCategory.CONCURRENCY, ("concurrent", "concurrency", "race condition", "deadlock", "transaction race")),
    (TaskCategory.FINANCIAL, ("financial", "money", "mortgage", "payment", "billing", "invoice")),
    (TaskCategory.LEGAL, ("legal", "contract clause", "compliance policy")),
    (TaskCategory.ARCHITECTURE, ("architecture", "system design", "service boundary")),
    (TaskCategory.REFACTOR, ("refactor", "restructure", "extract module")),
    (TaskCategory.FEATURE, ("feature", "add a", "implement", "customer search", "endpoint")),
    (TaskCategory.ISOLATED_CODE, ("isolated", "formatting helper", "small helper", "typo fix")),
    (TaskCategory.DOCUMENTATION, ("documentation", "readme", "guide", "docs")),
)

_CATEGORY_RISK: Final[dict[TaskCategory, RiskTier]] = {
    TaskCategory.DOCUMENTATION: RiskTier.LOW,
    TaskCategory.ISOLATED_CODE: RiskTier.LOW,
    TaskCategory.FEATURE: RiskTier.MEDIUM,
    TaskCategory.REFACTOR: RiskTier.MEDIUM,
    TaskCategory.ARCHITECTURE: RiskTier.HIGH,
    TaskCategory.AUTHENTICATION: RiskTier.HIGH,
    TaskCategory.AUTHORIZATION: RiskTier.HIGH,
    TaskCategory.TENANT_ISOLATION: RiskTier.HIGH,
    TaskCategory.MIGRATION: RiskTier.HIGH,
    TaskCategory.DEPLOYMENT: RiskTier.HIGH,
    TaskCategory.INFRASTRUCTURE: RiskTier.HIGH,
    TaskCategory.CONCURRENCY: RiskTier.HIGH,
    TaskCategory.FINANCIAL: RiskTier.HIGH,
    TaskCategory.LEGAL: RiskTier.HIGH,
    TaskCategory.UNKNOWN: RiskTier.HIGH,
}
_RISK_ORDER = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2}


def _detect_category(objective: str) -> TaskCategory:
    normalized = " ".join(objective.casefold().split())
    for category, keywords in _CATEGORY_RULES:
        if any(keyword in normalized for keyword in keywords):
            return category
    return TaskCategory.UNKNOWN


def _target_nodes(graph: Any, targets: tuple[str, ...]) -> list[str]:
    selected: list[str] = []
    target_set = set(targets)
    try:
        nodes = graph.nodes(data=True)
    except (AttributeError, TypeError):
        return []
    for node_id, data in nodes:
        if isinstance(data, dict) and data.get("source_file") in target_set:
            selected.append(str(node_id))
    return sorted(set(selected))


def _bounded_impact(graph: Any, starts: list[str]) -> tuple[set[str], bool]:
    visited = set(starts)
    queue = deque(starts)
    truncated = False
    while queue:
        node = queue.popleft()
        try:
            neighbors = set(graph.predecessors(node)) | set(graph.successors(node))
        except (AttributeError, KeyError):
            continue
        for neighbor in sorted(neighbors, key=str):
            normalized = str(neighbor)
            if normalized in visited:
                continue
            if len(visited) >= MAX_IMPACT_NODES:
                truncated = True
                return visited, truncated
            visited.add(normalized)
            queue.append(normalized)
    return visited, truncated


def classify_task(request: TaskRequest, graph: Any) -> TaskProfile:
    """Classify a request using fixed rules and bounded graph features."""
    detected = _detect_category(request.objective)
    category = detected
    detected_risk = _CATEGORY_RISK[detected]
    if request.category_hint is not None:
        hint_risk = _CATEGORY_RISK[request.category_hint]
        if _RISK_ORDER[hint_risk] > _RISK_ORDER[detected_risk]:
            category = request.category_hint
    risk = _CATEGORY_RISK[category]

    starts = _target_nodes(graph, request.targets)
    impacted, truncated = _bounded_impact(graph, starts)
    flags: list[str] = []
    communities: set[object] = set()
    tests_nearby = False
    for node in sorted(impacted):
        try:
            data = graph.nodes[node]
        except (AttributeError, KeyError):
            continue
        source_file = data.get("source_file") if isinstance(data, dict) else None
        if isinstance(source_file, str) and (
            source_file.startswith("tests/")
            or ".test." in source_file
            or ".spec." in source_file
            or source_file.endswith("_test.py")
        ):
            tests_nearby = True
        if isinstance(data, dict) and data.get("community") is not None:
            communities.add(data["community"])
    if tests_nearby:
        flags.append("tests_nearby")
    if len(communities) > 1:
        flags.append("community_crossing")
    if len(impacted) >= 25 or truncated:
        flags.append("broad_impact")
        if risk is RiskTier.LOW:
            risk = RiskTier.MEDIUM
    if detected is TaskCategory.UNKNOWN:
        flags.append("unknown_scope")
    if risk is RiskTier.HIGH:
        flags.append("high_risk_category")

    verification = ["tests"]
    if risk is RiskTier.HIGH:
        verification.extend(("security_review", "manual_frontier_review"))
    if category is TaskCategory.MIGRATION:
        verification.append("rollback_test")
    complexity = min(
        10,
        max(1, 1 + len(request.targets) + len(impacted) // 5 + _RISK_ORDER[risk] * 2),
    )
    task_material = json.dumps(
        {
            "objective": request.objective,
            "targets": list(request.targets),
            "category": category.value,
            "risk": risk.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    task_id = "task-" + hashlib.sha256(task_material).hexdigest()[:24]
    context_requirements = ["explicit_targets"]
    if impacted:
        context_requirements.append("dependency_neighbors")
    return TaskProfile(
        task_id=task_id,
        category=category,
        risk=risk,
        complexity=complexity,
        impact_radius=len(impacted),
        risk_flags=tuple(flags),
        context_requirements=tuple(context_requirements),
        verification_requirements=tuple(verification),
    )
