# Incident Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Durable, deduplicated, machine-readable capture of graphite failures (build/extract errors, malformed artifacts, graph-load failures, inconclusive queries, daemon incidents) with an event-sourced triage lifecycle, surfaced via `graphite incidents`, doctor, and daemon-health.

**Architecture:** New `incident_ledger.py` mirroring the `usage_ledger.py` JSONL idiom (append-only, 5 MB rotation, fail-open writes, read-time dedup by fingerprint). Writers hook five existing choke points; readers fold entries into per-fingerprint views (open/acked/resolved, reopen-on-occurrence-after-resolve). CLI/doctor/daemon-health render the folded views. No inference anywhere.

**Tech Stack:** Python 3.11+ stdlib only (hashlib, json, os, datetime). No new dependencies. pytest.

**Spec:** `docs/superpowers/specs/2026-07-25-incident-ledger-design.md` (commit f673ec1). Base: main `2776452`.

## Global Constraints

- Ledger paths follow the ACTUAL usage-ledger idiom: per-repo `<root>/.graphite/local/incidents.jsonl`, rotated sibling `incidents.jsonl.1` (rotation: `os.replace` when size > `MAX_LEDGER_BYTES` = 5 * 1024 * 1024); daemon-global `<daemon base>/.graphite-daemon/incidents.jsonl`. (The spec's §3 shorthand `.graphite/incidents.jsonl` / `.rotated.jsonl` is superseded by its own "reuse `usage_ledger.local_dir`" sentence — Task 1 amends the spec line.)
- Caps, verbatim: subject 512 chars, detail/note 2048 chars, line 8 KB (`8192` bytes, detail truncated further to fit), file 5 MB + one rotated generation.
- Fingerprint: first 16 hex chars of SHA-256 over `f"{class}|{code}|{subject}"`. `detail`/timestamps never enter the fingerprint.
- Entry kinds: `occurrence | ack | resolve`. Classes: `build | query | daemon`. Codes (closed set): `parse_error, read_error, worker_error, artifact_malformed, graph_load_failed, query_inconclusive, daemon_build_failed, provider_probe_failed`. `artifact_malformed`/`graph_load_failed` carry class `build`.
- Fold semantics: state = latest lifecycle entry; EXCEPT an occurrence strictly newer (`>` on ISO-Z timestamps) than a `resolve` reopens to `open`. An `ack` stays `acked` under newer occurrences. Sort: open, then acked, then resolved; within a state, most recent `last_seen` first.
- `record_incident` is fail-open: NEVER raises, whatever the I/O condition. Every capture hook must be unable to break the operation it records. `append_lifecycle` (interactive) may raise on I/O but returns False without writing for unknown fingerprints.
- No inference / no LLM anywhere (Canonical Graph Isolation). No cache-format or cache-version change (`ExtractionResult.errors` is merge-time only, never serialized to the extraction cache).
- Tests run in the worktree: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest <target> -q` (bash). NEVER `pip install -e` from the worktree. No background tasks; no full suite inside a task (the completion gate runs it).
- Fixture-test convention: real files under `tmp_path`, real pipeline (`collect_files` → `extract_all` / `_build_project`), no parser mocks; `Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite")`.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Ruff clean on touched files.
- Where a step says "match the local convention", the named file's existing code is authoritative for argument/variable names — adapt the shown code to it, never the reverse; the TESTS define correctness.

---

### Task 1: `incident_ledger.py` module + unit tests

**Files:**
- Create: `src/graphite/incident_ledger.py`
- Create: `tests/test_incident_ledger.py`
- Modify: `docs/superpowers/specs/2026-07-25-incident-ledger-design.md` (§3 path lines)

**Interfaces:**
- Produces (all consumed by Tasks 2–7):
  - `repo_ledger_dir(root: Path) -> Path`
  - `record_incident(ledger_dir: Path, *, klass: str, code: str, subject: str, detail: str) -> None`
  - `append_lifecycle(ledger_dir: Path, fingerprint: str, kind: str, note: str | None = None) -> bool`
  - `read_incident_entries(ledger_dir: Path) -> tuple[list[dict], int]`
  - `fold_incidents(entries: list[dict]) -> list[IncidentView]` (IncidentView fields: `fingerprint, klass, code, subject, state, first_seen, last_seen, count, last_detail, last_note`; method `to_json() -> dict` with key `"class"` for `klass`)
  - `incident_fingerprint(klass: str, code: str, subject: str) -> str`
  - Constants: `MAX_LEDGER_BYTES`, `MAX_SUBJECT_CHARS = 512`, `MAX_DETAIL_CHARS = 2048`, `MAX_LINE_BYTES = 8192`

- [ ] **Step 1: Write the failing tests** — create `tests/test_incident_ledger.py`:

```python
"""Incident ledger: fingerprints, rotation, fold semantics, fail-open writes."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.incident_ledger import (
    MAX_LEDGER_BYTES,
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
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest tests/test_incident_ledger.py -q`
Expected: FAIL — `ModuleNotFoundError: graphite.incident_ledger`

- [ ] **Step 3: Implement** — create `src/graphite/incident_ledger.py`:

```python
"""Machine-local incident ledger: durable capture of graphite failures.

Append-only JSONL, one file per repo (``.graphite/local/incidents.jsonl``,
the usage-ledger idiom) plus one for the daemon's own state dir. Dedup is a
READ-time concern: occurrences append freely and ``fold_incidents`` groups
them by fingerprint. Triage is event-sourced: ``ack``/``resolve`` are
appended entries, never mutations; a new occurrence strictly newer than a
resolve reopens the incident. Recording is best-effort by contract — a
ledger write failure must never break the operation being recorded.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_LEDGER_BYTES = 5 * 1024 * 1024
MAX_SUBJECT_CHARS = 512
MAX_DETAIL_CHARS = 2048
MAX_LINE_BYTES = 8192
_CLASSES = frozenset({"build", "query", "daemon"})
_LIFECYCLE_KINDS = frozenset({"ack", "resolve"})
_KINDS = frozenset({"occurrence"}) | _LIFECYCLE_KINDS
_STATE_ORDER = {"open": 0, "acked": 1, "resolved": 2}


def repo_ledger_dir(root: Path) -> Path:
    from .usage_ledger import local_dir

    return local_dir(root)


def ledger_path(ledger_dir: Path) -> Path:
    return ledger_dir / "incidents.jsonl"


def rotated_ledger_path(ledger_dir: Path) -> Path:
    return ledger_dir / "incidents.jsonl.1"


def incident_fingerprint(klass: str, code: str, subject: str) -> str:
    digest = hashlib.sha256(f"{klass}|{code}|{subject}".encode("utf-8")).hexdigest()
    return digest[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _append(ledger_dir: Path, entry: dict[str, Any]) -> None:
    path = ledger_path(ledger_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_LEDGER_BYTES:
        os.replace(path, rotated_ledger_path(ledger_dir))
    line = json.dumps(entry, ensure_ascii=False)
    overshoot = len(line.encode("utf-8")) - MAX_LINE_BYTES
    if overshoot > 0 and "detail" in entry:
        detail = str(entry["detail"])
        entry = {**entry, "detail": detail[: max(0, len(detail) - overshoot)]}
        line = json.dumps(entry, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def record_incident(ledger_dir: Path, *, klass: str, code: str, subject: str, detail: str) -> None:
    """Append one occurrence; never raise."""
    try:
        if klass not in _CLASSES:
            return
        subject = str(subject)[:MAX_SUBJECT_CHARS]
        _append(
            ledger_dir,
            {
                "schema": 1,
                "kind": "occurrence",
                "fingerprint": incident_fingerprint(klass, str(code), subject),
                "ts": _now(),
                "class": klass,
                "code": str(code),
                "subject": subject,
                "detail": str(detail)[:MAX_DETAIL_CHARS],
            },
        )
    except Exception:
        return


def append_lifecycle(ledger_dir: Path, fingerprint: str, kind: str, note: str | None = None) -> bool:
    """Append ack/resolve for a known fingerprint; False (no write) if unknown."""
    if kind not in _LIFECYCLE_KINDS:
        raise ValueError("kind must be 'ack' or 'resolve'")
    entries, _skipped = read_incident_entries(ledger_dir)
    known = any(e.get("kind") == "occurrence" and e.get("fingerprint") == fingerprint for e in entries)
    if not known:
        return False
    entry: dict[str, Any] = {"schema": 1, "kind": kind, "fingerprint": fingerprint, "ts": _now()}
    if note:
        entry["note"] = str(note)[:MAX_DETAIL_CHARS]
    _append(ledger_dir, entry)
    return True


def read_incident_entries(ledger_dir: Path) -> tuple[list[dict[str, Any]], int]:
    """(entries, skipped): rotated generation first; corrupt lines counted; never raises."""
    entries: list[dict[str, Any]] = []
    skipped = 0
    try:
        for path in (rotated_ledger_path(ledger_dir), ledger_path(ledger_dir)):
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                if len(line.encode("utf-8", errors="replace")) > MAX_LINE_BYTES:
                    skipped += 1
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("fingerprint"), str)
                    and entry.get("kind") in _KINDS
                ):
                    entries.append(entry)
                else:
                    skipped += 1
    except Exception:
        return entries, skipped
    return entries, skipped


@dataclass(frozen=True)
class IncidentView:
    fingerprint: str
    klass: str
    code: str
    subject: str
    state: str
    first_seen: str
    last_seen: str
    count: int
    last_detail: str
    last_note: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "class": self.klass,
            "code": self.code,
            "subject": self.subject,
            "state": self.state,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "count": self.count,
            "last_detail": self.last_detail,
            "last_note": self.last_note,
        }


def fold_incidents(entries: list[dict[str, Any]]) -> list[IncidentView]:
    """Group chronologically-ordered entries into per-fingerprint views."""
    slots: dict[str, dict[str, Any]] = {}
    for e in entries:
        fp = str(e["fingerprint"])
        slot = slots.setdefault(
            fp,
            {
                "first": None,
                "last": "",
                "count": 0,
                "klass": "",
                "code": "",
                "subject": "",
                "detail": "",
                "life": None,
                "life_ts": "",
                "note": None,
            },
        )
        ts = str(e.get("ts", ""))
        if e.get("kind") == "occurrence":
            slot["count"] += 1
            slot["klass"] = str(e.get("class", ""))
            slot["code"] = str(e.get("code", ""))
            slot["subject"] = str(e.get("subject", ""))
            slot["detail"] = str(e.get("detail", ""))
            if slot["first"] is None:
                slot["first"] = ts
            slot["last"] = ts
        else:
            slot["life"] = e["kind"]
            slot["life_ts"] = ts
            if e.get("note") is not None:
                slot["note"] = str(e["note"])
    views: list[IncidentView] = []
    for fp, s in slots.items():
        if s["count"] == 0:
            continue
        if s["life"] is None:
            state = "open"
        elif s["life"] == "resolve":
            state = "open" if s["last"] > s["life_ts"] else "resolved"
        else:
            state = "acked"
        views.append(
            IncidentView(
                fingerprint=fp,
                klass=s["klass"],
                code=s["code"],
                subject=s["subject"],
                state=state,
                first_seen=s["first"] or "",
                last_seen=s["last"],
                count=s["count"],
                last_detail=s["detail"],
                last_note=s["note"],
            )
        )
    views.sort(key=lambda v: v.last_seen, reverse=True)
    views.sort(key=lambda v: _STATE_ORDER[v.state])
    return views
```

- [ ] **Step 4: Amend the spec's path lines** — in `docs/superpowers/specs/2026-07-25-incident-ledger-design.md` §3, replace the per-repo path sentence so it reads: per-repo ledger at `` `local_dir(root)/incidents.jsonl` `` → `` `.graphite/local/incidents.jsonl` `` with rotation to `` `incidents.jsonl.1` `` (matching `usage_ledger`'s actual layout: `usage.jsonl` / `usage.jsonl.1` under `.graphite/local/`). Do not change anything else in the spec.

