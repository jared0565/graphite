"""Tests for `graphite.routing.route_pool_execution`, the single-use coordinator.

Named after the module so aramid's mutation stage 1 (`tests/test_<module>.py`)
has something to run; `-k route_pool_execution` selected nothing before this
file. `test_route_pool.py` drives the coordinator end to end through signed
approvals; this file pins the units underneath it -- the evidence record's
validation, the error mapping around the loader and the sink, and result
validation against the selection's remaining budget -- with plain data.
"""
from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from graphite.routing.route_pool import RouteAttemptEvidence, RoutePoolError, SideEffectState
from graphite.routing.route_pool_execution import (
    RouteAttemptFailure,
    RouteExecutionEvidence,
    RouteExecutionResult,
    _failure_evidence,
    _load_authority,
    _persist_evidence,
    _validate_result,
    execute_approved_route_pool,
)

_DIGEST = "ab" * 32


def _evidence(**changes: object) -> RouteExecutionEvidence:
    values: dict[str, object] = {
        "candidate_id": "candidate-1",
        "candidate_digest": _DIGEST,
        "attempt_ordinal": 1,
        "outcome_category": "succeeded",
        "input_tokens": 10,
        "output_tokens": 5,
        "duration_ms": 1_000,
        "cost_microunits": None,
    }
    values.update(changes)
    return RouteExecutionEvidence(**values)  # type: ignore[arg-type]


def _attempt(**changes: object) -> RouteAttemptEvidence:
    values: dict[str, object] = {
        "candidate_id": "candidate-1",
        "candidate_digest": _DIGEST,
        "attempt_ordinal": 1,
        "failure_category": "capacity_unavailable",
        "accepted_output": False,
        "side_effect_state": SideEffectState.NONE,
        "input_tokens": 3,
        "output_tokens": 0,
        "duration_ms": 250,
        "cost_microunits": None,
    }
    values.update(changes)
    return RouteAttemptEvidence(**values)  # type: ignore[arg-type]


def _selection(**changes: object) -> SimpleNamespace:
    """The fields of `RouteSelection` the validators read, as plain data."""
    values: dict[str, object] = {
        "candidate": SimpleNamespace(candidate_id="candidate-1", digest=_DIGEST),
        "attempt_ordinal": 1,
        "remaining_input_tokens": 100,
        "remaining_output_tokens": 50,
        "remaining_duration_ms": 5_000,
        "remaining_cost_microunits": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _result(**changes: object) -> RouteExecutionResult:
    values: dict[str, object] = {
        "candidate_id": "candidate-1",
        "candidate_digest": _DIGEST,
        "attempt_ordinal": 1,
        "output": "private output",
        "input_tokens": 100,
        "output_tokens": 50,
        "duration_ms": 5_000,
        "cost_microunits": None,
    }
    values.update(changes)
    return RouteExecutionResult(**values)  # type: ignore[arg-type]


# --- RouteExecutionEvidence ---------------------------------------------------------


def test_evidence_publishes_exactly_its_declared_fields() -> None:
    evidence = _evidence(cost_microunits=7)
    assert evidence.to_dict() == {
        "candidate_id": "candidate-1",
        "candidate_digest": _DIGEST,
        "attempt_ordinal": 1,
        "outcome_category": "succeeded",
        "input_tokens": 10,
        "output_tokens": 5,
        "duration_ms": 1_000,
        "cost_microunits": 7,
    }


@pytest.mark.parametrize(
    "category",
    [
        "succeeded",
        "capacity_unavailable",
        "provider_process_failure",
        "provider_unavailable",
        "provider_protocol",
        "timeout",
        "cancelled",
    ],
)
def test_evidence_accepts_every_allowlisted_outcome(category: str) -> None:
    assert _evidence(outcome_category=category).outcome_category == category


def test_evidence_accepts_the_budget_ceilings_exactly() -> None:
    evidence = _evidence(
        attempt_ordinal=2,
        input_tokens=262_144,
        output_tokens=32_768,
        duration_ms=86_400_000,
        cost_microunits=10**12,
    )
    assert (evidence.input_tokens, evidence.output_tokens, evidence.duration_ms) == (
        262_144,
        32_768,
        86_400_000,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"candidate_id": ""},
        {"candidate_id": "has space"},
        {"candidate_id": "nul\x00byte"},
        {"candidate_id": "x" * 257},
        {"candidate_id": 5},
        {"candidate_digest": "ab" * 31},
        {"candidate_digest": "zz" * 32},
        {"candidate_digest": 12},
        {"outcome_category": "unknown"},
        {"attempt_ordinal": 0},
        {"attempt_ordinal": 3},
        {"attempt_ordinal": True},
        {"attempt_ordinal": "1"},
        {"input_tokens": -1},
        {"input_tokens": 262_145},
        {"input_tokens": True},
        {"output_tokens": 32_769},
        {"output_tokens": 1.5},
        {"duration_ms": 86_400_001},
        {"duration_ms": False},
        {"cost_microunits": -1},
        {"cost_microunits": 10**12 + 1},
        {"cost_microunits": True},
        {"cost_microunits": "0"},
    ],
    ids=lambda changes: next(iter(changes)) + "=" + repr(next(iter(changes.values())))[:12],
)
def test_evidence_rejects_anything_outside_its_contract(changes: dict[str, object]) -> None:
    with pytest.raises(RoutePoolError, match="route_evidence_invalid"):
        _evidence(**changes)


