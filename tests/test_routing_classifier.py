"""Deterministic task category, complexity, impact, and risk tests."""
from __future__ import annotations

from pathlib import Path

import networkx as nx
import pytest

from graphite.routing.classifier import classify_task
from graphite.routing.contracts import RiskTier, TaskCategory, TaskRequest


def _request(tmp_path: Path, objective: str, *, hint=None, targets=("src/app.py",)) -> TaskRequest:
    return TaskRequest(
        objective=objective,
        repository_root=tmp_path,
        targets=targets,
        max_input_tokens=8_000,
        max_output_tokens=2_000,
        data_policy="source_allowed",
        category_hint=hint,
    )


def _graph() -> nx.DiGraph:
    graph = nx.DiGraph()
    graph.add_node("app", source_file="src/app.py", kind="file")
    graph.add_node("service", source_file="src/service.py", kind="file")
    graph.add_node("test", source_file="tests/test_app.py", kind="file")
    graph.add_edge("service", "app", relation="imports")
    graph.add_edge("test", "app", relation="imports")
    return graph


@pytest.mark.parametrize(
    ("objective", "category", "risk"),
    [
        ("Update the README installation guide", TaskCategory.DOCUMENTATION, RiskTier.LOW),
        ("Fix an isolated formatting helper", TaskCategory.ISOLATED_CODE, RiskTier.LOW),
        ("Add a customer search feature", TaskCategory.FEATURE, RiskTier.MEDIUM),
        ("Refactor the repository service", TaskCategory.REFACTOR, RiskTier.MEDIUM),
        ("Design the service architecture", TaskCategory.ARCHITECTURE, RiskTier.HIGH),
        ("Change login authentication", TaskCategory.AUTHENTICATION, RiskTier.HIGH),
        ("Fix role authorization", TaskCategory.AUTHORIZATION, RiskTier.HIGH),
        ("Enforce tenant isolation", TaskCategory.TENANT_ISOLATION, RiskTier.HIGH),
        ("Create a database migration", TaskCategory.MIGRATION, RiskTier.HIGH),
        ("Change production deployment", TaskCategory.DEPLOYMENT, RiskTier.HIGH),
        ("Provision cloud infrastructure", TaskCategory.INFRASTRUCTURE, RiskTier.HIGH),
        ("Fix a concurrent transaction race", TaskCategory.CONCURRENCY, RiskTier.HIGH),
        ("Calculate mortgage payment money", TaskCategory.FINANCIAL, RiskTier.HIGH),
        ("Generate a legal contract clause", TaskCategory.LEGAL, RiskTier.HIGH),
        ("Do the thing", TaskCategory.UNKNOWN, RiskTier.HIGH),
    ],
)
def test_classifier_uses_fixed_conservative_rules(
    tmp_path: Path,
    objective: str,
    category: TaskCategory,
    risk: RiskTier,
) -> None:
    profile = classify_task(_request(tmp_path, objective), _graph())

    assert profile.category is category
    assert profile.risk is risk


def test_category_hint_can_raise_but_never_lower_risk(tmp_path: Path) -> None:
    raised = classify_task(
        _request(tmp_path, "Update documentation", hint=TaskCategory.AUTHORIZATION),
        _graph(),
    )
    not_lowered = classify_task(
        _request(tmp_path, "Enforce tenant isolation", hint=TaskCategory.DOCUMENTATION),
        _graph(),
    )

    assert raised.category is TaskCategory.AUTHORIZATION
    assert raised.risk is RiskTier.HIGH
    assert not_lowered.category is TaskCategory.TENANT_ISOLATION
    assert not_lowered.risk is RiskTier.HIGH


def test_classifier_impact_is_bounded_deterministic_and_includes_test_proximity(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, "Add a customer search feature")

    first = classify_task(request, _graph())
    second = classify_task(request, _graph())

    assert first == second
    assert first.impact_radius == 3
    assert "tests_nearby" in first.risk_flags
    assert "tests" in first.verification_requirements


def test_conflicting_high_risk_evidence_selects_safer_category(tmp_path: Path) -> None:
    profile = classify_task(
        _request(tmp_path, "Update docs and change authentication database migration"),
        _graph(),
    )

    assert profile.risk is RiskTier.HIGH
    assert profile.category in {TaskCategory.AUTHENTICATION, TaskCategory.MIGRATION}
    assert "manual_frontier_review" in profile.verification_requirements
