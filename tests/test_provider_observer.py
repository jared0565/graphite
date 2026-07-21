from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from graphite.config import Config
from graphite.provider_observer import (
    ProviderObservationTarget,
    ProviderObserver,
    ProviderObserverOptions,
    VerificationManifestRequest,
)
from graphite.routing.contracts import Effort
from graphite.routing.lifecycle import (
    LifecycleProviderId,
    ProviderCompatibilityPolicy,
    ProviderLifecycleState,
    ProviderRuntimeIdentity,
    RuntimeKind,
)
from graphite.routing.lifecycle_service import ProviderLifecycleService
from graphite.routing.lifecycle_storage import LifecycleStore
from graphite.routing.probe_runner import ProviderProbeError
from graphite.routing.storage import RepositoryStore


def _identity(provider: LifecycleProviderId, observed_at: int, digest: str = "a" * 64) -> ProviderRuntimeIdentity:
    version = "2.1.214" if provider is LifecycleProviderId.CLAUDE_CODE else "0.144.1"
    return ProviderRuntimeIdentity(
        provider,
        RuntimeKind.LOCAL_CLI,
        version,
        digest,
        None,
        None,
        ("credential_health", "structured_output", "version"),
        "1.0.0",
        observed_at,
    )


def _policy(provider: LifecycleProviderId) -> ProviderCompatibilityPolicy:
    return ProviderCompatibilityPolicy(
        provider,
        RuntimeKind.LOCAL_CLI,
        "1.0.0",
        "0.1.0",
        "3.0.0",
        ("credential_health", "structured_output"),
    )


def _service(root: Path) -> ProviderLifecycleService:
    root.mkdir(parents=True)
    lifecycle = LifecycleStore(root)
    lifecycle.initialize()
    routing = RepositoryStore(root)
    routing.initialize()
    return ProviderLifecycleService(lifecycle, routing)


def _target(
    root: Path,
    provider: LifecycleProviderId,
    probe,
    *,
    target_id: str,
    cache_key: str | None = None,
) -> ProviderObservationTarget:
    return ProviderObservationTarget(
        target_id=target_id,
        boundary_digest=hashlib.sha256(target_id.encode("ascii")).hexdigest(),
        provider=provider,
        runtime_kind=RuntimeKind.LOCAL_CLI,
        policy=_policy(provider),
        lifecycle_service=_service(root),
        probe=probe,
        machine_cache_key=cache_key,
    )


def test_options_are_bounded() -> None:
    ProviderObserverOptions().validate()
    for changes in (
        {"interval_seconds": 0},
        {"timeout_seconds": 31},
        {"max_observations_per_cycle": 0},
        {"backoff_cap_seconds": 1},
        {"jitter_ratio": 0.6},
    ):
        with pytest.raises(ValueError):
            replace(ProviderObserverOptions(), **changes).validate()


def test_config_builds_fail_closed_observer_options() -> None:
    config = Config.from_env(
        {
            "GRAPHITE_PROVIDER_OBSERVER_ENABLED_PROVIDERS": "claude-code,codex,ollama,openrouter,zai",
            "GRAPHITE_PROVIDER_OBSERVER_INTERVAL": "30",
            "GRAPHITE_PROVIDER_OBSERVER_TIMEOUT": "2.5",
            "GRAPHITE_PROVIDER_OBSERVER_MAX_PER_CYCLE": "2",
            "GRAPHITE_PROVIDER_OBSERVER_BACKOFF_CAP": "300",
            "GRAPHITE_PROVIDER_OBSERVER_JITTER_RATIO": "0",
        }
    )

    options = config.provider_observer_options()

    assert options.enabled_providers == tuple(LifecycleProviderId)
    assert options.interval_seconds == 30
    assert options.timeout_seconds == 2.5
    assert options.max_observations_per_cycle == 2
    assert options.backoff_cap_seconds == 300
    assert options.jitter_ratio == 0
    with pytest.raises(ValueError, match="observer_enabled_providers_invalid"):
        Config(
            provider_observer_enabled_providers=("unknown-provider",)
        ).provider_observer_options()


