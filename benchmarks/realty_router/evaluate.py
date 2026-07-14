"""Offline-first evaluator for versioned Realty routing result records."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from graphite.routing.policy import wilson_lower_bound

_TASK_FIELDS = frozenset({
    "task_id", "category", "risk", "objective", "targets", "allowed_evidence",
    "verification", "max_context_bytes",
})
_RESULT_FIELDS = frozenset({
    "task_id", "policy", "model_id", "profile_version", "policy_version",
    "accepted", "repair_count", "escalated", "severe_failure", "latency_ms",
    "cost_usd_equivalent_micros",
})
_CATEGORIES = frozenset({
    "documentation", "isolated_code", "feature", "refactor", "architecture",
    "authentication", "authorization", "tenant_isolation", "migration",
    "deployment", "infrastructure", "concurrency", "financial", "legal", "unknown",
})
_POLICIES = frozenset({"frontier_only", "native_automatic", "graphite_routed"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MUTABLE_ALIASES = frozenset({"latest", "default", "stable", "auto"})


def _relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not value.startswith("/") and all(part not in {"", ".", ".."} for part in path.parts)


def load_tasks(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("corpus_invalid") from exc
    if not isinstance(data, dict) or set(data) != {"schema_version", "corpus", "tasks"}:
        raise ValueError("corpus_fields_invalid")
    if data["schema_version"] != "1" or data["corpus"] != "synthetic-realty-router":
        raise ValueError("corpus_version_invalid")
    tasks = data["tasks"]
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 100:
        raise ValueError("corpus_tasks_invalid")
    identifiers: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) != _TASK_FIELDS:
            raise ValueError("task_fields_invalid")
        task_id = task["task_id"]
        if not isinstance(task_id, str) or not _IDENTIFIER.fullmatch(task_id) or task_id in identifiers:
            raise ValueError("task_id_invalid")
        identifiers.add(task_id)
        if task["category"] not in _CATEGORIES or task["risk"] not in {"low", "medium", "high"}:
            raise ValueError("task_label_invalid")
        if not isinstance(task["objective"], str) or not 1 <= len(task["objective"]) <= 4096:
            raise ValueError("task_objective_invalid")
        if not isinstance(task["targets"], list) or not task["targets"] or not all(_relative_path(item) for item in task["targets"]):
            raise ValueError("task_targets_invalid")
        for field in ("allowed_evidence", "verification"):
            values = task[field]
            if not isinstance(values, list) or not values or not all(isinstance(item, str) and _IDENTIFIER.fullmatch(item) for item in values):
                raise ValueError(f"task_{field}_invalid")
        maximum = task["max_context_bytes"]
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 16_384 <= maximum <= 262_144:
            raise ValueError("task_context_invalid")
    return data


def _nonnegative(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 10**12:
        raise ValueError(code)
    return value


def evaluate_records(corpus: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    task_ids = {task["task_id"] for task in corpus["tasks"]}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != _RESULT_FIELDS:
            raise ValueError("result_fields_invalid")
        if record["task_id"] not in task_ids or record["policy"] not in _POLICIES:
            raise ValueError("result_identity_invalid")
        model = record["model_id"]
        if not isinstance(model, str) or not _IDENTIFIER.fullmatch(model) or model.casefold() in _MUTABLE_ALIASES:
            raise ValueError("result_model_invalid")
        for field in ("profile_version", "policy_version"):
            if not isinstance(record[field], str) or not _IDENTIFIER.fullmatch(record[field]):
                raise ValueError("result_version_invalid")
        for field in ("accepted", "escalated", "severe_failure"):
            if not isinstance(record[field], bool):
                raise ValueError("result_boolean_invalid")
        for field in ("repair_count", "latency_ms", "cost_usd_equivalent_micros"):
            _nonnegative(record[field], f"result_{field}_invalid")
        grouped.setdefault(record["policy"], []).append(record)
    policies: dict[str, Any] = {}
    for policy, values in sorted(grouped.items()):
        accepted = sum(item["accepted"] for item in values)
        policies[policy] = {
            "sample_count": len(values),
            "accepted_count": accepted,
            "acceptance_wilson_lower_95": wilson_lower_bound(accepted, len(values)),
            "repair_count": sum(item["repair_count"] for item in values),
            "escalation_count": sum(item["escalated"] for item in values),
            "severe_failure_count": sum(item["severe_failure"] for item in values),
            "latency_ms": sum(item["latency_ms"] for item in values),
            "cost_usd_equivalent_micros": sum(item["cost_usd_equivalent_micros"] for item in values),
        }
    sufficient = len(records) >= 100 and all(len(values) >= 30 for values in grouped.values())
    return {
        "schema_version": "1",
        "total_samples": len(records),
        "policies": policies,
        "causal_claim": "descriptive_only" if sufficient else "insufficient_evidence",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=Path, default=Path(__file__).with_name("tasks.json"))
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--approve-cost", action="store_true")
    args = parser.parse_args(argv)
    if args.live:
        if not args.approve_cost:
            parser.error("--live requires --approve-cost")
        parser.error("live runs must be initiated through the approval-gated routing service")
    corpus = load_tasks(args.tasks)
    records = json.loads(args.results.read_text(encoding="utf-8"))
    print(json.dumps(evaluate_records(corpus, records), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
