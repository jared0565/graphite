"""Provider-agnostic optional LLM enrichment for Graphite reports."""
from __future__ import annotations

import json
from dataclasses import replace
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Config


@dataclass(frozen=True)
class CompletionResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class CompletionProvider(Protocol):
    name: str

    def complete(self, system: str, user: str) -> CompletionResult:
        ...


class LLMConfigurationError(ValueError):
    """Raised when optional LLM enrichment is enabled but misconfigured."""


class OpenAICompatibleProvider:
    """Chat-completions adapter for OpenAI-compatible HTTP APIs.

    This intentionally avoids a vendor SDK. It works with OpenAI-compatible gateways such as
    OpenAI, OpenRouter, LM Studio, vLLM, Groq-style endpoints, and local servers that expose
    `/v1/chat/completions`.
    """

    name = "openai-compatible"

    def __init__(self, cfg: Config) -> None:
        base_url = cfg.llm_base_url or _default_openai_base_url(cfg.llm_provider)
        if not base_url:
            raise LLMConfigurationError(
                "GRAPHITE_LLM_BASE_URL is required for openai-compatible providers"
            )
        self.base_url = base_url.rstrip("/")
        self.model = cfg.llm_model or _default_model(cfg.llm_provider)
        self.api_key = cfg.llm_api_key
        self.timeout = cfg.llm_timeout_seconds
        self.seed = cfg.seed

    def complete(self, system: str, user: str) -> CompletionResult:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "stream": False,
        }
        if self.seed is not None:
            payload["seed"] = self.seed
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            headers=headers,
            method="POST",
        )
        body = _urlopen_json(request, self.timeout)
        choices = body.get("choices") or []
        text = ""
        if choices:
            message = choices[0].get("message") or {}
            text = str(message.get("content") or "").strip()
        usage = body.get("usage") or {}
        return CompletionResult(
            text=text,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        )


