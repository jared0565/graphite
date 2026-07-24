"""Result-scaled counterfactual estimator for the savings display.

The numbers are ESTIMATES of what equivalent manual exploration would have
cost, scaled by what each graphite answer actually contained; they are never
measurements. ``methodology()`` prints the formula and constants next to any
report so the claim stays auditable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class SavingsModel:
    grep_tokens: int = 1000
    grep_seconds: int = 20
    read_seconds: int = 10
    file_token_cap: int = 2000


MODEL = SavingsModel()


def _num(value: Any, cast: type) -> Any:
    """Best-effort numeric coercion; schema-corrupt values count as zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return cast(0)
    return cast(value)


def estimate_entry(entry: dict[str, Any], model: SavingsModel = MODEL) -> dict[str, Any]:
    files = entry.get("files") or []
    count = len(files)
    grep_rounds = 1 + count // 10
    manual_tokens = grep_rounds * model.grep_tokens + sum(
        min(_num(f.get("bytes", 0), int) // 4, model.file_token_cap) for f in files if isinstance(f, dict)
    )
    manual_seconds = float(grep_rounds * model.grep_seconds + count * model.read_seconds)
    cost_tokens = _num(entry.get("output_bytes", 0), int) // 4
    cost_seconds = _num(entry.get("wall_ms", 0), float) / 1000.0
    return {
        "tokens_saved": max(0, manual_tokens - cost_tokens),
        "seconds_saved": max(0.0, manual_seconds - cost_seconds),
    }


def summarize(entries: Iterable[dict[str, Any]], model: SavingsModel = MODEL) -> dict[str, Any]:
    total_tokens = 0
    total_seconds = 0.0
    count = 0
    by_cmd: dict[str, dict[str, Any]] = {}
    for entry in entries:
        est = estimate_entry(entry, model)
        cmd = str(entry.get("cmd", "unknown"))
        bucket = by_cmd.setdefault(cmd, {"count": 0, "tokens_saved": 0, "seconds_saved": 0.0})
        bucket["count"] += 1
        bucket["tokens_saved"] += est["tokens_saved"]
        bucket["seconds_saved"] += est["seconds_saved"]
        total_tokens += est["tokens_saved"]
        total_seconds += est["seconds_saved"]
        count += 1
    return {
        "count": count,
        "tokens_saved": total_tokens,
        "seconds_saved": total_seconds,
        "by_cmd": by_cmd,
    }


def methodology(model: SavingsModel = MODEL) -> str:
    return (
        "All figures are estimates of avoided manual exploration, never measurements. "
        f"Model: an answer covering K files ~ (1 + K//10) grep rounds x {model.grep_tokens} tokens "
        f"/ {model.grep_seconds}s each, plus per-file read of min(bytes/4, {model.file_token_cap}) tokens "
        f"/ {model.read_seconds}s; minus graphite's actual cost (output bytes/4 tokens, measured wall time); "
        "floored at zero."
    )


def format_compact(tokens: int, seconds: float) -> str:
    token_text = f"~{tokens / 1000:.1f}k tokens" if tokens >= 1000 else f"~{tokens} tokens"
    time_text = f"~{seconds / 60:.0f} min" if seconds >= 90 else f"~{seconds:.0f}s"
    return f"{token_text} / {time_text}"