def test_cycle_caps_work_and_caches_identical_machine_cli_probe(tmp_path: Path) -> None:
    calls: list[tuple[float, int]] = []

    def probe(timeout: float, observed_at: int) -> ProviderRuntimeIdentity:
        calls.append((timeout, observed_at))
        return _identity(LifecycleProviderId.CLAUDE_CODE, observed_at)

    options = ProviderObserverOptions(
        interval_seconds=60,
        timeout_seconds=3,
        max_observations_per_cycle=2,
        backoff_cap_seconds=600,
        jitter_ratio=0,
    )
    observer = ProviderObserver(options)
    targets = (
        _target(tmp_path / "one", LifecycleProviderId.CLAUDE_CODE, probe, target_id="a-one", cache_key="c" * 64),
        _target(tmp_path / "two", LifecycleProviderId.CLAUDE_CODE, probe, target_id="b-two", cache_key="c" * 64),
        _target(tmp_path / "three", LifecycleProviderId.CODEX, probe, target_id="d-three"),
    )

    summary = observer.run_cycle(targets, now=100)

    assert len(calls) == 1
    assert calls == [(3.0, 100)]
    assert summary.attempted == 2
    assert summary.deferred == 1
    assert summary.state_counts == {"verification_required": 2}
    assert summary.to_status() == {
        "attempted": 2,
        "deferred": 1,
        "succeeded": 2,
        "failed": 0,
        "state_counts": {"verification_required": 2},
        "reason_counts": {"discovered": 2},
    }


def test_cycle_can_prepare_exact_manifest_but_cannot_execute_it(tmp_path: Path) -> None:
    target = _target(
        tmp_path / "project",
        LifecycleProviderId.CLAUDE_CODE,
        lambda _timeout, observed_at: _identity(
            LifecycleProviderId.CLAUDE_CODE, observed_at
        ),
        target_id="manifest-target",
    )
    target = replace(
        target,
        verification_request=VerificationManifestRequest(
            requested_model="claude-sonnet-5",
            expected_effective_model="claude-sonnet-5",
            effort=Effort.HIGH,
            max_input_tokens=1_000,
            max_output_tokens=100,
            timeout_seconds=30,
            expires_at=200,
            fixture_repository_commit="1" * 40,
            graph_fingerprint="2" * 64,
            prompt_contract_hash="3" * 64,
            response_contract_hash="4" * 64,
        ),
    )

    summary = ProviderObserver(
        ProviderObserverOptions(max_observations_per_cycle=1, jitter_ratio=0)
    ).run_cycle((target,), now=100)

    assert len(summary.prepared_manifests) == 1
    manifest = summary.prepared_manifests[0]
    assert manifest.lifecycle_identity_digest == _identity(
        LifecycleProviderId.CLAUDE_CODE, 100
    ).digest
    assert manifest.max_attempts == 1
    assert manifest.fallback_enabled is False
    assert manifest.no_resume is True
    assert "prepared_manifests" not in summary.to_status()


def test_http_or_project_scoped_targets_cannot_share_machine_cache() -> None:
    with pytest.raises(ValueError, match="observer_cache_scope_invalid"):
        ProviderObservationTarget(
            target_id="ollama-one",
            boundary_digest="a" * 64,
            provider=LifecycleProviderId.OLLAMA,
            runtime_kind=RuntimeKind.LOCAL_HTTP,
            policy=ProviderCompatibilityPolicy(
                LifecycleProviderId.OLLAMA,
                RuntimeKind.LOCAL_HTTP,
                "1.0.0",
                "0.1.0",
                "2.0.0",
                ("metadata_show",),
            ),
            lifecycle_service=object(),
            probe=lambda *_: None,
            machine_cache_key="b" * 64,
        )


