"""Provider-agnostic LLM enrichment tests."""
from __future__ import annotations

import json
from typing import Any

import pytest

from graphite.config import Config
from graphite.llm import (
    MAX_RESPONSE_BYTES,
    LLMProviderError,
    _urlopen_json,
    build_report_prompt,
    enrich_report,
)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        del size
        return json.dumps(self.payload).encode("utf-8")


def _minimal_graph() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    graph = {"metadata": {"node_count": 3, "edge_count": 2}}
    clusters = {"clusters": [{"id": 1, "size": 3, "file_count": 1, "function_count": 2, "class_count": 0, "labels": ["src"]}]}
    analysis = {
        "top_files_by_links": [{"name": "app.ts", "degree": 2}],
        "god_nodes": [{"id": "src_app", "name": "app.ts", "kind": "file", "degree": 2, "in_degree": 0, "out_degree": 2}],
        "entry_points": [],
        "surprising_connections": [],
    }
    return graph, clusters, analysis


def test_llm_disabled_does_not_call_provider(monkeypatch) -> None:
    def fail_urlopen(*_: object, **__: object) -> None:
        raise AssertionError("urlopen should not be called when Graphite LLM mode is none")

    monkeypatch.setattr("graphite.llm._open_no_redirect", fail_urlopen)
    graph, clusters, analysis = _minimal_graph()

    result = enrich_report(graph, clusters, analysis, Config())

    assert result == {"enabled": False, "mode": "none", "tokens": 0}


def test_canonical_graph_config_removes_all_provider_settings() -> None:
    canonical = Config(
        llm_mode="cloud",
        llm_provider="openrouter",
        llm_model="vendor/model",
        llm_base_url="https://provider.invalid/v1",
        llm_api_key="secret",
        llm_timeout_seconds=1,
        llm_max_input_chars=99,
        llm_max_output_tokens=77,
    ).canonical_graph()

    assert canonical.llm_mode == "none"
    assert canonical.llm_provider == "none"
    assert canonical.llm_model is None
    assert canonical.llm_base_url is None
    assert canonical.llm_api_key is None
    assert canonical.llm_timeout_seconds == 30
    assert canonical.llm_max_input_chars == 12_000
    assert canonical.llm_max_output_tokens == 512


