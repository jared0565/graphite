"""Governed provider-neutral route-pool authority tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from graphite.routing.approval import ApprovalAuthority, ApprovalError
from graphite.routing.contracts import Effort, PermissionMode, RiskTier
from graphite.routing.lifecycle import (
    LifecycleProviderId,
    ProviderLifecycleState,
    RuntimeKind,
)
from graphite.routing.route_pool import (
    ApprovedRouteCandidate,
    ApprovedRoutePool,
    RouteAttemptEvidence,
    RouteAuthority,
    RoutePoolError,
    SideEffectState,
    select_route,
)
from graphite.routing.route_pool_execution import (
    RouteAttemptFailure,
    RouteExecutionEvidence,
    RouteExecutionResult,
    execute_approved_route_pool,
)
from graphite.routing.storage import RepositoryStore


_RUNTIME = {
    LifecycleProviderId.CLAUDE_CODE: RuntimeKind.LOCAL_CLI,
    LifecycleProviderId.CODEX: RuntimeKind.LOCAL_CLI,
    LifecycleProviderId.OLLAMA: RuntimeKind.LOCAL_HTTP,
    LifecycleProviderId.OPENROUTER: RuntimeKind.REMOTE_HTTPS,
    LifecycleProviderId.ZAI: RuntimeKind.REMOTE_HTTPS,
}


def _candidate(
    provider: LifecycleProviderId,
    index: int,
    **changes: object,
) -> ApprovedRouteCandidate:
    values: dict[str, object] = {
        "candidate_id": f"candidate-{index}",
        "provider": provider,
        "runtime_kind": _RUNTIME[provider],
        "lifecycle_identity_digest": f"{index}" * 64,
        "capability_snapshot_digest": f"{index + 1}" * 64,
        "model_identity_digest": f"{index + 2}" * 64,
        "routing_policy_digest": "f" * 64
        if provider is LifecycleProviderId.OPENROUTER
        else None,
        "requested_model": f"model-{index}",
        "effective_model": f"effective-{index}",
        "effort": Effort.HIGH,
        "permission_mode": PermissionMode.READ_ONLY,
        "risk_ceiling": RiskTier.HIGH,
        "trust_policy_digest": "a" * 64,
        "capabilities": ("architecture", "structured-output"),
        "context_window_tokens": 100_000,
        "snapshot_expires_at": 500,
    }
    values.update(changes)
    return ApprovedRouteCandidate(**values)


def _pool(**changes: object) -> ApprovedRoutePool:
    values: dict[str, object] = {
        "approval_id": "approval-pool-1",
        "task_id": "task-1",
        "decision_id": "decision-1",
        "candidates": (
            _candidate(LifecycleProviderId.CLAUDE_CODE, 1),
            _candidate(LifecycleProviderId.CODEX, 2),
        ),
        "required_capabilities": ("architecture",),
        "task_risk": RiskTier.HIGH,
        "permission_mode": PermissionMode.READ_ONLY,
        "trust_policy_digest": "a" * 64,
        "graph_fingerprint": "b" * 64,
        "context_manifest_hash": "c" * 64,
        "repository_commit": "d" * 40,
        "worktree_id": "worktree-1",
        "allow_cross_provider": True,
        "allowed_fallback_reasons": ("capacity_unavailable",),
        "max_attempts": 2,
        "max_input_tokens": 20_000,
        "max_output_tokens": 4_000,
        "max_duration_ms": 120_000,
        "max_cost_microunits": None,
        "policy_version": "route-pool-1",
        "issued_at": 100,
        "expires_at": 200,
        "nonce": "pool-nonce-1",
    }
    values.update(changes)
    return ApprovedRoutePool(**values)


def _authority(candidate: ApprovedRouteCandidate, **changes: object) -> RouteAuthority:
    values: dict[str, object] = {
        "candidate_id": candidate.candidate_id,
        "provider": candidate.provider,
        "runtime_kind": candidate.runtime_kind,
        "lifecycle_identity_digest": candidate.lifecycle_identity_digest,
        "capability_snapshot_digest": candidate.capability_snapshot_digest,
        "state": ProviderLifecycleState.ACTIVE,
    }
    values.update(changes)
    return RouteAuthority(**values)


def _authorities(pool: ApprovedRoutePool) -> tuple[RouteAuthority, ...]:
    return tuple(_authority(candidate) for candidate in pool.candidates)


def _capacity_attempt(pool: ApprovedRoutePool, **changes: object) -> RouteAttemptEvidence:
    values: dict[str, object] = {
        "candidate_id": pool.candidates[0].candidate_id,
        "candidate_digest": pool.candidates[0].digest,
        "attempt_ordinal": 1,
        "failure_category": "capacity_unavailable",
        "accepted_output": False,
        "side_effect_state": SideEffectState.NONE,
        "input_tokens": 0,
        "output_tokens": 0,
        "duration_ms": 1_000,
        "cost_microunits": None,
    }
    values.update(changes)
    return RouteAttemptEvidence(**values)


@pytest.mark.parametrize(
    "provider",
    # route_pool.py's own _RUNTIME_BY_PROVIDER map does not include ZAI.
    # Pool registration for zai is out of scope for this spec (the z.ai
    # native-provider verification plan) and is deferred to a future spec —
    # no task in this plan reverses this exclusion.
    # Exclude it here rather than exercising an unsupported provider.
    tuple(p for p in LifecycleProviderId if p is not LifecycleProviderId.ZAI),
)
def test_every_provider_can_be_an_exact_preapproved_candidate(
    provider: LifecycleProviderId,
) -> None:
    candidate = _candidate(provider, 1)
    pool = _pool(candidates=(candidate,), max_attempts=1, allow_cross_provider=False,
                 allowed_fallback_reasons=())

    selection = select_route(pool, (_authority(candidate),), (), now=150)

    assert selection.candidate == candidate
    assert selection.attempt_ordinal == 1


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (LifecycleProviderId.CLAUDE_CODE, LifecycleProviderId.CODEX),
        (LifecycleProviderId.OLLAMA, LifecycleProviderId.OPENROUTER),
    ],
)
def test_capacity_failure_selects_one_preapproved_cross_provider_fallback(
    first: LifecycleProviderId,
    second: LifecycleProviderId,
) -> None:
    pool = _pool(candidates=(_candidate(first, 1), _candidate(second, 2)))
    selection = select_route(
        pool,
        _authorities(pool),
        (_capacity_attempt(pool),),
        now=150,
    )

    assert selection.candidate == pool.candidates[1]
    assert selection.attempt_ordinal == 2
    assert selection.remaining_input_tokens == pool.max_input_tokens
    assert selection.remaining_duration_ms == 119_000


@pytest.mark.parametrize(
    ("attempt_changes", "code"),
    [
        ({"failure_category": "provider_process_failure"}, "fallback_reason_denied"),
        ({"accepted_output": True}, "fallback_output_accepted"),
        ({"side_effect_state": SideEffectState.OBSERVED}, "fallback_side_effects"),
        ({"side_effect_state": SideEffectState.UNKNOWN}, "fallback_side_effects"),
        ({"input_tokens": 20_000}, "route_pool_budget_exhausted"),
        ({"output_tokens": 4_000}, "route_pool_budget_exhausted"),
        ({"duration_ms": 120_000}, "route_pool_budget_exhausted"),
    ],
)
def test_fallback_denials_are_fail_closed(
    attempt_changes: dict[str, object], code: str
) -> None:
    pool = _pool()
    with pytest.raises(RoutePoolError, match=f"^{code}$"):
        select_route(
            pool,
            _authorities(pool),
            (_capacity_attempt(pool, **attempt_changes),),
            now=150,
        )


@pytest.mark.parametrize(
    ("authority_changes", "code"),
    [
        ({"state": ProviderLifecycleState.UNAVAILABLE}, "route_inactive"),
        ({"lifecycle_identity_digest": "e" * 64}, "route_identity_changed"),
        ({"capability_snapshot_digest": "e" * 64}, "route_snapshot_changed"),
    ],
)
def test_current_authority_must_match_exactly(
    authority_changes: dict[str, object], code: str
) -> None:
    pool = _pool()
    authorities = (
        _authority(pool.candidates[0]),
        _authority(pool.candidates[1], **authority_changes),
    )
    with pytest.raises(RoutePoolError, match=f"^{code}$"):
        select_route(pool, authorities, (_capacity_attempt(pool),), now=150)


def test_cross_provider_fallback_requires_explicit_authority() -> None:
    pool = _pool(allow_cross_provider=False)
    with pytest.raises(RoutePoolError, match="^cross_provider_denied$"):
        select_route(pool, _authorities(pool), (_capacity_attempt(pool),), now=150)


def test_cross_provider_pool_constructs_when_fallback_disallowed() -> None:
    # Intended design: a cross-provider pool CAN be constructed even with
    # allow_cross_provider=False; the cross-provider gate is enforced at
    # select_route (see test_cross_provider_fallback_requires_explicit_authority),
    # not at construction. This pins that behavior against re-introducing a
    # construction-time guard.
    pool = _pool(allow_cross_provider=False)
    assert pool.allow_cross_provider is False
    assert len({candidate.provider for candidate in pool.candidates}) == 2


def test_reordered_dynamic_duplicate_and_expired_authority_is_rejected() -> None:
    pool = _pool()
    with pytest.raises(RoutePoolError, match="^route_authority_invalid$"):
        select_route(pool, tuple(reversed(_authorities(pool))), (), now=150)
    with pytest.raises(RoutePoolError, match="^route_authority_invalid$"):
        select_route(pool, _authorities(pool) + (_authority(pool.candidates[0]),), (), now=150)
    with pytest.raises(RoutePoolError, match="^route_pool_expired$"):
        select_route(pool, _authorities(pool), (), now=200)


def test_pool_validation_rejects_more_than_one_fallback_and_unsafe_bindings() -> None:
    with pytest.raises(RoutePoolError, match="^route_candidates_invalid$"):
        _pool(candidates=(*_pool().candidates, _candidate(LifecycleProviderId.OLLAMA, 3)))
    with pytest.raises(RoutePoolError, match="^route_candidate_invalid$"):
        _candidate(LifecycleProviderId.OPENROUTER, 1, routing_policy_digest=None)
    with pytest.raises(RoutePoolError, match="^route_pool_invalid$"):
        _pool(required_capabilities=("missing",))
    for candidate in (
        _candidate(
            LifecycleProviderId.CLAUDE_CODE,
            1,
            permission_mode=PermissionMode.WORKSPACE_WRITE,
        ),
        _candidate(
            LifecycleProviderId.CLAUDE_CODE,
            1,
            trust_policy_digest="e" * 64,
        ),
        _candidate(
            LifecycleProviderId.CLAUDE_CODE,
            1,
            risk_ceiling=RiskTier.LOW,
        ),
    ):
        with pytest.raises(RoutePoolError, match="^route_pool_invalid$"):
            _pool(
                candidates=(candidate,),
                max_attempts=1,
                allow_cross_provider=False,
                allowed_fallback_reasons=(),
            )
    with pytest.raises(RoutePoolError, match="^route_pool_invalid$"):
        _pool(
            candidates=(
                _candidate(
                    LifecycleProviderId.CLAUDE_CODE,
                    1,
                    context_window_tokens=20_000,
                ),
            ),
            max_attempts=1,
            allow_cross_provider=False,
            allowed_fallback_reasons=(),
        )
    with pytest.raises(RoutePoolError, match="^route_pool_invalid$"):
        _pool(
            candidates=(
                _candidate(
                    LifecycleProviderId.CLAUDE_CODE,
                    1,
                    snapshot_expires_at=199,
                ),
            ),
            max_attempts=1,
            allow_cross_provider=False,
            allowed_fallback_reasons=(),
        )


def test_signed_pool_is_one_aggregate_single_use_quota_authority(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    authority = ApprovalAuthority(
        store,
        key_path=tmp_path / "machine" / "approval.key",
        quota_path=tmp_path / "machine" / "quota.sqlite3",
        now=lambda: 150,
    )
    pool = _pool()
    signed = authority.issue(pool)

    authority.consume(
        signed,
        pool,
        repository_quota_tokens=30_000,
        machine_quota_tokens=30_000,
    )

    assert store.approval_status(pool.approval_id) == "consumed"
    assert authority.machine_reserved_token_total() == 24_000
    with pytest.raises(ApprovalError, match="^approval_reused$"):
        authority.consume(
            signed,
            pool,
            repository_quota_tokens=30_000,
            machine_quota_tokens=30_000,
        )


def test_signed_pool_rejects_candidate_reordering_or_limit_changes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    authority = ApprovalAuthority(
        store,
        key_path=tmp_path / "machine" / "approval.key",
        quota_path=tmp_path / "machine" / "quota.sqlite3",
        now=lambda: 150,
    )
    pool = _pool()
    signed = authority.issue(pool)

    with pytest.raises(ApprovalError, match="^approval_manifest_changed$"):
        authority.verify(signed, replace(pool, candidates=tuple(reversed(pool.candidates))))
    with pytest.raises(ApprovalError, match="^approval_manifest_changed$"):
        authority.verify(signed, replace(pool, max_duration_ms=119_999))


def test_concurrent_pool_consumers_cannot_split_or_replay_authority(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    authority = ApprovalAuthority(
        store,
        key_path=tmp_path / "machine" / "approval.key",
        quota_path=tmp_path / "machine" / "quota.sqlite3",
        now=lambda: 150,
    )
    pool = _pool()
    signed = authority.issue(pool)

    def consume() -> str:
        try:
            authority.consume(
                signed,
                pool,
                repository_quota_tokens=50_000,
                machine_quota_tokens=50_000,
            )
            return "consumed"
        except ApprovalError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(lambda _index: consume(), range(2)))
    assert results == ["approval_reused", "consumed"]


def test_pool_public_records_never_accept_raw_diagnostics() -> None:
    forbidden = {
        "stdout",
        "stderr",
        "prompt",
        "response",
        "credential",
        "path",
        "diagnostic",
    }
    records = (_pool(), *_pool().candidates, _capacity_attempt(_pool()))
    for record in records:
        assert forbidden.isdisjoint(record.to_dict())


def _approval_authority(tmp_path: Path) -> tuple[RepositoryStore, ApprovalAuthority]:
    root = tmp_path / "repo"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    return store, ApprovalAuthority(
        store,
        key_path=tmp_path / "machine" / "approval.key",
        quota_path=tmp_path / "machine" / "quota.sqlite3",
        now=lambda: 150,
    )


def test_coordinator_automatically_selects_cross_provider_capacity_fallback(
    tmp_path: Path,
) -> None:
    store, approval_authority = _approval_authority(tmp_path)
    pool = _pool()
    signed = approval_authority.issue(pool)
    invoked: list[str] = []
    persisted: list[RouteExecutionEvidence] = []

    def loader() -> tuple[RouteAuthority, ...]:
        return _authorities(pool)

    def runner(selection) -> RouteExecutionResult:
        invoked.append(selection.candidate.candidate_id)
        if selection.attempt_ordinal == 1:
            raise RouteAttemptFailure(_capacity_attempt(pool))
        return RouteExecutionResult(
            candidate_id=selection.candidate.candidate_id,
            candidate_digest=selection.candidate.digest,
            attempt_ordinal=2,
            output="ephemeral private model output",
            input_tokens=10_000,
            output_tokens=500,
            duration_ms=20_000,
            cost_microunits=None,
        )

    result = execute_approved_route_pool(
        pool=pool,
        signed_approval=signed,
        approval_authority=approval_authority,
        authority_loader=loader,
        runner=runner,
        evidence_sink=persisted.append,
        repository_quota_tokens=30_000,
        machine_quota_tokens=30_000,
        now=lambda: 150,
    )

    assert result.output == "ephemeral private model output"
    assert repr(result).find("ephemeral") == -1
    assert invoked == ["candidate-1", "candidate-2"]
    assert [item.outcome_category for item in persisted] == [
        "capacity_unavailable",
        "succeeded",
    ]
    assert all("output" not in item.to_dict() for item in persisted)
    assert store.approval_status(pool.approval_id) == "consumed"


def test_coordinator_never_falls_back_after_side_effect_or_unknown_failure(
    tmp_path: Path,
) -> None:
    _store, approval_authority = _approval_authority(tmp_path)
    pool = _pool()
    signed = approval_authority.issue(pool)
    invoked = 0

    def runner(selection) -> RouteExecutionResult:
        nonlocal invoked
        invoked += 1
        raise RouteAttemptFailure(
            _capacity_attempt(pool, side_effect_state=SideEffectState.UNKNOWN)
        )

    with pytest.raises(RoutePoolError, match="^fallback_side_effects$"):
        execute_approved_route_pool(
            pool=pool,
            signed_approval=signed,
            approval_authority=approval_authority,
            authority_loader=lambda: _authorities(pool),
            runner=runner,
            evidence_sink=lambda evidence: None,
            repository_quota_tokens=30_000,
            machine_quota_tokens=30_000,
            now=lambda: 150,
        )
    assert invoked == 1


def test_coordinator_rechecks_authority_before_fallback(tmp_path: Path) -> None:
    _store, approval_authority = _approval_authority(tmp_path)
    pool = _pool()
    signed = approval_authority.issue(pool)
    loads = 0

    def loader() -> tuple[RouteAuthority, ...]:
        nonlocal loads
        loads += 1
        if loads == 1:
            return _authorities(pool)
        return (
            _authority(pool.candidates[0]),
            _authority(
                pool.candidates[1],
                state=ProviderLifecycleState.UNAVAILABLE,
            ),
        )

    def runner(selection) -> RouteExecutionResult:
        raise RouteAttemptFailure(_capacity_attempt(pool))

    with pytest.raises(RoutePoolError, match="^route_inactive$"):
        execute_approved_route_pool(
            pool=pool,
            signed_approval=signed,
            approval_authority=approval_authority,
            authority_loader=loader,
            runner=runner,
            evidence_sink=lambda evidence: None,
            repository_quota_tokens=30_000,
            machine_quota_tokens=30_000,
            now=lambda: 150,
        )
    assert loads == 2


def test_evidence_sink_failure_stops_before_fallback(tmp_path: Path) -> None:
    _store, approval_authority = _approval_authority(tmp_path)
    pool = _pool()
    signed = approval_authority.issue(pool)
    invoked = 0

    def runner(selection) -> RouteExecutionResult:
        nonlocal invoked
        invoked += 1
        raise RouteAttemptFailure(_capacity_attempt(pool))

    def sink(evidence: RouteExecutionEvidence) -> None:
        raise OSError("PRIVATE persistence diagnostic")

    with pytest.raises(RoutePoolError, match="^route_evidence_unavailable$") as caught:
        execute_approved_route_pool(
            pool=pool,
            signed_approval=signed,
            approval_authority=approval_authority,
            authority_loader=lambda: _authorities(pool),
            runner=runner,
            evidence_sink=sink,
            repository_quota_tokens=30_000,
            machine_quota_tokens=30_000,
            now=lambda: 150,
        )
    assert invoked == 1
    assert caught.value.__cause__ is None


def test_unknown_runner_failure_stops_without_fallback_or_raw_diagnostic(
    tmp_path: Path,
) -> None:
    _store, approval_authority = _approval_authority(tmp_path)
    pool = _pool()
    signed = approval_authority.issue(pool)
    invoked = 0

    def runner(selection) -> RouteExecutionResult:
        nonlocal invoked
        invoked += 1
        raise RuntimeError("PRIVATE provider diagnostic")

    with pytest.raises(RoutePoolError, match="^route_attempt_failed$") as caught:
        execute_approved_route_pool(
            pool=pool,
            signed_approval=signed,
            approval_authority=approval_authority,
            authority_loader=lambda: _authorities(pool),
            runner=runner,
            evidence_sink=lambda evidence: None,
            repository_quota_tokens=30_000,
            machine_quota_tokens=30_000,
            now=lambda: 150,
        )
    assert invoked == 1
    assert "PRIVATE" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_success_result_cannot_exceed_remaining_aggregate_budget(tmp_path: Path) -> None:
    _store, approval_authority = _approval_authority(tmp_path)
    pool = _pool()
    signed = approval_authority.issue(pool)
    persisted: list[RouteExecutionEvidence] = []

    def runner(selection) -> RouteExecutionResult:
        return RouteExecutionResult(
            candidate_id=selection.candidate.candidate_id,
            candidate_digest=selection.candidate.digest,
            attempt_ordinal=1,
            output="private",
            input_tokens=pool.max_input_tokens + 1,
            output_tokens=0,
            duration_ms=1,
            cost_microunits=None,
        )

    with pytest.raises(RoutePoolError, match="^route_pool_budget_exhausted$"):
        execute_approved_route_pool(
            pool=pool,
            signed_approval=signed,
            approval_authority=approval_authority,
            authority_loader=lambda: _authorities(pool),
            runner=runner,
            evidence_sink=persisted.append,
            repository_quota_tokens=30_000,
            machine_quota_tokens=30_000,
            now=lambda: 150,
        )
    assert persisted == []


def test_zai_candidate_constructs_with_remote_https_runtime() -> None:
    # z.ai (GLM-5.2) must be a poolable REMOTE_HTTPS candidate (Track-2 parity). Before adding
    # ZAI to route_pool._RUNTIME_BY_PROVIDER this raised route_candidate_invalid at construction.
    candidate = _candidate(LifecycleProviderId.ZAI, 3)
    assert candidate.provider is LifecycleProviderId.ZAI
    assert candidate.runtime_kind is RuntimeKind.REMOTE_HTTPS
    assert candidate.routing_policy_digest is None


def test_zai_candidate_rejects_non_remote_https_runtime() -> None:
    # z.ai is pinned to REMOTE_HTTPS by _RUNTIME_BY_PROVIDER; any other runtime fails closed.
    with pytest.raises(RoutePoolError, match="^route_candidate_invalid$"):
        _candidate(LifecycleProviderId.ZAI, 3, runtime_kind=RuntimeKind.LOCAL_CLI)


def test_zai_candidate_rejects_routing_policy_digest() -> None:
    # routing_policy_digest is OpenRouter-exclusive; every other provider (incl. z.ai) must
    # carry None. A non-None digest on z.ai must fail closed (regression pin for route_pool:172).
    with pytest.raises(RoutePoolError, match="^route_candidate_invalid$"):
        _candidate(LifecycleProviderId.ZAI, 3, routing_policy_digest="f" * 64)


def test_zai_candidate_pools_alongside_local_provider() -> None:
    # z.ai is poolable end-to-end: a cross-provider pool pairing a local CLI candidate with z.ai
    # constructs successfully once z.ai is a registered runtime.
    pool = _pool(
        candidates=(
            _candidate(LifecycleProviderId.CLAUDE_CODE, 1),
            _candidate(LifecycleProviderId.ZAI, 3),
        )
    )
    assert pool.candidates[1].provider is LifecycleProviderId.ZAI
    assert pool.candidates[1].runtime_kind is RuntimeKind.REMOTE_HTTPS
