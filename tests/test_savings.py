"""Tests for the result-scaled savings estimator."""
from __future__ import annotations

import json as _json
from pathlib import Path

from graphite.cli import main
from graphite.savings import MODEL, estimate_entry, format_compact, methodology, summarize
from graphite.usage_ledger import record_usage


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


def _seed(root: Path) -> None:
    record_usage(root, cmd="context", wall_ms=50, result={"files": [{"path": "a.py"}]})
    record_usage(root, cmd="impact", wall_ms=30, result={"impacted_files": ["a.py"]})


def test_savings_report_json(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)

    assert main(["savings", "--json"]) == 0
    payload = _json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert payload["all_time"]["count"] == 2
    assert set(payload["all_time"]["by_cmd"]) == {"context", "impact"}
    assert payload["today"]["count"] == 2
    assert "estimate" in payload["methodology"]
    assert payload["savings_display"] is True


def test_savings_report_human_has_methodology(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)

    assert main(["savings"]) == 0
    out = capsys.readouterr().out

    assert "estimates" in out.lower()
    assert "all-time" in out.lower()


def test_savings_toggle_roundtrip(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["savings", "off"]) == 0
    capsys.readouterr()
    assert main(["savings", "status", "--json"]) == 0
    status = _json.loads(capsys.readouterr().out)
    assert status["savings_display"] is False
    assert main(["savings", "on"]) == 0


def test_savings_report_works_when_toggle_off(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)
    main(["savings", "off"])
    capsys.readouterr()

    assert main(["savings", "--json"]) == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["all_time"]["count"] == 2


def test_savings_rejects_llm_flags(capsys) -> None:
    assert main(["--llm", "cloud", "savings"]) == 2


def test_capabilities_does_not_list_savings(capsys) -> None:
    assert main(["capabilities", "--json"]) == 0
    payload = _json.loads(capsys.readouterr().out)
    assert "savings" not in payload["commands"]
