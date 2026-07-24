"""Tests for the machine-local usage ledger (never-fatal by contract)."""
from __future__ import annotations

import json
from pathlib import Path

from graphite import usage_ledger
from graphite.usage_ledger import (
    collect_answer_files,
    iter_entries,
    ledger_path,
    read_cursor,
    record_usage,
    savings_display_enabled,
    set_savings_display,
    write_cursor,
)


def _entries(root: Path) -> list[dict]:
    return list(iter_entries(root))


def test_record_usage_appends_entry_with_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n" * 100, encoding="utf-8")
    result = {"results": [{"id": "a.py", "source_file": "a.py"}], "ok": True}

    record_usage(tmp_path, cmd="search", wall_ms=42, result=result)
    entries = _entries(tmp_path)

    assert len(entries) == 1
    entry = entries[0]
    assert entry["cmd"] == "search"
    assert entry["wall_ms"] == 42
    assert entry["output_bytes"] == len(json.dumps(result))
    assert entry["files"] == [{"path": "a.py", "bytes": (tmp_path / "a.py").stat().st_size}]
    assert "ts" in entry


def test_collect_answer_files_walks_nested_and_list_keys(tmp_path: Path) -> None:
    for name in ("x.py", "y.py", "t.py"):
        (tmp_path / name).write_text("pass\n", encoding="utf-8")
    result = {
        "context": [{"file": "x.py", "neighbors": [{"source_file": "y.py"}]}],
        "impacted_files": ["x.py"],
        "likely_tests": ["t.py"],
        "missing": [],
    }

    files = collect_answer_files(result, tmp_path)

    assert [f["path"] for f in files] == ["x.py", "y.py", "t.py"]  # deduped, order-preserving


def test_collect_answer_files_caps_and_tolerates_missing(tmp_path: Path) -> None:
    result = {"impacted_files": [f"gone{i}.py" for i in range(150)]}
    files = collect_answer_files(result, tmp_path)
    assert len(files) == usage_ledger.MAX_FILES_PER_ENTRY
    assert all(f["bytes"] == 0 for f in files)  # stat failures -> 0, never raise


def test_record_usage_rotates_at_cap(tmp_path: Path, monkeypatch) -> None:
    from graphite.usage_ledger import rotated_ledger_path

    monkeypatch.setattr(usage_ledger, "MAX_LEDGER_BYTES", 200)
    for i in range(20):
        record_usage(tmp_path, cmd="query", wall_ms=i, result={"r": "x" * 50})

    assert ledger_path(tmp_path).exists()
    assert rotated_ledger_path(tmp_path).exists()
    assert _entries(tmp_path)  # reader sees rotated + current, in order


def test_iter_entries_skips_corrupt_lines(tmp_path: Path) -> None:
    record_usage(tmp_path, cmd="query", wall_ms=1, result={})
    with open(ledger_path(tmp_path), "a", encoding="utf-8") as f:
        f.write("{corrupt\n")
    record_usage(tmp_path, cmd="impact", wall_ms=2, result={})

    assert [e["cmd"] for e in _entries(tmp_path)] == ["query", "impact"]


def test_record_usage_never_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(usage_ledger, "ledger_path", lambda root: 1 / 0)
    record_usage(tmp_path, cmd="query", wall_ms=1, result={})  # must not raise


def test_savings_display_toggle_default_on(tmp_path: Path) -> None:
    assert savings_display_enabled(tmp_path) is True
    out = set_savings_display(tmp_path, False)
    assert out["savings_display"] is False
    assert savings_display_enabled(tmp_path) is False
    set_savings_display(tmp_path, True)
    assert savings_display_enabled(tmp_path) is True


def test_cursor_roundtrip_and_default(tmp_path: Path) -> None:
    assert read_cursor(tmp_path) == {}
    write_cursor(tmp_path, {"sessions": {"s1": {"offset": 10}}})
    assert read_cursor(tmp_path) == {"sessions": {"s1": {"offset": 10}}}