# --- RouteAttemptFailure --------------------------------------------------------------


def test_attempt_failure_carries_its_evidence_and_names_only_the_category() -> None:
    attempt = _attempt()
    failure = RouteAttemptFailure(attempt)
    assert failure.evidence is attempt
    assert str(failure) == "capacity_unavailable"


def test_attempt_failure_refuses_anything_but_attempt_evidence() -> None:
    with pytest.raises(RoutePoolError, match="route_attempt_invalid"):
        RouteAttemptFailure({"failure_category": "timeout"})  # type: ignore[arg-type]


# --- the loader and the sink ------------------------------------------------------------


def test_load_authority_returns_the_loaded_tuple() -> None:
    authorities = ("a", "b")
    assert _load_authority(lambda: authorities) is authorities  # type: ignore[arg-type,return-value]


def test_load_authority_maps_a_raising_loader_to_unavailable() -> None:
    def loader() -> tuple[object, ...]:
        raise RuntimeError("store is locked")

    with pytest.raises(RoutePoolError, match="route_authority_unavailable") as info:
        _load_authority(loader)  # type: ignore[arg-type]
    assert info.value.__cause__ is None  # the store's error text never rides along


def test_load_authority_rejects_a_non_tuple_result() -> None:
    with pytest.raises(RoutePoolError, match="route_authority_invalid"):
        _load_authority(lambda: ["not", "a", "tuple"])  # type: ignore[arg-type,return-value]


def test_persist_evidence_hands_the_record_to_the_sink() -> None:
    seen: list[RouteExecutionEvidence] = []
    evidence = _evidence()
    _persist_evidence(seen.append, evidence)
    assert seen == [evidence]


def test_persist_evidence_maps_a_failing_sink_to_unavailable() -> None:
    def sink(evidence: RouteExecutionEvidence) -> None:
        raise OSError("disk full")

    with pytest.raises(RoutePoolError, match="route_evidence_unavailable") as info:
        _persist_evidence(sink, _evidence())
    assert info.value.__cause__ is None


# --- _failure_evidence ---------------------------------------------------------------------


def test_failure_evidence_copies_the_attempt_into_execution_evidence() -> None:
    evidence = _failure_evidence(_selection(), _attempt())  # type: ignore[arg-type]
    assert evidence == _evidence(
        outcome_category="capacity_unavailable", input_tokens=3, output_tokens=0, duration_ms=250
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"candidate_id": "candidate-2"},
        {"candidate_digest": "cd" * 32},
        {"attempt_ordinal": 2},
    ],
    ids=["other-candidate", "other-digest", "other-attempt"],
)
def test_failure_evidence_rejects_an_attempt_for_a_different_selection(changes: dict[str, object]) -> None:
    with pytest.raises(RoutePoolError, match="route_attempt_invalid"):
        _failure_evidence(_selection(), _attempt(**changes))  # type: ignore[arg-type]


# --- _validate_result ------------------------------------------------------------------------


