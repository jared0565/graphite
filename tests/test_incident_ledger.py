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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_extract_all_collects_per_file_errors(tmp_path):
    from graphite.config import Config
    from graphite.extract.ast import extract_all
    from graphite.ingest import collect_files

    _write(tmp_path / "good.py", "def ok():\n    return 1\n")
    bad = tmp_path / "bad.py"
    _write(bad, "def broken():\n    return 2\n")
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite")
    entries = collect_files(tmp_path, cfg)
    # PROBE RESULT (see task-2-report.md for the full trace): tree-sitter is
    # error-tolerant, so neither a syntax error (`def broken(:`) nor invalid
    # UTF-8 bytes without a NUL (`\xff\xfe`) makes extract_file() raise or
    # return .error — both parse cleanly. Undecodable bytes WITH a NUL
    # (`\xff\xfe\x00broken`) never even reach extract_file: ingest.collect_files'
    # `_is_binary()` treats any NUL byte as binary and drops the file from
    # `entries` before extraction is attempted. The one input that verifiably
    # errors at workers=1 (no thread pool to catch stray exceptions) is a
    # collect-then-vanish race: the file is real at collect_files() time (so it
    # becomes an entry) and removed before extract_all() opens it, so
    # extract_file's `open(entry.abs_path, "rb")` raises FileNotFoundError,
    # which its own try/except turns into a `read_error` ExtractionResult.
    bad.unlink()
    result = extract_all(entries, cfg)
    assert isinstance(result.errors, list)
    assert result.errors, "expected the vanished file to produce a read_error"
    err = result.errors[0]
    assert set(err) == {"code", "subject", "detail"}
    assert err["subject"] == "bad.py"
    assert err["code"] == "read_error"


def test_build_records_extraction_incidents(tmp_path, monkeypatch):
    import argparse

    from graphite.cli import _build, _scan
    from graphite.config import Config

    _write(tmp_path / "src" / "ok.py", "def ok():\n    return 1\n")
    bad = tmp_path / "src" / "bad.py"
    _write(bad, "def also_ok():\n    return 2\n")
    monkeypatch.chdir(tmp_path)
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite")
    args = argparse.Namespace(path=str(tmp_path))
    # Same collect-then-vanish technique as test_extract_all_collects_per_file_errors
    # (see that test's comment / the task-2 report for the probe trace): _scan()
    # collects `bad.py` as a real entry, then it is removed before _build() (which
    # calls extract_all() -> extract_file()) ever opens it, producing a genuine
    # read_error. _build_project() runs both steps back-to-back with no seam to
    # inject the deletion, so this calls _scan()/_build() directly — the exact
    # pair _build_project() wraps — to exercise the incident-recording wiring
    # added to _build() right after its extract_all() call.
    manifest, entries = _scan(args, cfg)
    bad.unlink()
    _build(args, cfg, manifest, entries)
    incident_entries, _ = read_incident_entries(repo_ledger_dir(tmp_path))
    build_entries = [e for e in incident_entries if e["class"] == "build"]
    assert any(e["subject"].endswith("bad.py") for e in build_entries)
    assert all(e["code"] in ("parse_error", "read_error", "worker_error") for e in build_entries)


def test_check_records_artifact_malformed(tmp_path, capsys):
    import argparse

    from graphite.cli import cmd_check

    out = tmp_path / "graph-out"
    out.mkdir(parents=True)
    (out / ".graphite_analysis.json").write_text("{not valid json", encoding="utf-8")
    # cmd_check reads args.path, args.ignore_engine (via check_graph_freshness),
    # and args.json; ignore_engine defaults to False per the --ignore-engine
    # parser flag (action="store_true").
    args = argparse.Namespace(path=str(tmp_path), json=True, ignore_engine=False)
    cmd_check(args)
    capsys.readouterr()
    entries, _ = read_incident_entries(repo_ledger_dir(tmp_path))
    assert any(e["code"] == "artifact_malformed" for e in entries)


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


