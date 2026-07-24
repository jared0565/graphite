"""Tests for the result-scaled savings estimator."""
from __future__ import annotations

from graphite.savings import MODEL, estimate_entry, format_compact, methodology, summarize


def _entry(cmd: str = "context", files: int = 0, file_bytes: int = 4000, wall_ms: int = 100, output_bytes: int = 400) -> dict:
    return {
        "cmd": cmd,
        "wall_ms": wall_ms,
        "output_bytes": output_bytes,
        "files": [{"path": f"f{i}.py", "bytes": file_bytes} for i in range(files)],
    }


def test_zero_file_answer_still_counts_grep_round_baseline() -> None:
    est = estimate_entry(_entry(files=0, wall_ms=0, output_bytes=0))
    assert est["tokens_saved"] == MODEL.grep_tokens  # 1 round, no reads, zero cost
    assert est["seconds_saved"] == float(MODEL.grep_seconds)


def test_estimate_scales_with_files_and_caps_per_file() -> None:
    est = estimate_entry(_entry(files=12, file_bytes=100_000, wall_ms=0, output_bytes=0))
    # 12 files -> grep_rounds = 1 + 12//10 = 2; each file capped at file_token_cap
    assert est["tokens_saved"] == 2 * MODEL.grep_tokens + 12 * MODEL.file_token_cap
    assert est["seconds_saved"] == 2 * MODEL.grep_seconds + 12 * MODEL.read_seconds


def test_estimate_floors_at_zero_when_graphite_cost_exceeds_manual() -> None:
    est = estimate_entry(_entry(files=0, wall_ms=10_000_000, output_bytes=10_000_000))
    assert est["tokens_saved"] == 0
    assert est["seconds_saved"] == 0.0


def test_summarize_totals_and_by_cmd() -> None:
    entries = [_entry(cmd="context", files=2), _entry(cmd="impact", files=1), _entry(cmd="context", files=0)]
    summary = summarize(entries)
    assert summary["count"] == 3
    assert set(summary["by_cmd"]) == {"context", "impact"}
    assert summary["by_cmd"]["context"]["count"] == 2
    assert summary["tokens_saved"] == sum(v["tokens_saved"] for v in summary["by_cmd"].values())


def test_methodology_names_constants_and_estimate_label() -> None:
    text = methodology()
    for token in ("1000", "20", "10", "2000", "estimate"):
        assert token in text


def test_format_compact_units() -> None:
    assert format_compact(12_345, 250.0) == "~12.3k tokens / ~4 min"
    assert format_compact(900, 45.0) == "~900 tokens / ~45s"