def test_validate_result_books_a_success_that_fits_the_remaining_budget() -> None:
    pool = SimpleNamespace(max_cost_microunits=None)
    evidence = _validate_result(pool, _selection(), _result())  # type: ignore[arg-type]
    assert evidence == _evidence(input_tokens=100, output_tokens=50, duration_ms=5_000)
    assert "output" not in evidence.to_dict()


def test_validate_result_books_a_cost_when_the_pool_meters_one() -> None:
    pool = SimpleNamespace(max_cost_microunits=1_000)
    selection = _selection(remaining_cost_microunits=400)
    evidence = _validate_result(pool, selection, _result(cost_microunits=400))  # type: ignore[arg-type]
    assert evidence.cost_microunits == 400


@pytest.mark.parametrize(
    "changes",
    [
        {"candidate_id": "candidate-2"},
        {"candidate_digest": "cd" * 32},
        {"attempt_ordinal": 2},
        {"output": ""},
        {"output": "nul\x00"},
        {"output": "x" * 1_048_577},
        {"output": b"bytes"},
    ],
    ids=["other-candidate", "other-digest", "other-attempt", "empty", "nul", "too-long", "not-text"],
)
def test_validate_result_rejects_a_result_for_another_selection_or_unusable_output(
    changes: dict[str, object],
) -> None:
    pool = SimpleNamespace(max_cost_microunits=None)
    with pytest.raises(RoutePoolError, match="route_result_invalid"):
        _validate_result(pool, _selection(), _result(**changes))  # type: ignore[arg-type]


def test_validate_result_rejects_a_result_that_is_not_the_result_type() -> None:
    pool = SimpleNamespace(max_cost_microunits=None)
    with pytest.raises(RoutePoolError, match="route_result_invalid"):
        _validate_result(pool, _selection(), SimpleNamespace(**asdict(_result())))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"input_tokens": 101},
        {"output_tokens": 51},
        {"duration_ms": 5_001},
        {"input_tokens": -1},
        {"input_tokens": True},
        {"duration_ms": 1.0},
    ],
    ids=["input-over", "output-over", "duration-over", "negative", "bool", "float"],
)
def test_validate_result_rejects_usage_beyond_the_remaining_budget(changes: dict[str, object]) -> None:
    pool = SimpleNamespace(max_cost_microunits=None)
    with pytest.raises(RoutePoolError, match="route_pool_budget_exhausted"):
        _validate_result(pool, _selection(), _result(**changes))  # type: ignore[arg-type]


def test_validate_result_rejects_a_cost_on_an_unmetered_pool() -> None:
    pool = SimpleNamespace(max_cost_microunits=None)
    with pytest.raises(RoutePoolError, match="route_pool_cost_invalid"):
        _validate_result(pool, _selection(), _result(cost_microunits=0))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("remaining", "cost"),
    [
        (400, None),
        (400, 401),
        (400, -1),
        (400, True),
        (None, 1),
    ],
    ids=["missing-cost", "over-remaining", "negative", "bool", "no-remaining"],
)
def test_validate_result_rejects_a_cost_the_metered_pool_cannot_cover(
    remaining: int | None, cost: object
) -> None:
    pool = SimpleNamespace(max_cost_microunits=1_000)
    selection = _selection(remaining_cost_microunits=remaining)
    with pytest.raises(RoutePoolError, match="route_pool_budget_exhausted"):
        _validate_result(pool, selection, _result(cost_microunits=cost))  # type: ignore[arg-type]


# --- execute_approved_route_pool: argument validation ---------------------------------------


def test_execute_rejects_bad_collaborators_before_consuming_anything() -> None:
    consumed: list[object] = []

    class _Authority:
        def consume(self, *args: object, **kwargs: object) -> None:
            consumed.append(args)

    with pytest.raises(RoutePoolError, match="route_execution_invalid"):
        execute_approved_route_pool(
            pool=SimpleNamespace(),  # type: ignore[arg-type]
            signed_approval=SimpleNamespace(),  # type: ignore[arg-type]
            approval_authority=_Authority(),  # type: ignore[arg-type]
            authority_loader=lambda: (),
            runner=lambda selection: _result(),
            evidence_sink=lambda evidence: None,
            repository_quota_tokens=1,
            machine_quota_tokens=1,
            now=lambda: 0,
        )
    assert consumed == []
