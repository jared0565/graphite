"""Isolated synthetic LLM connectivity worker with fixed-schema output."""
from __future__ import annotations

import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypedDict

if __package__ in {None, ""}:
    source = Path(__file__).resolve(strict=True)
    source_root = source.parent.parent
    sys.path.insert(0, str(source_root))
    import graphite
    from graphite import llm_probe as trusted_worker

    if (
        Path(graphite.__file__).resolve(strict=True) != source.parent / "__init__.py"
        or Path(trusted_worker.__file__).resolve(strict=True) != source
    ):
        raise SystemExit(70)
    raise SystemExit(trusted_worker.main())

from .config import Config
from .llm import (
    CompletionProvider,
    LLMConfigurationError,
    LLMProviderError,
    ProviderErrorCategory,
    make_provider,
)

SYSTEM_PROMPT = "You are a connectivity probe. Reply with READY only."
USER_PROMPT = "Synthetic Graphite connectivity test. No repository data is included."
WORKER_INPUT_LIMIT_BYTES = 16 * 1024
_INPUT_KEYS = frozenset(
    {
        "mode",
        "provider",
        "model",
        "base_url",
        "api_key",
        "timeout_seconds",
        "seed",
        "system",
        "user",
    }
)


class ReadyProbeResult(TypedDict):
    status: Literal["ready"]
    response_present: Literal[True]


class DegradedProbeResult(TypedDict):
    status: Literal["degraded"]
    category: ProviderErrorCategory


ProbeResult = ReadyProbeResult | DegradedProbeResult
ProviderFactory = Callable[[Config], CompletionProvider]


def _failure(category: ProviderErrorCategory) -> DegradedProbeResult:
    return {"status": "degraded", "category": category}


def _classify_exception(exc: Exception) -> ProviderErrorCategory:
    if isinstance(exc, LLMConfigurationError):
        return "configuration"
    if isinstance(exc, LLMProviderError):
        return exc.category
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (ConnectionError, OSError)):
        return "connection"
    return "provider_error"


def run_synthetic_probe(
    cfg: Config,
    *,
    provider_factory: ProviderFactory = make_provider,
) -> ProbeResult:
    """Make exactly one constant-content completion and discard the response text."""
    try:
        provider = provider_factory(cfg)
        completion = provider.complete(SYSTEM_PROMPT, USER_PROMPT)
        text = completion.text
    except Exception as exc:
        return _failure(_classify_exception(exc))
    if not isinstance(text, str) or not text.strip():
        return _failure("provider_error")
    return {"status": "ready", "response_present": True}


def _optional_string(value: object, *, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError
    return value


def _config_from_payload(payload: object) -> Config:
    if not isinstance(payload, dict) or set(payload) != _INPUT_KEYS:
        raise ValueError
    mode = payload["mode"]
    provider = payload["provider"]
    timeout = payload["timeout_seconds"]
    seed = payload["seed"]
    if (
        not isinstance(mode, str)
        or len(mode) > 16
        or mode.strip().lower() not in {"auto", "local", "cloud"}
        or not isinstance(provider, str)
        or len(provider) > 128
        or not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > 60
        or not isinstance(seed, int)
        or isinstance(seed, bool)
        or payload["system"] != SYSTEM_PROMPT
        or payload["user"] != USER_PROMPT
    ):
        raise ValueError
    return Config(
        llm_mode=mode,
        llm_provider=provider,
        llm_model=_optional_string(payload["model"], limit=512),
        llm_base_url=_optional_string(payload["base_url"], limit=2048),
        llm_api_key=_optional_string(payload["api_key"], limit=4096),
        llm_timeout_seconds=float(timeout),
        seed=seed,
    )


def main() -> int:
    """Read bounded configuration from stdin and emit only fixed-schema JSON."""
    try:
        raw = sys.stdin.buffer.read(WORKER_INPUT_LIMIT_BYTES + 1)
        if len(raw) > WORKER_INPUT_LIMIT_BYTES:
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
        result = run_synthetic_probe(_config_from_payload(payload))
    except Exception:
        result = _failure("configuration")
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    return 0