class OllamaProvider:
    """Native Ollama chat adapter using `/api/chat`."""

    name = "ollama"

    def __init__(self, cfg: Config) -> None:
        self.base_url = (cfg.llm_base_url or "http://localhost:11434").rstrip("/")
        self.model = cfg.llm_model or "llama3.1"
        self.timeout = cfg.llm_timeout_seconds
        self.seed = cfg.seed

    def complete(self, system: str, user: str) -> CompletionResult:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.1, "seed": self.seed},
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        body = _urlopen_json(request, self.timeout)
        message = body.get("message") or {}
        input_tokens = int(body.get("prompt_eval_count") or 0)
        output_tokens = int(body.get("eval_count") or 0)
        return CompletionResult(
            text=str(message.get("content") or "").strip(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )


def make_provider(cfg: Config) -> CompletionProvider:
    provider = cfg.llm_provider.strip().lower().replace("_", "-")
    if provider in {"ollama", "local"}:
        return OllamaProvider(cfg)
    if provider in {"openai", "openai-compatible", "compatible", "lmstudio", "lm-studio", "vllm", "openrouter", "groq"}:
        return OpenAICompatibleProvider(cfg)
    raise LLMConfigurationError(
        f"unsupported Graphite LLM provider '{cfg.llm_provider}'. Use 'ollama' or 'openai-compatible'."
    )


def _provider_requires_api_key(provider: str) -> bool:
    normalized = provider.strip().lower().replace("_", "-")
    return normalized in {"openai", "openrouter", "groq"}


def _provider_has_default_base_url(provider: str) -> bool:
    normalized = provider.strip().lower().replace("_", "-")
    return normalized in {"openai", "openrouter", "groq", "lmstudio", "lm-studio", "vllm"}


def _auto_effective_mode(provider: str) -> str:
    normalized = provider.strip().lower().replace("_", "-")
    if normalized in {"ollama", "local", "lmstudio", "lm-studio", "vllm"}:
        return "local"
    return "cloud"


def decide_auto_llm(
    graph_data: dict[str, Any],
    clusters: dict[str, Any],
    analysis: dict[str, Any],
    cfg: Config,
) -> dict[str, Any]:
    """Decide whether optional LLM enrichment is worth running for this graph."""
    metadata = graph_data.get("metadata") or {}
    node_count = int(metadata.get("node_count") or len(graph_data.get("nodes") or []))
    edge_count = int(metadata.get("edge_count") or len(graph_data.get("edges") or []))
    cluster_count = int(clusters.get("count") or len(clusters.get("clusters") or []))
    god_node_count = len(analysis.get("god_nodes") or [])
    surprising_count = len(analysis.get("surprising_connections") or [])
    top_link_count = len(analysis.get("top_files_by_links") or [])

    signals: list[str] = []
    if node_count >= 250:
        signals.append("node_count>=250")
    if edge_count >= 350:
        signals.append("edge_count>=350")
    if cluster_count >= 8:
        signals.append("cluster_count>=8")
    if god_node_count >= 2:
        signals.append("god_nodes>=2")
    if surprising_count >= 2:
        signals.append("surprising_connections>=2")
    if top_link_count >= 10 and edge_count >= 200:
        signals.append("high_linked_files")

    provider = cfg.llm_provider.strip().lower().replace("_", "-")
    base_url_ready = bool(cfg.llm_base_url) or _provider_has_default_base_url(provider) or provider in {"ollama", "local"}
    api_key_ready = bool(cfg.llm_api_key) or not _provider_requires_api_key(provider)
    provider_ready = base_url_ready and api_key_ready

    decision = {
        "enabled": bool(signals) and provider_ready,
        "signals": signals,
        "provider": cfg.llm_provider,
        "model": cfg.llm_model or _default_model(cfg.llm_provider),
        "effective_mode": _auto_effective_mode(cfg.llm_provider),
        "node_count": node_count,
        "edge_count": edge_count,
        "cluster_count": cluster_count,
        "god_node_count": god_node_count,
        "surprising_connection_count": surprising_count,
        "provider_ready": provider_ready,
    }
    if not signals:
        decision["reason"] = "graph_below_auto_threshold"
    elif not base_url_ready:
        decision["reason"] = "missing_llm_base_url"
    elif not api_key_ready:
        decision["reason"] = "missing_llm_api_key"
    else:
        decision["reason"] = "auto_threshold_met"
    return decision

def enrich_report(
    graph_data: dict[str, Any],
    clusters: dict[str, Any],
    analysis: dict[str, Any],
    cfg: Config,
) -> dict[str, Any]:
    """Return optional LLM report enrichment without making LLM mandatory for builds."""
    mode = cfg.llm_mode.strip().lower()
    if mode in {"", "0", "false", "none", "off", "disabled"}:
        return {"enabled": False, "mode": "none", "tokens": 0}

    auto_decision: dict[str, Any] | None = None
    effective_cfg = cfg
    if mode == "auto":
        auto_decision = decide_auto_llm(graph_data, clusters, analysis, cfg)
        if not auto_decision["enabled"]:
            return {
                "enabled": False,
                "status": "skipped",
                "mode": "auto",
                "tokens": 0,
                "provider": cfg.llm_provider,
                "model": auto_decision["model"],
                "reason": auto_decision["reason"],
                "auto": auto_decision,
            }
        effective_cfg = replace(cfg, llm_mode=str(auto_decision["effective_mode"]))

    try:
        provider = make_provider(effective_cfg)
        system, user = build_report_prompt(graph_data, clusters, analysis, effective_cfg)
        completion = provider.complete(system, user)
        return {
            "enabled": True,
            "status": "ok",
            "mode": mode,
            "effective_mode": effective_cfg.llm_mode,
            "provider": provider.name,
            "model": cfg.llm_model or _default_model(cfg.llm_provider),
            "summary": completion.text,
            "auto": auto_decision,
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
            "tokens": completion.total_tokens or completion.input_tokens + completion.output_tokens,
        }
    except Exception as exc:  # Optional enrichment must not break deterministic graph builds.
        return {
            "enabled": True,
            "status": "error",
            "mode": mode,
            "effective_mode": effective_cfg.llm_mode,
            "provider": cfg.llm_provider,
            "model": cfg.llm_model or _default_model(cfg.llm_provider),
            "auto": auto_decision,
            "tokens": 0,
            "error": _sanitize_error(exc, cfg),
        }


def build_report_prompt(
    graph_data: dict[str, Any],
    clusters: dict[str, Any],
    analysis: dict[str, Any],
    cfg: Config,
) -> tuple[str, str]:
    """Build a bounded prompt from graph metadata only, not source code."""
    payload = {
        "metadata": graph_data.get("metadata", {}),
        "top_files_by_links": analysis.get("top_files_by_links", [])[:12],
        "god_nodes": analysis.get("god_nodes", [])[:12],
        "entry_points": analysis.get("entry_points", [])[:12],
        "surprising_connections": analysis.get("surprising_connections", [])[:12],
        "clusters": [
            {
                "id": c.get("id"),
                "size": c.get("size"),
                "file_count": c.get("file_count"),
                "function_count": c.get("function_count"),
                "class_count": c.get("class_count"),
                "labels": c.get("labels", []),
            }
            for c in clusters.get("clusters", [])[:12]
        ],
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(data) > cfg.llm_max_input_chars:
        data = data[: cfg.llm_max_input_chars] + "\n... truncated by GRAPHITE_LLM_MAX_INPUT_CHARS"
    system = (
        "You are Graphite's optional code-graph analyst. Use only the provided graph metrics. "
        "Do not claim to have inspected source code. Focus on architecture risks, likely hotspots, "
        "and practical next checks. Keep the answer concise."
    )
    user = (
        "Analyze this code graph summary. Return concise bullets under: Hotspots, Coupling risks, "
        "Suggested next checks.\n\n"
        f"{data}"
    )
    return system, user


def _urlopen_json(request: urllib.request.Request, timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec: opt-in user URL
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"LLM provider HTTP {exc.code}: {detail}") from exc
    return json.loads(raw)


def _default_openai_base_url(provider: str) -> str | None:
    normalized = provider.strip().lower().replace("_", "-")
    if normalized == "openai":
        return "https://api.openai.com/v1"
    if normalized in {"lmstudio", "lm-studio"}:
        return "http://localhost:1234/v1"
    if normalized == "vllm":
        return "http://localhost:8000/v1"
    if normalized == "openrouter":
        return "https://openrouter.ai/api/v1"
    if normalized == "groq":
        return "https://api.groq.com/openai/v1"
    return None


def _default_model(provider: str) -> str:
    normalized = provider.strip().lower().replace("_", "-")
    if normalized in {"ollama", "local"}:
        return "llama3.1"
    if normalized == "openrouter":
        return "moonshotai/kimi-k2.7-code"
    if normalized == "groq":
        return "llama-3.1-8b-instant"
    return "gpt-4o-mini"


def _sanitize_error(exc: Exception, cfg: Config) -> str:
    message = str(exc)
    if cfg.llm_api_key:
        message = message.replace(cfg.llm_api_key, "[redacted]")
    return message[:500]