- [ ] **Step 5: Run to verify pass**

Run: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest tests/test_incident_ledger.py -q`
Expected: all PASS

- [ ] **Step 6: Ruff + commit**

```bash
cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m ruff check src/graphite/incident_ledger.py tests/test_incident_ledger.py
git add src/graphite/incident_ledger.py tests/test_incident_ledger.py docs/superpowers/specs/2026-07-25-incident-ledger-design.md
git commit -m "feat(incidents): append-only incident ledger with event-sourced triage

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Extraction-error plumbing + build writer

**Files:**
- Modify: `src/graphite/extract/ast.py` (`ExtractionResult` dataclass ~line 18-24; `extract_all` ~line 1009-1028)
- Modify: `src/graphite/cli.py` (`_build`, after the `extract_all` call at ~line 200)
- Test: `tests/test_incident_ledger.py` (append)

**Interfaces:**
- Consumes: `record_incident`, `repo_ledger_dir`, `read_incident_entries` (Task 1).
- Produces: `ExtractionResult.errors: list[dict]` — each `{"code": str, "subject": rel_path, "detail": str}` — populated ONLY by `extract_all` on the merged result (never serialized to the extraction cache; per-file `.error` strings and `_merge`'s legacy `.error` behavior unchanged).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_incident_ledger.py`:

```python
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_extract_all_collects_per_file_errors(tmp_path):
    from graphite.config import Config
    from graphite.extract.ast import extract_all
    from graphite.ingest import collect_files

    _write(tmp_path / "good.py", "def ok():\n    return 1\n")
    _write(tmp_path / "bad.py", "def broken(:\n")
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite")
    entries = collect_files(tmp_path, cfg)
    result = extract_all(entries, cfg)
    # NOTE: tree-sitter is error-tolerant and may parse bad.py without raising.
    # If result.errors is empty here, replace bad.py's content with a file that
    # actually fails extraction (e.g. undecodable bytes for a read/parse error:
    # (tmp_path / "bad.py").write_bytes(b"\xff\xfe\x00broken")) and re-check —
    # the assertion below is about PLUMBING, not about which input breaks.
    assert isinstance(result.errors, list)
    if result.errors:
        err = result.errors[0]
        assert set(err) == {"code", "subject", "detail"}
        assert err["subject"] == "bad.py"


def test_build_records_extraction_incidents(tmp_path, monkeypatch):
    from graphite.cli import _build_project
    from graphite.config import Config

    _write(tmp_path / "src" / "ok.py", "def ok():\n    return 1\n")
    (tmp_path / "src" / "bad.py").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "bad.py").write_bytes(b"\xff\xfe\x00broken\x00")
    monkeypatch.chdir(tmp_path)
    _build_project(tmp_path, Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite"))
    entries, _ = read_incident_entries(repo_ledger_dir(tmp_path))
    build_entries = [e for e in entries if e["class"] == "build"]
    # If the undecodable file did not produce an extraction error either,
    # this test must be adapted with an input that verifiably errors (check
    # extract_file's read/parse paths) — do NOT weaken the assertion.
    assert any(e["subject"].endswith("bad.py") for e in build_entries)
    assert all(e["code"] in ("parse_error", "read_error", "worker_error") for e in build_entries)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest tests/test_incident_ledger.py -q`
Expected: FAIL — `ExtractionResult` has no attribute `errors`; no incidents recorded. IMPORTANT: first verify which fixture input actually produces a per-file extraction error (run a tiny probe: `extract_file` on the undecodable file) and pin the fixture accordingly; the tests carry adaptation notes for this. Report what the probe showed.

- [ ] **Step 3: Implement**

In `src/graphite/extract/ast.py`, add to the `ExtractionResult` dataclass (alongside the existing `error: str | None = None` field):

```python
    errors: list[dict[str, Any]] = field(default_factory=list)
```

(`field` is already imported for other dataclasses in the file; add the import if not.) Do NOT touch `_result_to_dict` / `_result_from_dict` — the cache format stays `nodes/edges/error`.

In `extract_all`, collect per-file errors with the entry in hand and attach them, sorted, to the merged result:

```python
def _error_record(rel_path: str, error: str) -> dict[str, Any]:
    code, _, _rest = error.partition(":")
    return {"code": code.strip() or "extract_error", "subject": rel_path, "detail": error}


def extract_all(entries: list[FileEntry], cfg: Config, cache: Cache | None = None) -> ExtractionResult:
    """Extract all files, optionally in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: list[ExtractionResult] = []
    error_records: list[dict[str, Any]] = []
    source_index = SourceIndex.from_entries(entries, cfg)
    if cfg.workers <= 1:
        for entry in entries:
            result = extract_file(entry, cfg, cache, source_index)
            if result.error:
                error_records.append(_error_record(entry.rel_path, result.error))
            results.append(result)
        merged = _merge(results)
        merged.errors = sorted(error_records, key=lambda r: r["subject"])
        return merged

    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futures = {pool.submit(extract_file, entry, cfg, cache, source_index): entry for entry in entries}
        for future in as_completed(futures):
            entry = futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = ExtractionResult(error=f"worker_error: {entry.rel_path}: {e}", nodes=[], edges=[])
            if result.error:
                error_records.append(_error_record(entry.rel_path, result.error))
            results.append(result)
    merged = _merge(results)
    merged.errors = sorted(error_records, key=lambda r: r["subject"])
    return merged
```

In `src/graphite/cli.py` `_build`, immediately after the `extract_all` call (~line 200), add (imports go at the top of cli.py: `from .incident_ledger import record_incident, repo_ledger_dir`):

```python
    if extraction.errors:
        _root = Path(args.path).resolve()
        _seen: set[tuple[str, str]] = set()
        for err in extraction.errors:
            key = (err["code"], err["subject"])
            if key in _seen:
                continue
            _seen.add(key)
            record_incident(
                repo_ledger_dir(_root),
                klass="build",
                code=err["code"],
                subject=err["subject"],
                detail=err["detail"],
            )
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest tests/test_incident_ledger.py tests/test_call_graph.py tests/test_smoke.py -q`
Expected: all PASS

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m ruff check src/graphite/extract/ast.py src/graphite/cli.py tests/test_incident_ledger.py
git add src/graphite/extract/ast.py src/graphite/cli.py tests/test_incident_ledger.py
git commit -m "feat(incidents): extraction errors surface as build incidents

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Artifact, graph-load, and inconclusive writers

**Files:**
- Modify: `src/graphite/health.py` (`persisted_resolution`, line ~111-123)
- Modify: `src/graphite/agent_hooks.py` (call site line ~273)
- Modify: `src/graphite/cli.py` (`cmd_check` call site ~line 404; `_load_graph` ~line 267-273; new `_record_inconclusive` helper next to `_record_canonical_usage` ~line 276; hooks at the three `_record_canonical_usage` call sites for query/impact/context: lines ~750, ~932, ~1042)
- Test: `tests/test_incident_ledger.py` (append) and `tests/test_health.py` (append)

**Interfaces:**
- Consumes: Task 1 API.
- Produces: `persisted_resolution(root, on_error=None)` — `on_error: Callable[[Exception], None] | None`, invoked ONLY for `(ValueError, RecursionError)` (malformed content), never for `OSError` (absence/unreadability is not malformation); the function still returns None in every failure case.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_health.py`:

```python
def test_persisted_resolution_on_error_fires_for_malformed(tmp_path):
    from graphite.health import persisted_resolution

    out = tmp_path / "graph-out"
    out.mkdir()
    (out / ".graphite_analysis.json").write_text("{not valid json", encoding="utf-8")
    seen: list[Exception] = []
    assert persisted_resolution(tmp_path, on_error=seen.append) is None
    assert len(seen) == 1 and isinstance(seen[0], ValueError)


def test_persisted_resolution_on_error_silent_for_missing(tmp_path):
    from graphite.health import persisted_resolution

    seen: list[Exception] = []
    assert persisted_resolution(tmp_path, on_error=seen.append) is None
    assert seen == []
```

Append to `tests/test_incident_ledger.py`:

```python
def test_check_records_artifact_malformed(tmp_path, capsys):
    import argparse

    from graphite.cli import cmd_check

    out = tmp_path / "graph-out"
    out.mkdir(parents=True)
    (out / ".graphite_analysis.json").write_text("{not valid json", encoding="utf-8")
    args = argparse.Namespace(path=str(tmp_path), json=True)
    cmd_check(args)
    capsys.readouterr()
    entries, _ = read_incident_entries(repo_ledger_dir(tmp_path))
    assert any(e["code"] == "artifact_malformed" for e in entries)
    # ADAPTATION NOTE: build the Namespace with exactly the attributes
    # cmd_check reads (inspect its signature/body); if it requires more
    # (e.g. cfg-related flags), add them with their parser defaults.


def test_inconclusive_query_records_incident(tmp_path, monkeypatch):
    from graphite.cli import _record_inconclusive

    monkeypatch.chdir(tmp_path)
    result = {
        "inconclusive": True,
        "resolution_health": {
            "healthy": False,
            "by_relation": {"imports": {"ratio": 0.05}, "calls": {"ratio": 0.26}},
        },
    }
    _record_inconclusive("callers auto_resolve_tdd", result)
    entries, _ = read_incident_entries(repo_ledger_dir(tmp_path))
    assert len(entries) == 1
    e = entries[0]
    assert e["class"] == "query" and e["code"] == "query_inconclusive"
    assert e["subject"] == "callers auto_resolve_tdd"
    assert "0.05" in e["detail"] and "0.26" in e["detail"]
    _record_inconclusive("callers x", {"inconclusive": False})
    entries, _ = read_incident_entries(repo_ledger_dir(tmp_path))
    assert len(entries) == 1  # non-inconclusive results record nothing
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest tests/test_incident_ledger.py tests/test_health.py -q`
Expected: new tests FAIL (`persisted_resolution` takes no `on_error`; `_record_inconclusive` undefined)

- [ ] **Step 3: Implement**

`src/graphite/health.py` — new signature and split except clauses (add `Callable` to the existing typing imports):

```python
def persisted_resolution(
    root: Path, on_error: Callable[[Exception], None] | None = None
) -> dict[str, Any] | None:
    """Fail-open read of the persisted block from graph-out/.graphite_analysis.json.

    ``on_error`` fires only for MALFORMED content (ValueError/RecursionError) —
    absence or unreadability is not malformation and stays silent.
    """
    path = root / "graph-out" / ".graphite_analysis.json"
    try:
        if not path.is_file() or path.stat().st_size > _MAX_ANALYSIS_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    except (ValueError, RecursionError) as exc:
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:
                pass
        return None
    if not isinstance(data, dict):
        return None
    block = data.get("resolution_health")
    return block if isinstance(block, dict) else None
```

`src/graphite/agent_hooks.py` line ~273 — pass a recorder (module-top import `from .incident_ledger import record_incident, repo_ledger_dir`):

```python
                health = persisted_resolution(
                    root,
                    on_error=lambda exc: record_incident(
                        repo_ledger_dir(root),
                        klass="build",
                        code="artifact_malformed",
                        subject=".graphite_analysis.json",
                        detail=str(exc),
                    ),
                )
```

`src/graphite/cli.py` `cmd_check` (~line 404) — same `on_error` lambda with `Path(args.path).resolve()` as the root (bind it to a local first).

`src/graphite/cli.py` `_load_graph` — record before re-raising:

```python
def _load_graph(path: Path, *, root: Path | None = None) -> Any:
    selected_root = (root or Path.cwd()).resolve()
    try:
        _, graph = load_validated_graph_bundle(path, root=selected_root)
    except GraphReadError as exc:
        record_incident(
            repo_ledger_dir(selected_root),
            klass="build",
            code="graph_load_failed",
            subject="graph-out/graph.json",
            detail=str(exc.code),
        )
        raise ValueError(f"graph unavailable: {exc.code}") from None
    return graph
```

`src/graphite/cli.py` — new helper directly below `_record_canonical_usage`:

```python
def _record_inconclusive(subject: str, result: Any) -> None:
    """Best-effort incident capture for inconclusive answers; never fatal."""
    try:
        if not (isinstance(result, dict) and result.get("inconclusive") is True):
            return
        health = result.get("resolution_health") or {}
        by_rel = health.get("by_relation") or {}

        def _ratio(rel: str) -> Any:
            cell = by_rel.get(rel) or {}
            return cell.get("ratio")

        record_incident(
            repo_ledger_dir(Path.cwd()),
            klass="query",
            code="query_inconclusive",
            subject=subject,
            detail=f"imports {_ratio('imports')}, calls {_ratio('calls')}, healthy {health.get('healthy')}",
        )
    except Exception:
        return
```

Wire it at the three result sites, each placed immediately BEFORE the existing `_record_canonical_usage` call so it also fires when the result is inconclusive-but-successful:
- `cmd_query` (~line 749): `_record_inconclusive(f"query {args.query}", result)` — match the local args attribute holding the query string.
- `cmd_impact` (~line 932): `_record_inconclusive("impact " + ",".join(<the changes list local>), result)` — use the same local variable `_impact` was called with; keep the joined subject under the 512 cap (the ledger truncates anyway).
- `cmd_context` (~line 1042): `_record_inconclusive("context " + ",".join(<the targets list local>), result)` — same rule.
(ADAPTATION NOTE: the exact local names are visible at those call sites; match them. The subject prefix strings `query `/`impact `/`context ` are fixed.)

- [ ] **Step 4: Run to verify pass**

Run: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest tests/test_incident_ledger.py tests/test_health.py tests/test_agent_hooks.py -q`
Expected: all PASS

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m ruff check src/graphite/health.py src/graphite/agent_hooks.py src/graphite/cli.py tests/test_incident_ledger.py tests/test_health.py
git add src/graphite/health.py src/graphite/agent_hooks.py src/graphite/cli.py tests/test_incident_ledger.py tests/test_health.py
git commit -m "feat(incidents): artifact, graph-load, and inconclusive-query writers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `graphite incidents` CLI (list / ack / resolve)

**Files:**
- Modify: `src/graphite/cli.py` (three `cmd_incidents_*` handlers + `_incidents_ledger_dir` helper; argparse group added near the doctor subparser, dispatch via `set_defaults(func=...)` like every other command)
- Test: `tests/test_incident_ledger.py` (append)

**Interfaces:**
- Consumes: Task 1 API.
- Produces: `cmd_incidents_list(args)`, `cmd_incidents_ack(args)`, `cmd_incidents_resolve(args)`; JSON envelope `{"schema_version": 1, "incidents": [IncidentView.to_json()...], "skipped": int}` (Task 7 publishes its schema). Flags: `--json`, `--all`, `--global` (dest `global_ledger`), `-m/--message` for lifecycle notes; path argument matching the doctor parser's convention; `--daemon-base` optional like doctor's.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_incident_ledger.py`:

```python
def _list_json(tmp_path, capsys, **over):
    import argparse

    from graphite.cli import cmd_incidents_list

    defaults = dict(path=str(tmp_path), json=True, all=False, global_ledger=False, daemon_base=None)
    defaults.update(over)
    cmd_incidents_list(argparse.Namespace(**defaults))
    return json.loads(capsys.readouterr().out)


def test_incidents_list_json_envelope(tmp_path, capsys):
    fp = _record(repo_ledger_dir(tmp_path))
    payload = _list_json(tmp_path, capsys)
    assert payload["schema_version"] == 1 and payload["skipped"] == 0
    assert len(payload["incidents"]) == 1
    inc = payload["incidents"][0]
    assert inc["fingerprint"] == fp and inc["state"] == "open" and inc["class"] == "build"


def test_incidents_ack_resolve_roundtrip(tmp_path, capsys):
    import argparse

    from graphite.cli import cmd_incidents_ack, cmd_incidents_resolve

    fp = _record(repo_ledger_dir(tmp_path))
    base = dict(path=str(tmp_path), fingerprint=fp, message=None, global_ledger=False, daemon_base=None)
    assert cmd_incidents_ack(argparse.Namespace(**base)) == 0
    capsys.readouterr()
    assert _list_json(tmp_path, capsys)["incidents"][0]["state"] == "acked"
    assert cmd_incidents_resolve(argparse.Namespace(**{**base, "message": "fixed"})) == 0
    capsys.readouterr()
    assert _list_json(tmp_path, capsys)["incidents"] == []  # resolved hidden by default
    assert _list_json(tmp_path, capsys, all=True)["incidents"][0]["state"] == "resolved"


def test_incidents_unknown_fingerprint_exits_1(tmp_path, capsys):
    import argparse

    from graphite.cli import cmd_incidents_ack

    args = argparse.Namespace(path=str(tmp_path), fingerprint="deadbeefdeadbeef", message=None, global_ledger=False, daemon_base=None)
    assert cmd_incidents_ack(args) == 1


def test_incidents_cli_via_main(tmp_path, capsys):
    from graphite.cli import main

    # The parser is built inside main() (there is no build_parser function),
    # so registration is proven by invoking the real entry point end-to-end.
    assert main(["incidents", "list", str(tmp_path)]) == 0
    assert "no incidents" in capsys.readouterr().out
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest tests/test_incident_ledger.py -q`
Expected: new tests FAIL — handlers undefined; `main(["incidents", ...])` errors on the unknown subcommand.

- [ ] **Step 3: Implement** — in `src/graphite/cli.py`:

```python
def _incidents_ledger_dir(args: argparse.Namespace) -> Path:
    if getattr(args, "global_ledger", False):
        if getattr(args, "daemon_base", None):
            base = Path(args.daemon_base).resolve()
        else:
            base = default_projects_root().resolve()  # same import doctor uses
        return base / ".graphite-daemon"
    return repo_ledger_dir(Path(args.path).resolve())


def cmd_incidents_list(args: argparse.Namespace) -> int:
    from .incident_ledger import fold_incidents, read_incident_entries

    entries, skipped = read_incident_entries(_incidents_ledger_dir(args))
    views = fold_incidents(entries)
    if not args.all:
        views = [v for v in views if v.state != "resolved"]
    if args.json:
        payload = {
            "schema_version": 1,
            "incidents": [v.to_json() for v in views],
            "skipped": skipped,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not views:
        print("[graphite] no incidents")
    for v in views:
        print(f"{v.state:9} {v.fingerprint} {v.klass}/{v.code} {v.subject} x{v.count} last {v.last_seen}")
    if skipped:
        print(f"[graphite] skipped {skipped} corrupt line(s)")
    return 0


def _incidents_lifecycle(args: argparse.Namespace, kind: str) -> int:
    from .incident_ledger import append_lifecycle, fold_incidents, read_incident_entries

    ledger_dir = _incidents_ledger_dir(args)
    if not append_lifecycle(ledger_dir, args.fingerprint, kind, note=args.message):
        print(f"[graphite] unknown fingerprint: {args.fingerprint}", file=sys.stderr)
        return 1
    entries, _ = read_incident_entries(ledger_dir)
    for v in fold_incidents(entries):
        if v.fingerprint == args.fingerprint:
            print(f"{v.state:9} {v.fingerprint} {v.klass}/{v.code} {v.subject} x{v.count}")
    return 0


def cmd_incidents_ack(args: argparse.Namespace) -> int:
    return _incidents_lifecycle(args, "ack")


def cmd_incidents_resolve(args: argparse.Namespace) -> int:
    return _incidents_lifecycle(args, "resolve")
```

Argparse (place beside the doctor parser; MATCH the doctor parser's path-argument convention exactly — positional vs `--path`, default `"."`; `default_projects_root` is imported the same way doctor.py imports it):

```python
    p_incidents = sub.add_parser("incidents", help="List and triage recorded incidents")
    incidents_sub = p_incidents.add_subparsers(dest="incidents_cmd", required=True)
    p_inc_list = incidents_sub.add_parser("list", help="Folded incident views (open+acked by default)")
    p_inc_list.add_argument("path", nargs="?", default=".")
    p_inc_list.add_argument("--json", action="store_true")
    p_inc_list.add_argument("--all", action="store_true", help="Include resolved incidents")
    p_inc_list.add_argument("--global", dest="global_ledger", action="store_true", help="Read the daemon-global ledger")
    p_inc_list.add_argument("--daemon-base", default=None)
    p_inc_list.set_defaults(func=cmd_incidents_list)
    for name, handler in (("ack", cmd_incidents_ack), ("resolve", cmd_incidents_resolve)):
        p_life = incidents_sub.add_parser(name, help=f"{name} an incident by fingerprint")
        p_life.add_argument("fingerprint")
        p_life.add_argument("path", nargs="?", default=".")
        p_life.add_argument("-m", "--message", default=None)
        p_life.add_argument("--global", dest="global_ledger", action="store_true")
        p_life.add_argument("--daemon-base", default=None)
        p_life.set_defaults(func=handler)
```

Do NOT add `incidents` to `_CANONICAL_COMMANDS` (cli.py:96): that set feeds the published `capabilities` envelope (cli.py:779) and the LLM-gating union (cli.py:115); widening a published contract is not this round's call. If any capabilities/parser test fails because of the new subcommand, report it rather than editing the set.

- [ ] **Step 4: Run to verify pass**

Run: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest tests/test_incident_ledger.py tests/test_cli_capabilities.py -q`
(If `tests/test_cli_capabilities.py` does not exist, run the test file that covers CLI parsing/capabilities — grep `capabilities` under tests/ — and name it in your report.)
Expected: all PASS

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m ruff check src/graphite/cli.py tests/test_incident_ledger.py
git add src/graphite/cli.py tests/test_incident_ledger.py
git commit -m "feat(incidents): incidents list/ack/resolve CLI

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Daemon writers (build-cycle + observer-cycle)

**Files:**
- Modify: `src/graphite/daemon.py` (`_record_build_result` ~line 478-489; the provider-observer `except Exception` branch ~line 406-414)
- Test: `tests/test_incident_ledger.py` (append)

**Interfaces:**
- Consumes: Task 1 API. `ProjectRuntime.root: Path` (daemon.py:156) provides the per-project ledger root; the observer object's logger holds `state_dir` (DaemonLogger stores it, daemon.py:190).
- Produces: daemon-side incidents — `daemon_build_failed` (per-repo ledger of the failing project, subject = str(project root)) and `provider_probe_failed` (daemon-global ledger, subject `"daemon"`, detail `"observer_cycle_failed"`).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_incident_ledger.py`:

```python
def test_daemon_build_failure_records_incident(tmp_path):
    from graphite.daemon import _record_build_result

    # ADAPTATION NOTE: construct ProjectRuntime / WatchChange / BuildResult
    # with the MINIMAL real constructors from graphite.daemon (read their
    # dataclass definitions; use tmp_path as root and a failing BuildResult —
    # returncode != 0 with stderr text). Do not mock the ledger.
    state = _minimal_project_runtime(tmp_path)
    change = _minimal_watch_change()
    result = _failing_build_result("boom: extraction exploded")
    _record_build_result(state, change, result)
    entries, _ = read_incident_entries(repo_ledger_dir(tmp_path))
    assert any(
        e["class"] == "daemon" and e["code"] == "daemon_build_failed" and "boom" in e["detail"]
        for e in entries
    )


def test_daemon_build_success_records_nothing(tmp_path):
    from graphite.daemon import _record_build_result

    state = _minimal_project_runtime(tmp_path)
    change = _minimal_watch_change()
    result = _passing_build_result()
    _record_build_result(state, change, result)
    entries, _ = read_incident_entries(repo_ledger_dir(tmp_path))
    assert entries == []
```

(The three `_minimal_*`/`_failing_*`/`_passing_*` helpers are yours to write at the top of the test section from the real dataclasses in `graphite.daemon` — no mocks, real constructors, minimal fields. If existing daemon tests already build these objects, copy their construction idiom and name the file you copied from in your report.)

- [ ] **Step 2: Run to verify failure**

Run: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest tests/test_incident_ledger.py -q`
Expected: the two new tests FAIL (no incident recorded on failure) — the success-path test may pass trivially; note it.

- [ ] **Step 3: Implement** — in `src/graphite/daemon.py` (module-top import `from .incident_ledger import record_incident, repo_ledger_dir`):

In `_record_build_result`, inside the failure branch that sets `state.last_error` (~line 489), append:

```python
        record_incident(
            repo_ledger_dir(state.root),
            klass="daemon",
            code="daemon_build_failed",
            subject=str(state.root),
            detail=state.last_error or "build failed",
        )
```

In the provider-observer cycle's `except Exception` branch (~line 406-414, where `reason_counts: {"observer_cycle_failed": 1}` is built), append — using the state dir the observer's logger already holds (verify the attribute name on DaemonLogger; it stores `state_dir` at construction):

```python
            record_incident(
                self._logger.state_dir,
                klass="daemon",
                code="provider_probe_failed",
                subject="daemon",
                detail="observer_cycle_failed",
            )
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest tests/test_incident_ledger.py tests/test_daemon.py -q`
(If daemon tests live under a different name, grep `_record_build_result` in tests/ and run that file; name it in your report.)
Expected: all PASS

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m ruff check src/graphite/daemon.py tests/test_incident_ledger.py
git add src/graphite/daemon.py tests/test_incident_ledger.py
git commit -m "feat(incidents): daemon build-cycle and observer-cycle writers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Doctor check + daemon-health block

**Files:**
- Modify: `src/graphite/doctor.py` (new `_incidents_check`; wire into `_fast_checks`)
- Modify: `src/graphite/daemon_health.py` (`evaluate_daemon_health` report gains `incidents`)
- Test: `tests/test_incident_ledger.py` (append)

**Interfaces:**
- Consumes: Task 1 API; `DoctorCheck(code, label, status, summary, details, remediation)` (doctor.py:70); doctor status literals from that module's `_RANK` (use the module's own ok/warn identifiers — read them, do not invent).
- Produces: doctor check `code="incidents"` (status ok when no open incidents, the module's warn-level status otherwise; `details["top_open"]` = up to 10 `"{fingerprint} {code} {subject} x{count}"` strings); daemon-health report key `incidents = {"open": int, "acked": int, "by_class": {class: open_count}, "projects": {root_str: open_count}}` (projects included only for projects already present in the status payload, open counts > 0 only).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_incident_ledger.py`:

```python
def test_doctor_reports_incidents_section(tmp_path):
    from graphite.config import Config
    from graphite.doctor import run_doctor

    _record(repo_ledger_dir(tmp_path), subject="src/broken.py")
    report = run_doctor(tmp_path, cfg=Config(cache_dir=tmp_path / ".cache"), daemon_base=tmp_path)
    checks = {c["code"]: c for c in report["checks"]}
    # ADAPTATION NOTE: read build_report to confirm the report's checks are
    # dicts under "checks" with a "code" key; adapt access if the shape
    # differs, keeping the assertions' substance.
    assert "incidents" in checks
    assert "1 open" in checks["incidents"]["summary"]
    assert any("src/broken.py" in line for line in checks["incidents"]["details"]["top_open"])


def test_doctor_incidents_ok_when_none(tmp_path):
    from graphite.config import Config
    from graphite.doctor import run_doctor

    report = run_doctor(tmp_path, cfg=Config(cache_dir=tmp_path / ".cache"), daemon_base=tmp_path)
    checks = {c["code"]: c for c in report["checks"]}
    assert "incidents" in checks and "0 open" in checks["incidents"]["summary"]


def test_daemon_health_includes_incident_counts(tmp_path):
    from graphite.daemon_health import evaluate_daemon_health

    state_dir = tmp_path / ".graphite-daemon"
    record_incident(state_dir, klass="daemon", code="provider_probe_failed", subject="daemon", detail="x")
    report = evaluate_daemon_health(tmp_path, state_dir=state_dir)
    # ADAPTATION NOTE: evaluate_daemon_health may need a status.json to exist;
    # if it fails hard on a missing status, write the minimal valid status
    # artifact the way existing daemon_health tests do (copy their fixture
    # idiom and name the file in your report).
    assert report["incidents"]["open"] == 1
    assert report["incidents"]["by_class"] == {"daemon": 1}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest tests/test_incident_ledger.py -q`
Expected: new tests FAIL (no incidents check; no incidents key)

- [ ] **Step 3: Implement**

`src/graphite/doctor.py` — add and wire into `_fast_checks`' returned list (use the module's actual ok/warn status identifiers from `_RANK`):

```python
def _incidents_check(root: Path) -> DoctorCheck:
    from .incident_ledger import fold_incidents, read_incident_entries, repo_ledger_dir

    entries, skipped = read_incident_entries(repo_ledger_dir(root))
    views = [v for v in fold_incidents(entries) if v.state != "resolved"]
    open_views = [v for v in views if v.state == "open"]
    top = [f"{v.fingerprint} {v.code} {v.subject} x{v.count}" for v in open_views[:10]]
    summary = f"{len(open_views)} open / {len(views) - len(open_views)} acked"
    if skipped:
        summary += f", {skipped} corrupt line(s)"
    status = "degraded" if open_views else "ready"
    return DoctorCheck(
        code="incidents",
        label="Incident ledger",
        status=status,
        summary=summary,
        details={"top_open": top},
        remediation=("graphite incidents list", "graphite incidents ack <fingerprint> -m NOTE")
        if open_views
        else (),
    )
```

(`DoctorStatus` is `Literal["ready", "optional", "degraded", "blocked"]` — doctor.py:26; `"degraded"`/`"ready"` above are those exact literals. Verify the `report["checks"]` element shape in `build_report` when writing the test accessors.)

`src/graphite/daemon_health.py` — `evaluate_daemon_health` has FOUR `return _finalize(...)` sites (three early returns for missing/oversized/unreadable status at lines ~95-135, plus the happy-path return at the end). Attach incidents on ALL of them via a closure defined right after `status_path = (state_dir or (base / ".graphite-daemon")) / "status.json"` (line ~90) — `status_path.parent` IS the global ledger dir:

```python
    def _with_incidents(report: dict[str, Any], project_roots: list[str]) -> dict[str, Any]:
        from .incident_ledger import fold_incidents, read_incident_entries, repo_ledger_dir

        g_entries, _g_skipped = read_incident_entries(status_path.parent)
        g_views = [v for v in fold_incidents(g_entries) if v.state != "resolved"]
        by_class: dict[str, int] = {}
        for v in g_views:
            if v.state == "open":
                by_class[v.klass] = by_class.get(v.klass, 0) + 1
        projects: dict[str, int] = {}
        for project_root in project_roots[:_MAX_INPUT_PROJECTS]:
            p_entries, _ = read_incident_entries(repo_ledger_dir(Path(project_root)))
            open_count = sum(1 for v in fold_incidents(p_entries) if v.state == "open")
            if open_count:
                projects[str(project_root)] = open_count
        report["incidents"] = {
            "open": sum(1 for v in g_views if v.state == "open"),
            "acked": sum(1 for v in g_views if v.state == "acked"),
            "by_class": by_class,
            "projects": projects,
        }
        return report
```

Then wrap each return site: the three early returns become `return _with_incidents(_finalize(...), [])`; the happy-path return passes the project-root strings the function already extracted from the normalized status (the same roots `project_health` is computed from — match the local name in that tail; it is a list of root strings parsed from `status["projects"]`). If the happy-path root list is not already a plain local, bind it to one where it is first derived. Report the local name used.

- [ ] **Step 4: Run to verify pass**

Run: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest tests/test_incident_ledger.py tests/test_doctor.py tests/test_daemon_health.py -q`
(Adapt test-file names to reality via grep if they differ; name them in your report.)
Expected: all PASS

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m ruff check src/graphite/doctor.py src/graphite/daemon_health.py tests/test_incident_ledger.py
git add src/graphite/doctor.py src/graphite/daemon_health.py tests/test_incident_ledger.py
git commit -m "feat(incidents): doctor check + daemon-health incident counts

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Published schema, docs, template bump, e2e acceptance

**Files:**
- Create: `docs/schemas/incidents.v1.schema.json`
- Modify: `docs/agent-integration.md` (new section after the resolution-health section), `src/graphite/init.py` (template item + `DOC_VERSION = 6` → `7` at init.py:17), `tests/test_init.py` (digest re-pin, same mechanism as the DOC_VERSION 5→6 bump used), the published-schemas test module (add incidents compat test following its existing per-schema pattern)
- Test: `tests/test_incident_ledger.py` (append e2e)

**Interfaces:**
- Consumes: everything from Tasks 1–6.
- Produces: published envelope schema for `incidents list --json`; managed-template DOC_VERSION 7.

- [ ] **Step 1: Write the schema** — create `docs/schemas/incidents.v1.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://graphite.local/schemas/incidents.v1.schema.json",
  "title": "graphite incidents list --json envelope (v1)",
  "type": "object",
  "required": ["schema_version", "incidents", "skipped"],
  "properties": {
    "schema_version": {"const": 1},
    "skipped": {"type": "integer", "minimum": 0},
    "incidents": {
      "type": "array",
      "items": {
        "type": "object",
        "required": [
          "fingerprint", "class", "code", "subject", "state",
          "first_seen", "last_seen", "count", "last_detail"
        ],
        "properties": {
          "fingerprint": {"type": "string", "pattern": "^[0-9a-f]{16}$"},
          "class": {"enum": ["build", "query", "daemon"]},
          "code": {"type": "string"},
          "subject": {"type": "string", "maxLength": 512},
          "state": {"enum": ["open", "acked", "resolved"]},
          "first_seen": {"type": "string"},
          "last_seen": {"type": "string"},
          "count": {"type": "integer", "minimum": 1},
          "last_detail": {"type": "string"}
        },
        "additionalProperties": true
      }
    }
  },
  "additionalProperties": true
}
```

(Two deliberate choices: the explicit `additionalProperties: true` values are REQUIRED — the repo's zero-dep subset validator treats absent `additionalProperties` as closed. And `last_note` is intentionally NOT in `properties`/`required`: it is nullable, the subset validator supports no combinators, and a `["string", "null"]` union type may not be in the supported subset — the field rides under `additionalProperties`. If `is_supported_schema` rejects anything else in this schema, simplify the offending construct and note it in your report; never widen the validator.)

- [ ] **Step 2: Write the failing tests**

In `tests/test_published_schemas.py`: add `"incidents.v1.schema.json"` to the `_ALL_SCHEMAS` tuple (line ~18 — this automatically runs the `is_supported_schema` subset-validator gate over it), then append:

```python
def test_incidents_envelope_matches_published_schema(tmp_path, capsys):
    from graphite.incident_ledger import incident_fingerprint, record_incident, repo_ledger_dir

    record_incident(
        repo_ledger_dir(tmp_path), klass="build", code="parse_error", subject="src/a.py", detail="boom"
    )
    fp = incident_fingerprint("build", "parse_error", "src/a.py")
    assert main(["incidents", "ack", fp, str(tmp_path), "-m", "known"]) == 0
    capsys.readouterr()
    assert main(["incidents", "list", str(tmp_path), "--json", "--all"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert matches_schema(payload, _load("incidents.v1.schema.json")) is True
```

(`main`, `json`, `matches_schema`, and `_load` are already imported/defined in that module — see its lines 4-27.)

Append to `tests/test_incident_ledger.py` the end-to-end acceptance:

```python
def test_e2e_broken_file_to_triage_roundtrip(tmp_path, monkeypatch, capsys):
    import argparse

    from graphite.cli import _build_project, cmd_incidents_ack, cmd_incidents_list
    from graphite.config import Config

    _write(tmp_path / "src" / "ok.py", "def ok():\n    return 1\n")
    (tmp_path / "src" / "bad.py").write_bytes(b"\xff\xfe\x00broken\x00")
    monkeypatch.chdir(tmp_path)
    _build_project(tmp_path, Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite"))
    capsys.readouterr()
    cmd_incidents_list(argparse.Namespace(path=str(tmp_path), json=True, all=False, global_ledger=False, daemon_base=None))
    payload = json.loads(capsys.readouterr().out)
    open_incidents = [i for i in payload["incidents"] if i["state"] == "open"]
    assert open_incidents, "build over a broken file must surface an open incident"
    fp = open_incidents[0]["fingerprint"]
    assert cmd_incidents_ack(argparse.Namespace(path=str(tmp_path), fingerprint=fp, message="triaged", global_ledger=False, daemon_base=None)) == 0
    capsys.readouterr()
    cmd_incidents_list(argparse.Namespace(path=str(tmp_path), json=True, all=False, global_ledger=False, daemon_base=None))
    payload = json.loads(capsys.readouterr().out)
    assert payload["incidents"][0]["state"] == "acked"
```

- [ ] **Step 3: Run to verify failure**

Run: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest tests/test_incident_ledger.py -q` plus the published-schemas module.
Expected: schema compat test FAILS (schema file exists but nothing validated yet / or passes immediately once transcribed — note which); e2e should PASS if Tasks 1–6 are correct — if it FAILS, that is a real integration bug to fix, never a test to weaken (exception: if the undecodable-bytes fixture doesn't error, pin a fixture that verifiably does, per Task 2's probe).

- [ ] **Step 4: Docs + template**

- `docs/agent-integration.md`: add a section "Incidents" after the resolution-health section: what gets recorded (the eight codes), fingerprint stability, fold/lifecycle semantics (ack persists, resolve reopens on recurrence), `incidents list --json` envelope + schema pointer, and one paragraph: recurring incidents belong in a governed spec round, not ad-hoc fixes.
- `src/graphite/init.py`: `DOC_VERSION = 6` → `7` (line 17). In the managed `GRAPHITE_DOC` template, add item: `7. **Incidents**: `python -m graphite incidents list` shows recorded failures (build errors, malformed artifacts, inconclusive queries). Check it when a graph answer looks wrong; recurring incidents belong in a governed round.` (match the template's existing numbering/formatting).
- `tests/test_init.py`: re-pin the managed-template digest using the same mechanism the DOC_VERSION 5→6 bump used (grep the digest constant; recompute; verify the test suite's helper output matches).

- [ ] **Step 5: Run to verify pass**

Run: `cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest tests/test_incident_ledger.py tests/test_init.py tests/test_documentation.py -q` plus the published-schemas module.
Expected: all PASS

- [ ] **Step 6: Ruff + commit**

```bash
cd /f/tmp/graphite-incident-ledger && PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m ruff check src/graphite/init.py tests
git add docs/schemas/incidents.v1.schema.json docs/agent-integration.md src/graphite/init.py tests
git commit -m "feat(incidents): published schema, docs, managed-template DOC_VERSION 7, e2e

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Completion

Full suite (`PYTHONPATH='F:/tmp/graphite-incident-ledger/src' python -m pytest -q`, expect baseline 2167 + new passed / 44 skipped / 0 failed) + `ruff check src tests`, final whole-branch review per superpowers:subagent-driven-development, then superpowers:finishing-a-development-branch. Post-merge (operator-gated): **daemon restart** (daemon-executed surfaces changed — operator runs Stop-Process), consumer re-init for DOC_VERSION 7, live acceptance per spec §11 (seeded broken file → incident → doctor → ack/resolve; daemon-health counts; fail-open build check), memory update.