def test_all_absent_providers_are_independent_and_never_retried_in_cycle(tmp_path: Path) -> None:
    runtime_kinds = {
        LifecycleProviderId.CLAUDE_CODE: RuntimeKind.LOCAL_CLI,
        LifecycleProviderId.CODEX: RuntimeKind.LOCAL_CLI,
        LifecycleProviderId.OLLAMA: RuntimeKind.LOCAL_HTTP,
        LifecycleProviderId.OPENROUTER: RuntimeKind.REMOTE_HTTPS,
        LifecycleProviderId.ZAI: RuntimeKind.REMOTE_HTTPS,
    }
    calls: list[LifecycleProviderId] = []
    targets: list[ProviderObservationTarget] = []
    for index, provider in enumerate(LifecycleProviderId):
        runtime_kind = runtime_kinds[provider]

        def absent(_timeout: float, _observed_at: int, provider=provider):
            calls.append(provider)
            raise ProviderProbeError("probe_unavailable")

        targets.append(
            ProviderObservationTarget(
                target_id=f"target-{index}",
                boundary_digest=f"{index + 1:x}" * 64,
                provider=provider,
                runtime_kind=runtime_kind,
                policy=ProviderCompatibilityPolicy(
                    provider,
                    runtime_kind,
                    "1.0.0",
                    "0.1.0",
                    "3.0.0",
                    ("credential_health",),
                ),
                lifecycle_service=_service(tmp_path / provider.value),
                probe=absent,
            )
        )
    observer = ProviderObserver(
        ProviderObserverOptions(max_observations_per_cycle=5, jitter_ratio=0)
    )

    summary = observer.run_cycle(targets, now=100)

    assert calls == list(LifecycleProviderId)
    assert summary.attempted == 5
    assert summary.failed == 5
    assert summary.state_counts == {"unavailable": 5}
    assert summary.reason_counts == {"probe_unavailable": 5}


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("probe_timeout", "probe_timeout"),
        ("probe_auth_unhealthy", "credential_unhealthy"),
        ("probe_executable_invalid", "runtime_missing"),
        ("path=C:/secret token=abc", "probe_failed"),
    ],
)
def test_probe_failures_are_sanitized_persisted_and_backed_off(
    tmp_path: Path, code: str, reason: str
) -> None:
    def probe(_timeout: float, _observed_at: int) -> ProviderRuntimeIdentity:
        raise ProviderProbeError(code)

    target = _target(
        tmp_path / "project",
        LifecycleProviderId.CLAUDE_CODE,
        probe,
        target_id="a-target",
    )
    observer = ProviderObserver(
        ProviderObserverOptions(
            interval_seconds=10,
            timeout_seconds=1,
            max_observations_per_cycle=1,
            backoff_cap_seconds=40,
            jitter_ratio=0,
        )
    )

    first = observer.run_cycle((target,), now=100)
    not_due = observer.run_cycle((target,), now=119)
    second = observer.run_cycle((target,), now=120)

    assert first.reason_counts == {reason: 1}
    assert first.state_counts == {"unavailable": 1}
    assert first.failed == 1
    assert not_due.attempted == 0
    assert second.attempted == 1
    assert "secret" not in repr(first.to_status())
    assert "token" not in repr(first.to_status())


def test_corrupt_lifecycle_database_isolated_from_other_targets(tmp_path: Path) -> None:
    bad_root = tmp_path / "bad"
    bad = _target(
        bad_root,
        LifecycleProviderId.CLAUDE_CODE,
        lambda _timeout, observed_at: _identity(LifecycleProviderId.CLAUDE_CODE, observed_at),
        target_id="a-bad",
    )
    bad.lifecycle_service.lifecycle_store.path.write_bytes(b"not sqlite")
    good = _target(
        tmp_path / "good",
        LifecycleProviderId.CODEX,
        lambda _timeout, observed_at: _identity(LifecycleProviderId.CODEX, observed_at),
        target_id="b-good",
    )
    observer = ProviderObserver(
        ProviderObserverOptions(max_observations_per_cycle=2, jitter_ratio=0)
    )

    summary = observer.run_cycle((bad, good), now=100)

    assert summary.attempted == 2
    assert summary.succeeded == 1
    assert summary.failed == 1
    assert summary.reason_counts == {
        "discovered": 1,
        "lifecycle_persistence_failed": 1,
    }


def test_simultaneous_provider_drift_stays_independent(tmp_path: Path) -> None:
    current = {LifecycleProviderId.CLAUDE_CODE: "a" * 64, LifecycleProviderId.CODEX: "b" * 64}

    def make(provider: LifecycleProviderId):
        return lambda _timeout, observed_at: _identity(provider, observed_at, current[provider])

    targets = (
        _target(tmp_path / "claude", LifecycleProviderId.CLAUDE_CODE, make(LifecycleProviderId.CLAUDE_CODE), target_id="a-claude"),
        _target(tmp_path / "codex", LifecycleProviderId.CODEX, make(LifecycleProviderId.CODEX), target_id="b-codex"),
    )
    observer = ProviderObserver(
        ProviderObserverOptions(interval_seconds=10, max_observations_per_cycle=2, jitter_ratio=0)
    )
    observer.run_cycle(targets, now=100)
    current[LifecycleProviderId.CLAUDE_CODE] = "c" * 64
    current[LifecycleProviderId.CODEX] = "d" * 64

    summary = observer.run_cycle(targets, now=110)

    assert summary.state_counts == {"verification_required": 2}
    assert summary.reason_counts == {"hash_changed": 2}
    assert all(
        target.lifecycle_service.lifecycle_store.current_observation(target.boundary_digest).state
        is ProviderLifecycleState.VERIFICATION_REQUIRED
        for target in targets
    )
