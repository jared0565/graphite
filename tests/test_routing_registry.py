"""Verified Ollama Cloud registry and effort mapping tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import graphite.routing.registry as registry_module
from graphite.routing.contracts import Effort, ModelProfile, RiskTier
from graphite.routing.effort import EFFORT_PAYLOADS, EffortMappingError, effort_payload
from graphite.routing.registry import (
    BUNDLED_PROFILES,
    ModelRole,
    RegistryError,
    RegistryProfile,
    RegistrySnapshot,
    UsageClass,
    load_cached_registry,
    parse_inventory,
    profile_is_eligible,
    refresh_model_inventory,
    require_model_available,
)
from graphite.routing.storage import RepositoryStore


def _inventory() -> dict:
    return {
        "models": [
            {
                "name": "kimi-k2.7-code:cloud",
                "model": "kimi-k2.7-code:cloud",
                "digest": "a" * 64,
                "size": 388,
                "details": {"context_length": 262_144},
                "capabilities": ["completion", "tools", "thinking", "vision"],
            },
            {
                "name": "minimax-m2.7:cloud",
                "model": "minimax-m2.7:cloud",
                "digest": "b" * 64,
                "size": 323,
                "details": {"context_length": 204_800},
                "capabilities": ["completion", "tools", "thinking"],
            },
        ]
    }


def test_bundled_profiles_are_the_approved_nonexpiring_pool() -> None:
    assert set(BUNDLED_PROFILES) == {
        "kimi-k2.7-code:cloud",
        "minimax-m2.7:cloud",
        "nemotron-3-super:cloud",
        "minimax-m3:cloud",
    }
    expected = {
        "kimi-k2.7-code:cloud": (
            UsageClass.HIGH,
            {ModelRole.CODING_PRIMARY, ModelRole.CODING},
            262_144,
        ),
        "minimax-m2.7:cloud": (
            UsageClass.MEDIUM,
            {ModelRole.CODING, ModelRole.AGENTIC},
            204_800,
        ),
        "nemotron-3-super:cloud": (
            UsageClass.MEDIUM,
            {ModelRole.REASONING, ModelRole.REVIEW},
            262_144,
        ),
        "minimax-m3:cloud": (
            UsageClass.HIGH,
            {ModelRole.LONG_CONTEXT, ModelRole.AGENTIC},
            524_288,
        ),
    }
    expected_capabilities = {
        "kimi-k2.7-code:cloud": {"code", "completion", "tools", "thinking", "vision"},
        "minimax-m2.7:cloud": {"code", "completion", "tools", "thinking"},
        "nemotron-3-super:cloud": {"completion", "reasoning", "tools", "thinking"},
        "minimax-m3:cloud": {
            "architecture",
            "completion",
            "reasoning",
            "tools",
            "thinking",
            "vision",
        },
    }
    assert set(EFFORT_PAYLOADS) == set(BUNDLED_PROFILES)
    for model_id, entry in BUNDLED_PROFILES.items():
        usage, roles, context_window = expected[model_id]
        assert entry.profile.model_id == model_id
        assert entry.profile.profile_version == "2026-07-14.2"
        assert set(entry.profile.capabilities) == expected_capabilities[model_id]
        assert entry.profile.context_window_tokens == context_window
        assert entry.profile.provisional is True
        assert entry.usage_class is usage
        assert set(entry.roles) == roles
        assert entry.retirement_date is None
        assert entry.profile.supported_efforts == (Effort.DEFAULT,)
        assert entry.effort_payloads == {Effort.DEFAULT: {}}
        assert entry.evidence_url.startswith("https://ollama.com/library/")
        assert entry.evidence_accessed == "2026-07-14"


def test_removed_and_unapproved_models_are_not_profiles() -> None:
    for model_id in (
        "glm-5:cloud",
        "kimi-k2.6:cloud",
        "deepseek-v4-flash:cloud",
        "gemma4:31b-cloud",
        "qwen3.5:cloud",
    ):
        assert model_id not in BUNDLED_PROFILES


def _registry_profile(**changes: object) -> RegistryProfile:
    values = {
        "profile": ModelProfile(
            model_id="test-model:cloud",
            profile_version="1",
            capabilities=("completion",),
            context_window_tokens=128_000,
            supported_efforts=(Effort.DEFAULT,),
            provisional=True,
        ),
        "effort_payloads": {Effort.DEFAULT: {}},
        "evidence_url": "https://ollama.com/library/test-model",
        "evidence_accessed": "2026-07-14",
        "roles": (ModelRole.CODING,),
        "usage_class": UsageClass.MEDIUM,
        "retirement_date": None,
    }
    values.update(changes)
    return RegistryProfile(**values)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"roles": ()}, "roles_empty"),
        ({"roles": (ModelRole.CODING, "coding")}, "roles_duplicate"),
        ({"evidence_accessed": "2026-7-14"}, "evidence_accessed_invalid"),
        ({"evidence_accessed": "not-a-date"}, "evidence_accessed_invalid"),
        ({"usage_class": "low"}, "usage_class_invalid"),
        ({"retirement_date": "2026-07-14"}, "retirement_date_invalid"),
        ({"retirement_date": "2026-07-13"}, "retirement_date_invalid"),
    ],
)
def test_registry_profile_rejects_invalid_metadata(
    changes: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(ValueError, match=f"^{code}$"):
        _registry_profile(**changes)


def test_unknown_models_aliases_and_unsupported_efforts_are_ineligible() -> None:
    snapshot = parse_inventory(_inventory(), refreshed_at=100, ttl_seconds=300)

    with pytest.raises(RegistryError, match="model_profile_missing"):
        require_model_available(snapshot, "kimi-k2.7-code:latest")
    with pytest.raises(EffortMappingError, match="effort_unsupported"):
        effort_payload("kimi-k2.7-code:cloud", Effort.MAX)
    assert profile_is_eligible("kimi-k2.7-code:cloud", RiskTier.LOW) is True
    assert profile_is_eligible("kimi-k2.7-code:cloud", RiskTier.MEDIUM) is True
    assert profile_is_eligible("kimi-k2.7-code:cloud", RiskTier.HIGH) is False


def test_recommendation_reads_cached_snapshot_without_http(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    snapshot = parse_inventory(_inventory(), refreshed_at=100, ttl_seconds=300)
    store.save_registry_snapshot(snapshot.to_storage_dict())
    monkeypatch.setattr(
        registry_module.http.client,
        "HTTPConnection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("HTTP forbidden")),
    )

    cached = load_cached_registry(store, now=200)

    assert cached == snapshot
    assert require_model_available(cached, "kimi-k2.7-code:cloud").digest == "a" * 64


def test_expired_cached_snapshot_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    store.save_registry_snapshot(
        parse_inventory(_inventory(), refreshed_at=100, ttl_seconds=10).to_storage_dict()
    )

    with pytest.raises(RegistryError, match="registry_snapshot_expired"):
        load_cached_registry(store, now=111)


class _FakeResponse:
    status = 200
    reason = "OK"

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0
        self.headers = {"Content-Type": "application/json", "Content-Length": str(len(payload))}

    def read(self, amount: int) -> bytes:
        chunk = self._payload[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk


class _FakeConnection:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.request_call: tuple[str, str, dict[str, str]] | None = None

    def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
        self.request_call = (method, path, headers)

    def getresponse(self) -> _FakeResponse:
        return _FakeResponse(self.payload)

    def close(self) -> None:
        pass


def test_refresh_is_explicit_loopback_only_and_stores_sanitized_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    fake = _FakeConnection(json.dumps(_inventory()).encode("utf-8"))
    monkeypatch.setattr(
        registry_module.http.client,
        "HTTPConnection",
        lambda host, port, timeout: fake,
    )

    snapshot = refresh_model_inventory(store, now=100, ttl_seconds=300)

    assert fake.request_call == (
        "GET",
        "/api/tags",
        {"Accept": "application/json", "Connection": "close"},
    )
    assert snapshot.refreshed_at == 100
    stored = store.load_registry_snapshot()
    assert stored is not None
    serialized = json.dumps(stored)
    assert "remote_host" not in serialized
    assert "Authorization" not in serialized


@pytest.mark.parametrize(
    "payload",
    [
        {"models": "wrong"},
        {"models": [{}]},
        {"models": _inventory()["models"] * 129},
        {
            "models": [
                {
                    "name": "C:/private/model",
                    "model": "C:/private/model",
                    "digest": "a" * 64,
                    "details": {},
                    "capabilities": [],
                }
            ]
        },
    ],
)
def test_inventory_parser_rejects_unbounded_or_unsanitized_payload(payload: object) -> None:
    with pytest.raises(RegistryError):
        parse_inventory(payload, refreshed_at=100, ttl_seconds=300)


def test_model_identity_is_rechecked_against_exact_digest() -> None:
    snapshot = parse_inventory(_inventory(), refreshed_at=100, ttl_seconds=300)

    require_model_available(snapshot, "minimax-m2.7:cloud", expected_digest="b" * 64)
    with pytest.raises(RegistryError, match="model_identity_changed"):
        require_model_available(snapshot, "minimax-m2.7:cloud", expected_digest="c" * 64)


def test_snapshot_public_shape_is_bounded_and_path_free() -> None:
    snapshot = parse_inventory(_inventory(), refreshed_at=100, ttl_seconds=300)

    assert isinstance(snapshot, RegistrySnapshot)
    public = snapshot.to_dict()
    assert list(public) == ["schema_version", "refreshed_at", "expires_at", "models"]
    assert "remote_host" not in json.dumps(public)
