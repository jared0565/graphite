"""Schema and offline evaluator tests for the Realty routing benchmark."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.realty_router.evaluate import evaluate_records, load_tasks


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "benchmarks" / "realty_router" / "tasks.json"


def test_task_corpus_is_versioned_bounded_and_non_proprietary() -> None:
    corpus = load_tasks(TASKS)
    assert corpus["schema_version"] == "1"
    assert len(corpus["tasks"]) >= 12
    ids = [task["task_id"] for task in corpus["tasks"]]
    assert len(ids) == len(set(ids))
    serialized = json.dumps(corpus).casefold()
    for forbidden in ("api_key", "password=", "c:\\", "/home/", "mls listing data"):
        assert forbidden not in serialized
    assert {task["risk"] for task in corpus["tasks"]} >= {"low", "medium", "high"}
    assert all(task["verification"] for task in corpus["tasks"])
    assert all(16_384 <= task["max_context_bytes"] <= 262_144 for task in corpus["tasks"])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["tasks"].append(dict(data["tasks"][0])),
        lambda data: data["tasks"][0].update({"unknown": True}),
        lambda data: data["tasks"][0].update({"verification": []}),
        lambda data: data["tasks"][0].update({"model_id": "latest"}),
        lambda data: data["tasks"][0].update({"targets": ["C:\\secret.py"]}),
    ],
)
def test_loader_rejects_invalid_or_mutable_corpus(tmp_path: Path, mutation) -> None:
    data = json.loads(TASKS.read_text(encoding="utf-8"))
    mutation(data)
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_tasks(path)


def test_offline_evaluator_reports_samples_cost_latency_quality_and_uncertainty() -> None:
    tasks = load_tasks(TASKS)
    records = [
        {
            "task_id": tasks["tasks"][0]["task_id"],
            "policy": "graphite_routed",
            "model_id": "kimi-k2.7-code:cloud",
            "profile_version": "2026-07-14.1",
            "policy_version": "1",
            "accepted": True,
            "repair_count": 0,
            "escalated": False,
            "severe_failure": False,
            "latency_ms": 1200,
            "cost_usd_equivalent_micros": 250,
        },
        {
            "task_id": tasks["tasks"][1]["task_id"],
            "policy": "frontier_only",
            "model_id": "frontier-exact-version",
            "profile_version": "vendor-version",
            "policy_version": "native",
            "accepted": False,
            "repair_count": 2,
            "escalated": True,
            "severe_failure": False,
            "latency_ms": 2400,
            "cost_usd_equivalent_micros": 1000,
        },
    ]
    report = evaluate_records(tasks, records)
    assert report["total_samples"] == 2
    assert set(report["policies"]) == {"frontier_only", "graphite_routed"}
    routed = report["policies"]["graphite_routed"]
    assert routed["sample_count"] == 1
    assert routed["accepted_count"] == 1
    assert routed["cost_usd_equivalent_micros"] == 250
    assert "acceptance_wilson_lower_95" in routed
    assert report["causal_claim"] == "insufficient_evidence"


def test_evaluator_rejects_unknown_tasks_fields_and_mutable_model_aliases() -> None:
    tasks = load_tasks(TASKS)
    base = {
        "task_id": tasks["tasks"][0]["task_id"], "policy": "graphite_routed",
        "model_id": "latest", "profile_version": "1", "policy_version": "1",
        "accepted": True, "repair_count": 0, "escalated": False,
        "severe_failure": False, "latency_ms": 1,
        "cost_usd_equivalent_micros": 1,
    }
    with pytest.raises(ValueError, match="result_model_invalid"):
        evaluate_records(tasks, [base])
    with pytest.raises(ValueError, match="result_fields_invalid"):
        evaluate_records(tasks, [{**base, "model_id": "exact:1", "prompt": "secret"}])
