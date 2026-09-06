"""Tests for `graphite.llm_probe`, the isolated synthetic connectivity worker.

Named after the module so aramid's mutation stage 1 (`tests/test_<module>.py`)
has something to run; `-k llm_probe` collected one test before this file.
The doctor suite reaches this worker only as a subprocess, so the payload
validation and the fixed-schema result are pinned here directly.
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass

import pytest

from graphite import llm_probe
from graphite.config import Config
from graphite.llm import (
    PROBE_MAX_OUTPUT_TOKENS,
    CompletionResult,
    LLMConfigurationError,
    LLMProviderError,
)
from graphite.llm_probe import (
    SYSTEM_PROMPT,
    USER_PROMPT,
    WORKER_INPUT_LIMIT_BYTES,
    _classify_exception,
    _config_from_payload,
    run_synthetic_probe,
)


@dataclass
class _Provider:
    name: str = "fake"
    text: str = "READY"
    raises: Exception | None = None
    seen: list[tuple[str, str]] | None = None

    def complete(self, system: str, user: str) -> CompletionResult:
        if self.seen is not None:
            self.seen.append((system, user))
        if self.raises is not None:
            raise self.raises
        return CompletionResult(text=self.text)


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "mode": "local",
        "provider": "ollama",
        "model": None,
        "base_url": None,
        "api_key": None,
        "timeout_seconds": 5,
        "seed": 7,
        "system": SYSTEM_PROMPT,
        "user": USER_PROMPT,
    }
    base.update(overrides)
    return base


# --- _classify_exception ---------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (LLMConfigurationError("bad"), "configuration"),
        (LLMProviderError("authentication"), "authentication"),
        (TimeoutError(), "timeout"),
        (ConnectionError(), "connection"),
        (OSError(), "connection"),
        (RuntimeError("anything else"), "provider_error"),
    ],
)
def test_classify_exception_maps_each_failure_to_its_fixed_category(
    exc: Exception, category: str
) -> None:
    assert _classify_exception(exc) == category


# --- run_synthetic_probe ----------------------------------------------------------


def test_probe_reports_ready_without_carrying_the_response_text() -> None:
    seen: list[tuple[str, str]] = []
    provider = _Provider(text="  some answer  ", seen=seen)
    result = run_synthetic_probe(Config(), provider_factory=lambda cfg: provider)
    assert result == {"status": "ready", "response_present": True}
    assert seen == [(SYSTEM_PROMPT, USER_PROMPT)]


def test_probe_caps_output_tokens_on_the_configuration_it_hands_the_factory() -> None:
    handed: list[Config] = []

    def factory(cfg: Config) -> _Provider:
        handed.append(cfg)
        return _Provider()

    run_synthetic_probe(Config(llm_max_output_tokens=4096), provider_factory=factory)
    assert [cfg.llm_max_output_tokens for cfg in handed] == [PROBE_MAX_OUTPUT_TOKENS]


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_probe_treats_a_blank_completion_as_a_provider_error(text: str) -> None:
    result = run_synthetic_probe(Config(), provider_factory=lambda cfg: _Provider(text=text))
    assert result == {"status": "degraded", "category": "provider_error"}


def test_probe_classifies_a_provider_exception_instead_of_raising() -> None:
    provider = _Provider(raises=LLMProviderError("timeout"))
    result = run_synthetic_probe(Config(), provider_factory=lambda cfg: provider)
    assert result == {"status": "degraded", "category": "timeout"}


def test_probe_classifies_a_factory_failure_as_configuration() -> None:
    def factory(cfg: Config) -> _Provider:
        raise LLMConfigurationError("no key")

    assert run_synthetic_probe(Config(), provider_factory=factory) == {
        "status": "degraded",
        "category": "configuration",
    }


# --- _config_from_payload ---------------------------------------------------------


def test_config_from_payload_maps_every_field() -> None:
    cfg = _config_from_payload(
        _payload(mode="Cloud", provider="openrouter", model="m", base_url="http://h", api_key="k")
    )
    assert cfg == Config(
        llm_mode="Cloud",
        llm_provider="openrouter",
        llm_model="m",
        llm_base_url="http://h",
        llm_api_key="k",
        llm_timeout_seconds=5.0,
        llm_max_output_tokens=PROBE_MAX_OUTPUT_TOKENS,
        seed=7,
    )


@pytest.mark.parametrize(
    "payload",
    [
        "not a dict",
        {},
        {**_payload(), "extra": 1},
        {k: v for k, v in _payload().items() if k != "seed"},
        _payload(mode="hybrid"),
        _payload(mode=5),
        _payload(provider="p" * 129),
        _payload(timeout_seconds=True),
        _payload(timeout_seconds=0),
        _payload(timeout_seconds=61),
        _payload(timeout_seconds=float("nan")),
        _payload(seed=True),
        _payload(seed="7"),
        _payload(system="different"),
        _payload(user="different"),
        _payload(model=12),
        _payload(model="m" * 513),
        _payload(base_url="u" * 2049),
        _payload(api_key="k" * 4097),
    ],
    ids=[
        "not-a-dict",
        "empty",
        "extra-key",
        "missing-key",
        "unknown-mode",
        "non-string-mode",
        "provider-too-long",
        "bool-timeout",
        "zero-timeout",
        "timeout-over-60",
        "nan-timeout",
        "bool-seed",
        "string-seed",
        "wrong-system-prompt",
        "wrong-user-prompt",
        "non-string-model",
        "model-too-long",
        "base-url-too-long",
        "api-key-too-long",
    ],
)
def test_config_from_payload_rejects_anything_outside_the_contract(payload: object) -> None:
    with pytest.raises(ValueError):
        _config_from_payload(payload)


def test_config_from_payload_accepts_the_boundary_lengths() -> None:
    cfg = _config_from_payload(
        _payload(model="m" * 512, base_url="u" * 2048, api_key="k" * 4096, timeout_seconds=60)
    )
    assert (len(cfg.llm_model or ""), len(cfg.llm_base_url or ""), len(cfg.llm_api_key or "")) == (
        512,
        2048,
        4096,
    )
    assert cfg.llm_timeout_seconds == 60.0


# --- main() -----------------------------------------------------------------------


def _run_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raw: bytes
) -> tuple[int, dict[str, object]]:
    stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    monkeypatch.setattr(llm_probe.sys, "stdin", stdin)
    rc = llm_probe.main()
    return rc, json.loads(capsys.readouterr().out)


def test_main_runs_the_probe_on_a_valid_payload_and_prints_compact_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handed: list[Config] = []

    def fake_probe(cfg: Config) -> dict[str, object]:
        handed.append(cfg)
        return {"status": "ready", "response_present": True}

    monkeypatch.setattr(llm_probe, "run_synthetic_probe", fake_probe)
    rc, result = _run_main(monkeypatch, capsys, json.dumps(_payload()).encode("utf-8"))
    assert rc == 0
    assert result == {"status": "ready", "response_present": True}
    assert handed == [_config_from_payload(_payload())]


def test_main_reports_an_oversized_or_invalid_input_as_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def never(cfg: Config) -> dict[str, object]:
        raise AssertionError("the probe must not run on rejected input")

    monkeypatch.setattr(llm_probe, "run_synthetic_probe", never)
    oversized = b" " * (WORKER_INPUT_LIMIT_BYTES + 1)
    for raw in (oversized, b"not json", json.dumps(_payload(mode="hybrid")).encode("utf-8")):
        rc, result = _run_main(monkeypatch, capsys, raw)
        assert rc == 0
        assert result == {"status": "degraded", "category": "configuration"}


def test_main_never_raises_past_the_fixed_schema(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode(cfg: Config) -> dict[str, object]:
        raise RuntimeError("provider code path blew up")

    monkeypatch.setattr(llm_probe, "run_synthetic_probe", explode)
    rc, result = _run_main(monkeypatch, capsys, json.dumps(_payload()).encode("utf-8"))
    assert rc == 0
    assert result == {"status": "degraded", "category": "configuration"}
