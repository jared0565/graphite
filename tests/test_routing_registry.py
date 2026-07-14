"""Verified Ollama Cloud registry and effort mapping tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import graphite.routing.registry as registry_module
from graphite.routing.contracts import Effort, RiskTier
from graphite.routing.effort import EffortMappingError, effort_payload
from graphite.routing.registry import (
    BUNDLED_PROFILES,
    RegistryError,
    RegistrySnapshot,
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
                "name": "glm-5:cloud",
                "model": "glm-5:cloud",
                "digest": "b" * 64,
                "size": 323,
                "details": {"context_length": 202_752},
                "capabilities": ["completion", "tools", "thinking"],
            },
        ]
    }


def test_bundled_profiles_use_exact_verified_identifiers() -> None:
    assert set(BUNDLED_PROFILES) == {
        "kimi-k2.7-code:cloud",
        "kimi-k2.6:cloud",
        "glm-5:cloud",
    }
    assert "glm-5.2:cloud" not in BUNDLED_PROFILES
    for model_id, entry in BUNDLED_PROFILES.items():
        assert entry.profile.model_id == model_id
        assert entry.profile.provisional is True
        assert entry.profile.supported_efforts == (Effort.DEFAULT,)
        assert entry.effort_payloads == {Effort.DEFAULT: {}}
        assert entry.evidence_url.startswith("https://")
        assert entry.evidence_accessed == "2026-07-14"


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

    require_model_available(snapshot, "glm-5:cloud", expected_digest="b" * 64)
    with pytest.raises(RegistryError, match="model_identity_changed"):
        require_model_available(snapshot, "glm-5:cloud", expected_digest="c" * 64)


def test_snapshot_public_shape_is_bounded_and_path_free() -> None:
    snapshot = parse_inventory(_inventory(), refreshed_at=100, ttl_seconds=300)

    assert isinstance(snapshot, RegistrySnapshot)
    public = snapshot.to_dict()
    assert list(public) == ["schema_version", "refreshed_at", "expires_at", "models"]
    assert "remote_host" not in json.dumps(public)
