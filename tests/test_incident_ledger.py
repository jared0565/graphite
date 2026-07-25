"""Incident ledger: fingerprints, rotation, fold semantics, fail-open writes."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.incident_ledger import (
    append_lifecycle,
    fold_incidents,
    incident_fingerprint,
    read_incident_entries,
    record_incident,
    repo_ledger_dir,
)


def _record(d: Path, code: str = "parse_error", subject: str = "src/a.py", detail: str = "boom") -> str:
    record_incident(d, klass="build", code=code, subject=subject, detail=detail)
    return incident_fingerprint("build", code, subject)


def test_fingerprint_stable_and_detail_independent():
    a = incident_fingerprint("build", "parse_error", "src/a.py")
    b = incident_fingerprint("build", "parse_error", "src/a.py")
    c = incident_fingerprint("build", "parse_error", "src/b.py")
    assert a == b and a != c and len(a) == 16


def test_record_and_read_roundtrip(tmp_path):
    fp = _record(tmp_path)
    entries, skipped = read_incident_entries(tmp_path)
    assert skipped == 0 and len(entries) == 1
    e = entries[0]
    assert e["schema"] == 1 and e["kind"] == "occurrence"
    assert e["fingerprint"] == fp and e["class"] == "build"
    assert e["code"] == "parse_error" and e["subject"] == "src/a.py"
    assert e["ts"].endswith("Z")


def test_caps_applied(tmp_path):
    record_incident(tmp_path, klass="build", code="parse_error", subject="s" * 999, detail="d" * 9999)
    entries, _ = read_incident_entries(tmp_path)
    assert len(entries[0]["subject"]) == 512
    assert len(entries[0]["detail"]) <= 2048


def test_fold_open_ack_resolve_reopen(tmp_path):
    fp = _record(tmp_path)
    assert fold_incidents(read_incident_entries(tmp_path)[0])[0].state == "open"
    assert append_lifecycle(tmp_path, fp, "ack", note="known") is True
    view = fold_incidents(read_incident_entries(tmp_path)[0])[0]
    assert view.state == "acked" and view.last_note == "known"
    _record(tmp_path)  # newer occurrence: ack STAYS acked
    assert fold_incidents(read_incident_entries(tmp_path)[0])[0].state == "acked"
    assert append_lifecycle(tmp_path, fp, "resolve") is True
    assert fold_incidents(read_incident_entries(tmp_path)[0])[0].state == "resolved"


def test_reopen_after_resolve(tmp_path, monkeypatch):
    import graphite.incident_ledger as il

    times = iter(["2026-07-25T10:00:00Z", "2026-07-25T10:00:01Z", "2026-07-25T10:00:02Z"])
    monkeypatch.setattr(il, "_now", lambda: next(times))
    fp = _record(tmp_path)
    append_lifecycle(tmp_path, fp, "resolve")
    _record(tmp_path)  # strictly newer than the resolve
    view = fold_incidents(read_incident_entries(tmp_path)[0])[0]
    assert view.state == "open" and view.count == 2


def test_lifecycle_unknown_fingerprint_refused(tmp_path):
    assert append_lifecycle(tmp_path, "deadbeefdeadbeef", "ack") is False
    entries, _ = read_incident_entries(tmp_path)
    assert entries == []


def test_corrupt_lines_skipped_and_counted(tmp_path):
    fp = _record(tmp_path)
    path = tmp_path / "incidents.jsonl"
    with open(path, "a", encoding="utf-8") as h:
        h.write("{not json\n")
        h.write(json.dumps({"schema": 1, "kind": "occurrence"}) + "\n")  # no fingerprint
    entries, skipped = read_incident_entries(tmp_path)
    assert len(entries) == 1 and entries[0]["fingerprint"] == fp
    assert skipped == 2


def test_rotation_at_cap(tmp_path, monkeypatch):
    import graphite.incident_ledger as il

    monkeypatch.setattr(il, "MAX_LEDGER_BYTES", 200)
    for i in range(20):
        _record(tmp_path, subject=f"src/f{i}.py")
    assert (tmp_path / "incidents.jsonl.1").exists()
    entries, _ = read_incident_entries(tmp_path)
    # Single-generation rotation DROPS older generations by design (the
    # usage-ledger idiom): only the rotated file + current file survive.
    assert 2 <= len(entries) < 20
    assert entries[-1]["subject"] == "src/f19.py"  # newest entry always present


def test_record_is_fail_open(tmp_path):
    blocker = tmp_path / "local"
    blocker.write_text("not a directory", encoding="utf-8")
    record_incident(blocker / "sub", klass="build", code="parse_error", subject="x", detail="y")
    # no exception is the assertion


def test_repo_ledger_dir_matches_usage_idiom(tmp_path):
    assert repo_ledger_dir(tmp_path) == tmp_path / ".graphite" / "local"


def test_sort_order_open_first(tmp_path):
    a = _record(tmp_path, subject="src/a.py")
    _record(tmp_path, subject="src/b.py")
    append_lifecycle(tmp_path, a, "resolve")
    views = fold_incidents(read_incident_entries(tmp_path)[0])
    assert [v.state for v in views] == ["open", "resolved"]