def test_natural_query_inconclusive_records_incident(tmp_path, monkeypatch):
    # Same unhealthy-graph idiom as test_health.py's _unhealthy_graph(): a
    # lonely node plus one unbound call edge elsewhere -> calls ratio 0.0 ->
    # unhealthy. answer_natural() routes "who calls lonely" through the same
    # execute_plan()/_attach_resolution() path as the structured "callers"
    # query, so it comes back with total 0 and inconclusive True.
    import networkx as nx

    from graphite.cli import main

    g = nx.DiGraph()
    g.add_node("lonely", kind="function", source_file="a.py")
    g.add_node("src", kind="function", source_file="a.py")
    g.add_node("ghost", kind="unknown", source_file=None)
    g.add_edge("src", "ghost", relation="calls", source_file="a.py")

    import graphite.cli as cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: g)
    monkeypatch.chdir(tmp_path)

    assert main(["query", "who calls lonely", "--natural"]) == 0

    entries, _ = read_incident_entries(repo_ledger_dir(tmp_path))
    query_entries = [e for e in entries if e["class"] == "query"]
    assert len(query_entries) == 1
    e = query_entries[0]
    assert e["code"] == "query_inconclusive"
    assert e["subject"] == "query who calls lonely"
    assert "calls 0.0" in e["detail"]


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


# ADAPTATION NOTE (task 5): no existing test file constructs ProjectRuntime,
# WatchChange, or BuildResult directly — tests/test_daemon.py only builds
# BuildResult (as the return value of its `fake_build` stubs, e.g. line 58:
# `BuildResult(success=True, returncode=0, duration_seconds=0.01, stdout="ok")`)
# and never touches ProjectRuntime/WatchChange at all; those are only ever
# constructed inside src/graphite/daemon.py itself (ProjectRuntime at line
# ~537: `ProjectRuntime(root=project, snapshot=snap, needs_initial_build=...)`).
# The helpers below use the same minimal-real-constructor idiom: ProjectRuntime
# needs `root` + `snapshot` (Snapshot = dict[str, str], so `{}` is valid and
# unused by _record_build_result); WatchChange's three fields all default to
# `()` so the no-arg constructor is already minimal; BuildResult is a frozen
# dataclass requiring success/returncode/duration_seconds with stdout/stderr/
# error defaulting to "" / "" / None.
def _minimal_project_runtime(root: Path):
    from graphite.daemon import ProjectRuntime

    return ProjectRuntime(root=root, snapshot={})


def _minimal_watch_change():
    from graphite.watch import WatchChange

    return WatchChange()


def _failing_build_result(stderr: str):
    from graphite.daemon import BuildResult

    return BuildResult(success=False, returncode=1, duration_seconds=0.01, stderr=stderr)


def _passing_build_result():
    from graphite.daemon import BuildResult

    return BuildResult(success=True, returncode=0, duration_seconds=0.01)


def test_daemon_build_failure_records_incident(tmp_path):
    from graphite.daemon import _record_build_result

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


def test_observer_cycle_failure_records_incident(tmp_path):
    # Not in the brief's Step 1 list (which only specified the two build-path
    # tests above) — added because the task title is "build-cycle +
    # observer-cycle" and the observer path had zero coverage otherwise.
    # Drives _ProviderObservationWorker._run() synchronously (no thread: we
    # never call .start()) with a real DaemonLogger backed by tmp_path, and a
    # cycle callable that sets worker._stop before raising so the loop body
    # runs exactly once and _run() returns on its own (stop.wait() sees the
    # event already set and returns True immediately).
    from graphite.daemon import DaemonLogger, _ProviderObservationWorker

    logger = DaemonLogger(tmp_path / ".graphite-daemon", 5_000_000)
    worker = _ProviderObservationWorker(cycle=lambda: None, logger=logger, interval_seconds=0.01)

    def _raising_cycle():
        worker._stop.set()
        raise RuntimeError("boom")

    worker._cycle = _raising_cycle
    worker._run()

    entries, _ = read_incident_entries(logger.state_dir)
    assert any(
        e["class"] == "daemon"
        and e["code"] == "provider_probe_failed"
        and e["subject"] == "daemon"
        and e["detail"] == "observer_cycle_failed"
        for e in entries
    )