def test_openai_compatible_provider_is_selected_by_base_url(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout: float):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {
                "choices": [{"message": {"content": "- Hotspots: app.ts"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }
        )

    monkeypatch.setattr("graphite.llm._open_no_redirect", fake_urlopen)
    graph, clusters, analysis = _minimal_graph()
    cfg = Config(
        llm_mode="cloud",
        llm_provider="openai-compatible",
        llm_model="portable-model",
        llm_base_url="http://llm.example/v1",
        llm_api_key="secret-token",
        llm_timeout_seconds=5.5,
        llm_max_output_tokens=777,
    )

    result = enrich_report(graph, clusters, analysis, cfg)

    assert result["status"] == "ok"
    assert result["provider"] == "openai-compatible"
    assert result["model"] == "portable-model"
    assert result["tokens"] == 18
    assert captured["url"] == "http://llm.example/v1/chat/completions"
    assert captured["timeout"] == 5.5
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["body"]["model"] == "portable-model"
    assert captured["body"]["stream"] is False
    assert captured["body"]["max_tokens"] == 777


def test_openrouter_provider_uses_default_base_url(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout: float):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {
                "choices": [{"message": {"content": "openrouter summary"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }
        )

    monkeypatch.setattr("graphite.llm._open_no_redirect", fake_urlopen)
    graph, clusters, analysis = _minimal_graph()
    cfg = Config(
        llm_mode="cloud",
        llm_provider="openrouter",
        llm_model="~openai/gpt-latest",
        llm_api_key="openrouter-secret",
    )

    result = enrich_report(graph, clusters, analysis, cfg)

    assert result["status"] == "ok"
    assert result["provider"] == "openai-compatible"
    assert result["model"] == "~openai/gpt-latest"
    assert result["tokens"] == 5
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer openrouter-secret"
    assert captured["body"]["model"] == "~openai/gpt-latest"

def test_openrouter_provider_defaults_to_kimi_code(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout: float):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {
                "choices": [{"message": {"content": "openrouter default summary"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            }
        )

    monkeypatch.setattr("graphite.llm._open_no_redirect", fake_urlopen)
    graph, clusters, analysis = _minimal_graph()
    cfg = Config(
        llm_mode="cloud",
        llm_provider="openrouter",
        llm_api_key="openrouter-secret",
    )

    result = enrich_report(graph, clusters, analysis, cfg)

    assert result["status"] == "ok"
    assert result["model"] == "moonshotai/kimi-k2.7-code"
    assert captured["body"]["model"] == "moonshotai/kimi-k2.7-code"

def test_ollama_provider_uses_native_chat_endpoint(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout: float):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {
                "message": {"content": "local summary"},
                "prompt_eval_count": 5,
                "eval_count": 3,
            }
        )

    monkeypatch.setattr("graphite.llm._open_no_redirect", fake_urlopen)
    graph, clusters, analysis = _minimal_graph()
    cfg = Config(llm_mode="local", llm_provider="ollama", llm_model="qwen2.5-coder")

    result = enrich_report(graph, clusters, analysis, cfg)

    assert result["status"] == "ok"
    assert result["provider"] == "ollama"
    assert result["summary"] == "local summary"
    assert result["tokens"] == 8
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["body"]["model"] == "qwen2.5-coder"
    assert captured["body"]["options"]["num_predict"] == 512


def test_provider_response_read_is_bounded_and_overflow_is_rejected(monkeypatch) -> None:
    read_sizes: list[int] = []

    class OversizedResponse:
        def __enter__(self) -> "OversizedResponse":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            read_sizes.append(size)
            return b"x" * size

    monkeypatch.setattr(
        "graphite.llm._open_no_redirect",
        lambda request, timeout: OversizedResponse(),
    )

    with pytest.raises(LLMProviderError) as exc_info:
        _urlopen_json(object(), 1.0)  # type: ignore[arg-type]

    assert exc_info.value.category == "provider_error"
    assert read_sizes == [MAX_RESPONSE_BYTES + 1]


@pytest.mark.parametrize("status", [301, 302, 303])
def test_provider_redirect_is_not_followed_and_error_body_is_bounded(
    monkeypatch,
    status: int,
) -> None:
    import urllib.error
    import urllib.request

    opens: list[str] = []
    read_sizes: list[int] = []
    handlers: list[object] = []

    class ErrorBody:
        def read(self, size: int) -> bytes:
            read_sizes.append(size)
            return b"redirect body with credential-shaped private data"

        def close(self) -> None:
            return None

    class FakeOpener:
        def open(self, request, timeout: float):
            del timeout
            opens.append(request.full_url)
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                "redirect",
                {"Location": "https://attacker.invalid/collect"},
                ErrorBody(),
            )

    def fake_build_opener(*configured_handlers: object) -> FakeOpener:
        handlers.extend(configured_handlers)
        return FakeOpener()

    monkeypatch.setattr("urllib.request.build_opener", fake_build_opener)
    request = urllib.request.Request(
        "https://provider.invalid/v1/chat/completions",
        headers={"Authorization": "Bearer ApiKey_SECRET"},
    )

    with pytest.raises(LLMProviderError) as exc_info:
        _urlopen_json(request, 1.0)

    assert exc_info.value.category == "provider_error"
    assert "credential-shaped" not in str(exc_info.value)
    assert opens == ["https://provider.invalid/v1/chat/completions"]
    assert read_sizes == [MAX_RESPONSE_BYTES + 1]
    redirect_handler = next(handler for handler in handlers if hasattr(handler, "redirect_request"))
    assert (
        redirect_handler.redirect_request(
            request, None, status, "redirect", {}, request.full_url
        )
        is None
    )


def test_llm_output_token_config_defaults_overrides_and_clamps() -> None:
    assert Config().llm_max_output_tokens == 512
    assert Config.from_env({"GRAPHITE_LLM_MAX_OUTPUT_TOKENS": "2048"}).llm_max_output_tokens == 2048
    assert Config.from_env({"GRAPHITE_LLM_MAX_OUTPUT_TOKENS": "0"}).llm_max_output_tokens == 1
    assert Config.from_env({"GRAPHITE_LLM_MAX_OUTPUT_TOKENS": "999999"}).llm_max_output_tokens == 4096
    assert Config.from_env({"GRAPHITE_LLM_MAX_OUTPUT_TOKENS": "invalid"}).llm_max_output_tokens == 512


def test_llm_auto_skips_simple_graph(monkeypatch) -> None:
    def fail_urlopen(*_: object, **__: object) -> None:
        raise AssertionError("auto should not call provider for a small simple graph")

    monkeypatch.setattr("graphite.llm._open_no_redirect", fail_urlopen)
    graph, clusters, analysis = _minimal_graph()
    cfg = Config(llm_mode="auto", llm_provider="openrouter", llm_api_key="openrouter-secret")

    result = enrich_report(graph, clusters, analysis, cfg)

    assert result["enabled"] is False
    assert result["status"] == "skipped"
    assert result["reason"] == "graph_below_auto_threshold"
    assert result["auto"]["signals"] == []


def test_llm_auto_skips_cloud_provider_without_api_key(monkeypatch) -> None:
    def fail_urlopen(*_: object, **__: object) -> None:
        raise AssertionError("auto should not call cloud provider without an API key")

    monkeypatch.setattr("graphite.llm._open_no_redirect", fail_urlopen)
    graph = {"metadata": {"node_count": 300, "edge_count": 500}}
    clusters = {"count": 10, "clusters": []}
    analysis = {"top_files_by_links": [{}] * 10, "god_nodes": [{}, {}], "surprising_connections": []}
    cfg = Config(llm_mode="auto", llm_provider="openrouter")

    result = enrich_report(graph, clusters, analysis, cfg)

    assert result["enabled"] is False
    assert result["status"] == "skipped"
    assert result["reason"] == "missing_llm_api_key"
    assert "node_count>=250" in result["auto"]["signals"]


def test_llm_auto_runs_when_threshold_and_provider_ready(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout: float):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {
                "choices": [{"message": {"content": "auto summary"}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
            }
        )

    monkeypatch.setattr("graphite.llm._open_no_redirect", fake_urlopen)
    graph = {"metadata": {"node_count": 300, "edge_count": 500}}
    clusters = {"count": 10, "clusters": []}
    analysis = {"top_files_by_links": [{}] * 10, "god_nodes": [{}, {}], "surprising_connections": []}
    cfg = Config(llm_mode="auto", llm_provider="openrouter", llm_api_key="openrouter-secret")

    result = enrich_report(graph, clusters, analysis, cfg)

    assert result["status"] == "ok"
    assert result["mode"] == "auto"
    assert result["effective_mode"] == "cloud"
    assert result["model"] == "moonshotai/kimi-k2.7-code"
    assert result["tokens"] == 13
    assert result["auto"]["reason"] == "auto_threshold_met"
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["body"]["model"] == "moonshotai/kimi-k2.7-code"

def test_unsupported_provider_is_non_fatal() -> None:
    graph, clusters, analysis = _minimal_graph()
    cfg = Config(llm_mode="cloud", llm_provider="imaginary-vendor")

    result = enrich_report(graph, clusters, analysis, cfg)

    assert result["status"] == "error"
    assert result["tokens"] == 0
    assert result["error_category"] == "configuration"
    assert "error" not in result


def test_prompt_is_bounded_and_uses_graph_metadata_only() -> None:
    graph, clusters, analysis = _minimal_graph()
    analysis["god_nodes"] = [{"id": f"node_{i}", "name": "x" * 80} for i in range(100)]
    cfg = Config(llm_max_input_chars=500)

    _, user = build_report_prompt(graph, clusters, analysis, cfg)

    assert len(user) < 750
    assert "truncated by GRAPHITE_LLM_MAX_INPUT_CHARS" in user
    assert "source code" not in user.lower()
