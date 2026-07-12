"""Typed, bounded system-readiness checks."""
from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
import graphite.doctor as doctor_module

from graphite.config import Config
from graphite.freshness import FreshnessLimitError, check_graph_freshness
from graphite.doctor import (
    DoctorCheck,
    DoctorStatus,
    build_report,
    check_git,
    check_graph,
    check_daemon,
    check_llm_config,
    check_mcp,
    check_python,
    check_typescript,
    format_doctor_text,
    run_doctor,
)


def test_report_is_typed_sorted_ranked_and_safe(tmp_path: Path) -> None:
    checks = [
        DoctorCheck("z", "Z", "optional", "optional"),
        DoctorCheck("a", "A", "degraded", "degraded", remediation=("Fix it",)),
    ]
    report = build_report(tmp_path / "secret-repo", checks, deep=False, llm_included=False)
    assert list(report) == ["schema_version", "root", "deep", "llm_included", "status", "exit_code", "checks"]
    assert report["root"] == "secret-repo"
    assert report["status"] == "degraded"
    assert report["exit_code"] == 0
    assert [item["code"] for item in report["checks"]] == ["a", "z"]
    assert tuple(checks[0].remediation) == ()
    with pytest.raises(Exception):
        checks[0].status = "ready"  # type: ignore[misc]
    text = format_doctor_text(report)
    assert text == "[graphite] doctor: degraded\n  [degraded] A: degraded\n    - Fix it\n  [optional] Z: optional\n"


def test_empty_report_ready_and_blocked_exits_one(tmp_path: Path) -> None:
    assert build_report(tmp_path, [], deep=False, llm_included=False)["status"] == "ready"
    report = build_report(tmp_path, [DoctorCheck("x", "X", "blocked", "no")], deep=False, llm_included=False)
    assert report["exit_code"] == 1
    with pytest.raises(ValueError):
        DoctorCheck("x", "X", "bad", "no")
    assert DoctorStatus is not None


@pytest.mark.parametrize("invalid", [Path("x"), b"x", math.inf, math.nan, {1: "x"}, object()])
def test_doctor_details_reject_non_json_values(invalid: object) -> None:
    with pytest.raises(ValueError, match="invalid doctor check details"):
        DoctorCheck("x", "X", "ready", "ok", {"nested": invalid})


def test_doctor_details_are_recursively_immutable_and_serializable(tmp_path: Path) -> None:
    source = {"nested": {"items": [1, {"ok": True}]}}
    check = DoctorCheck("x", "X", "ready", "ok", source)
    source["nested"]["items"].append("mutated")  # type: ignore[index,union-attr]
    first = check.to_dict()
    first["details"]["nested"]["items"].append("caller")
    second = check.to_dict()
    assert second["details"] == {"nested": {"items": [1, {"ok": True}]}}
    json.dumps(build_report(tmp_path, [check], deep=False, llm_included=False))
    with pytest.raises(ValueError, match="invalid doctor remediation"):
        DoctorCheck("x", "X", "ready", "ok", remediation=["bad"])  # type: ignore[arg-type]


def test_build_report_resolves_root_and_rejects_invalid_check_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert build_report(Path("."), [], deep=False, llm_included=False)["root"] == tmp_path.name

    class InvalidCheck:
        code = "bad"
        status = "invalid"

        def to_dict(self) -> dict:
            return {"code": self.code, "status": self.status}

    with pytest.raises(ValueError, match="invalid doctor check status"):
        build_report(tmp_path, [InvalidCheck()], deep=False, llm_included=False)  # type: ignore[list-item]


def test_python_and_git_checks_are_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert check_python().status in {"ready", "blocked"}

    class Runner:
        def __init__(self, root: Path):
            assert root == tmp_path

        def run(self, args, *, timeout_seconds, max_stdout_bytes):
            assert args == ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
            assert timeout_seconds == 10.0
            return type("R", (), {"returncode": 0, "stdout": b"line\ninside\0second\0"})()

    monkeypatch.setattr("graphite.doctor.GitRunner", Runner)
    result = check_git(tmp_path)
    assert result.status == "ready"
    assert result.details == {"record_count": 2}


def test_git_requires_nul_terminated_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Runner:
        def __init__(self, root: Path):
            pass

        def run(self, *args, **kwargs):
            return type("R", (), {"returncode": 0, "stdout": b"unterminated"})()

    monkeypatch.setattr("graphite.doctor.GitRunner", Runner)
    assert check_git(tmp_path).status == "blocked"


@pytest.mark.parametrize("output", [b"\xff\0", b"ok\0\0", b"../escape\0", b"/absolute\0", b"C:\\absolute\0", b".\0", b"a//b\0"])
def test_git_rejects_invalid_records(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output: bytes) -> None:
    class Runner:
        def __init__(self, root: Path): pass
        def run(self, *args, **kwargs): return type("R", (), {"returncode": 0, "stdout": output})()
    monkeypatch.setattr("graphite.doctor.GitRunner", Runner)
    assert check_git(tmp_path).status == "blocked"


def test_freshness_manifest_limit_is_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manifest = tmp_path / "out" / ".graphite_manifest.json"
    manifest.parent.mkdir()
    manifest.write_text("{}", encoding="utf-8")
    cfg = Config(output_dir=manifest.parent)
    monkeypatch.setattr("graphite.freshness._manifest_size", lambda path: 9)
    with pytest.raises(FreshnessLimitError, match="freshness manifest limit exceeded"):
        check_graph_freshness(tmp_path, cfg, max_manifest_bytes=8)


def test_freshness_file_cap_stops_before_unbounded_collection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manifest = tmp_path / "out" / ".graphite_manifest.json"
    manifest.parent.mkdir()
    manifest.write_text('{"files": []}', encoding="utf-8")
    entries = [type("Entry", (), {"rel_path": str(index), "content_hash": "x"})() for index in range(10_001)]
    monkeypatch.setattr("graphite.freshness.collect_files", lambda root, cfg: entries)
    with pytest.raises(FreshnessLimitError, match="freshness file limit exceeded"):
        check_graph_freshness(tmp_path, Config(output_dir=manifest.parent, max_files=10_001), max_manifest_bytes=1024)
    cfg = Config(output_dir=manifest.parent)
    monkeypatch.setattr("graphite.freshness._manifest_size", lambda path: 1)
    monkeypatch.setattr("graphite.freshness._read_manifest_bounded", lambda path, limit: (_ for _ in ()).throw(FreshnessLimitError("freshness manifest limit exceeded")))
    with pytest.raises(FreshnessLimitError, match="freshness manifest limit exceeded"):
        check_graph_freshness(tmp_path, cfg, max_manifest_bytes=8)


def _bundle() -> dict:
    return {"nodes": [], "edges": [], "clusters": [], "analysis": {}, "metadata": {"node_count": 0, "edge_count": 0, "community_count": 0}}


def test_graph_states_size_limit_and_safe_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = Config(output_dir=Path("out"))
    assert check_graph(tmp_path, cfg).status == "degraded"
    graph = tmp_path / "out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text(json.dumps(_bundle()), encoding="utf-8")
    monkeypatch.setattr("graphite.doctor.check_graph_freshness", lambda root, cfg, **kwargs: {"stale": False})
    ready = check_graph(tmp_path, cfg)
    assert ready.status == "ready"
    assert ready.details == {"node_count": 0, "edge_count": 0, "warning_count": 1, "stale": False}
    monkeypatch.setattr("graphite.doctor._artifact_size", lambda path: 128 * 1024 * 1024 + 1)
    assert check_graph(tmp_path, cfg).status == "blocked"
    monkeypatch.setattr("graphite.doctor._artifact_size", lambda path: 1)
    monkeypatch.setattr("graphite.doctor._read_json_bounded", lambda path, limit: (_ for _ in ()).throw(OSError("RAW C:/secret")))
    result = check_graph(tmp_path, cfg)
    assert "RAW" not in json.dumps(result.to_dict())
    assert str(tmp_path) not in json.dumps(result.to_dict())


def test_graph_blocks_freshness_limits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = Config(output_dir=Path("out"), max_files=None, max_file_size=99_000_000)
    graph = tmp_path / "out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text(json.dumps(_bundle()), encoding="utf-8")
    def limited(root, scoped, *, max_manifest_bytes):
        assert scoped.max_files == 10_001
        assert scoped.max_file_size == Config().max_file_size
        assert max_manifest_bytes == 16 * 1024 * 1024
        raise FreshnessLimitError("freshness file limit exceeded")
    monkeypatch.setattr("graphite.doctor.check_graph_freshness", limited)
    result = check_graph(tmp_path, cfg)
    assert result.status == "blocked"
    assert result.summary == "Graph freshness limit exceeded."


def test_mcp_typescript_and_llm_never_leak_raw_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("graphite.doctor.importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr("graphite.doctor.shutil.which", lambda name: None)
    assert check_mcp().status == "optional"

    monkeypatch.setattr("graphite.doctor._resolve_node_executable", lambda root: tmp_path.parent / "node.exe")
    captured = {}
    def node_probe(executable, root, script, *, timeout_seconds):
        captured["executable"] = executable
        captured["timeout"] = timeout_seconds
        return 0, b'{"ok":true,"version":"5.0"}', False
    monkeypatch.setattr("graphite.doctor._run_node_probe", node_probe)
    ts = check_typescript(tmp_path, timeout_seconds=1.25)
    assert ts.status == "ready"
    assert captured == {"executable": tmp_path.parent / "node.exe", "timeout": 1.25}
    assert "RAW" not in json.dumps(ts.to_dict())

    secret = "CREDENTIAL_SENTINEL"
    llm = check_llm_config(Config(llm_mode="none", llm_provider=" Open_AI ", llm_api_key=secret))
    encoded = json.dumps(llm.to_dict()) + format_doctor_text(build_report(tmp_path, [llm], deep=False, llm_included=False))
    assert secret not in encoded
    assert llm.details == {"mode": "none", "provider": "open-ai", "credential_present": True}
    assert "unused" in llm.summary.lower()


def test_daemon_missing_is_optional_and_exposes_counts_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("graphite.doctor.evaluate_daemon_health", lambda *a, **k: {
        "ok": False, "status": "degraded", "daemon_status": None,
        "errors": [{"code": "status_missing", "message": "RAW C:/secret"}], "warnings": [],
        "process": {"checked": False, "command": "RAW"}, "startup": {"checked": False},
    })
    result = check_daemon(tmp_path, tmp_path)
    assert result.status == "optional"
    assert result.details["registered"] is False
    assert "RAW" not in json.dumps(result.to_dict())


def test_run_doctor_deep_runner_is_injected_and_repo_not_mutated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.doctor._fast_checks", lambda root, cfg, daemon_base: [])
    before = sorted(tmp_path.iterdir())
    fast = run_doctor(tmp_path, Config(), deep=False, include_llm=True)
    assert fast["checks"] == []
    assert fast["llm_included"] is False
    calls = []
    def deep_runner(root, *, cfg, include_llm):
        calls.append((root, cfg, include_llm))
        return [DoctorCheck("deep", "Deep", "ready", "ok")]
    cfg = Config()
    deep = run_doctor(tmp_path, cfg, deep=True, include_llm=True, deep_runner=deep_runner)
    assert calls == [(tmp_path.resolve(), cfg, True)]
    assert deep["llm_included"] is True
    assert sorted(tmp_path.iterdir()) == before


def test_run_doctor_uses_default_daemon_base(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    daemon_base = tmp_path / "daemon-base"
    selected = tmp_path / "selected"
    selected.mkdir()
    observed = []
    monkeypatch.setattr("graphite.doctor.default_projects_root", lambda: daemon_base)
    monkeypatch.setattr("graphite.doctor.check_python", lambda: DoctorCheck("python", "Python", "ready", "ok"))
    monkeypatch.setattr("graphite.doctor.check_git", lambda root: DoctorCheck("git", "Git", "ready", "ok"))
    monkeypatch.setattr("graphite.doctor.check_graph", lambda root, cfg: DoctorCheck("graph", "Graph", "ready", "ok"))
    monkeypatch.setattr("graphite.doctor.check_mcp", lambda: DoctorCheck("mcp", "MCP", "ready", "ok"))
    monkeypatch.setattr("graphite.doctor.check_typescript", lambda root: DoctorCheck("typescript", "TypeScript", "ready", "ok"))
    monkeypatch.setattr("graphite.doctor.check_llm_config", lambda cfg: DoctorCheck("llm", "LLM", "optional", "ok"))
    monkeypatch.setattr("graphite.doctor.evaluate_daemon_health", lambda base, **kwargs: observed.append(base) or {"ok": True, "status": "ok", "daemon_status": "ok", "errors": [], "warnings": [], "process": {}, "startup": {}})
    run_doctor(selected, Config())
    assert observed == [daemon_base.resolve()]


def test_node_resolution_skips_repo_candidate_and_process_env_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_bin = tmp_path / "bin"
    repo_bin.mkdir()
    trusted = tmp_path.parent / "trusted-node"
    trusted.mkdir(exist_ok=True)
    name = "node.exe" if os.name == "nt" else "node"
    for directory in (repo_bin, trusted):
        executable = directory / name
        executable.write_bytes(b"node")
        if os.name != "nt":
            executable.chmod(0o700)
    monkeypatch.setenv("PATH", os.pathsep.join((str(repo_bin), str(trusted))))
    monkeypatch.setenv("NODE_OPTIONS", "SENTINEL")
    captured = {}
    class Process:
        returncode = 0
        stdout = type("Pipe", (), {"read": lambda self, n: b'{"ok":false,"reason":"missing"}', "close": lambda self: None})()
        def wait(self, timeout): return 0
        def kill(self): pass
    def popen(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return Process()
    monkeypatch.setattr("graphite.doctor.subprocess.Popen", popen)
    result = check_typescript(tmp_path)
    assert result.status == "optional"
    assert Path(captured["argv"][0]).resolve() == (trusted / name).resolve()
    assert captured["kwargs"]["shell"] is False
    assert "NODE_OPTIONS" not in captured["kwargs"]["env"]


class _ProbePipe:
    def __init__(self, payload: bytes = b"", *, held: bool = False, close_error: bool = False) -> None:
        self.payload = payload
        self.held = held
        self.close_error = close_error
        self.released = threading.Event()

    def read(self, size: int) -> bytes:
        if self.held:
            self.released.wait(2.0)
        return self.payload

    def close(self) -> None:
        self.released.set()
        if self.close_error:
            raise OSError("RAW close")


class _ProbeProcess:
    def __init__(self, pipe: _ProbePipe, *, timeout: bool = False, kill_error: bool = False) -> None:
        self.stdout = pipe
        self.returncode = None
        self.timeout = timeout
        self.kill_error = kill_error

    def wait(self, timeout: float) -> int:
        if self.timeout:
            raise subprocess.TimeoutExpired("RAW", timeout)
        self.returncode = 0
        return 0

    def kill(self) -> None:
        if self.kill_error:
            raise OSError("RAW kill")
        self.returncode = -9


@pytest.mark.parametrize(
    ("process", "timed_out"),
    [
        (_ProbeProcess(_ProbePipe(b"x" * 501)), False),
        (_ProbeProcess(_ProbePipe(held=True), timeout=True), True),
        (_ProbeProcess(_ProbePipe(b"", close_error=True), timeout=True, kill_error=True), True),
    ],
)
def test_bounded_process_cleanup_finishes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, process: _ProbeProcess, timed_out: bool) -> None:
    monkeypatch.setattr("graphite.doctor.subprocess.Popen", lambda *a, **k: process)
    started = time.monotonic()
    _, output, actual_timeout = doctor_module._run_bounded_process(
        [str(tmp_path / "node")], cwd=tmp_path, env={}, timeout_seconds=0.01, max_stdout_bytes=500
    )
    assert time.monotonic() - started < 1.0
    assert output == b""
    assert actual_timeout is timed_out


def test_typescript_invalid_utf8_is_degraded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("graphite.doctor._resolve_node_executable", lambda root: tmp_path.parent / "node")
    monkeypatch.setattr("graphite.doctor._run_node_probe", lambda *a, **k: (0, b"\xff", False))
    assert check_typescript(tmp_path).status == "degraded"


@pytest.mark.parametrize("selected_state", ["healthy", "failing", "pending", "stale", "missing"])
def test_daemon_classifies_only_selected_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selected_state: str) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    other = tmp_path / "other"
    selected = {"root": str(root), "build_count": 1, "last_error": None}
    if selected_state == "failing":
        selected["last_error"] = "RAW"
    if selected_state == "pending":
        selected["build_count"] = 0
    projects = [{"root": str(other), "build_count": 0, "last_error": "RAW"}]
    if selected_state != "missing":
        projects.append(selected)
    monkeypatch.setattr("graphite.doctor.read_daemon_status", lambda base, state_dir=None: {"projects": projects})
    categories = {"failing": [], "pending": [], "not_built_recently": []}
    if selected_state in {"failing", "pending", "stale"}:
        key = {"failing": "failing", "pending": "pending", "stale": "not_built_recently"}[selected_state]
        categories[key] = [{"root": str(root)}]
    monkeypatch.setattr("graphite.doctor.evaluate_daemon_health", lambda *a, **k: {"ok": False, "status": "degraded", "daemon_status": "ok", "errors": [{"code": "project_failing"}], "warnings": [], "process": {"checked": True, "running": True}, "startup": {"checked": True}, "projects": categories})
    result = check_daemon(root, tmp_path)
    expected = "ready" if selected_state == "healthy" else "optional" if selected_state == "missing" else "degraded"
    assert result.status == expected
    assert result.details["registered"] is (selected_state != "missing")
    assert str(root) not in json.dumps(result.to_dict())


@pytest.mark.parametrize(
    ("issue_kind", "issue_code", "registered"),
    [
        ("errors", "status_stale", True),
        ("errors", "daemon_process_not_running", True),
        ("warnings", "startup_missing", True),
        ("errors", "status_stale", False),
    ],
)
def test_daemon_preserves_global_health_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    issue_kind: str,
    issue_code: str,
    registered: bool,
) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    projects = [{"root": str(root), "build_count": 1, "last_error": None}] if registered else []
    monkeypatch.setattr("graphite.doctor.read_daemon_status", lambda *a, **k: {"projects": projects})
    report = {
        "ok": False,
        "status": "degraded",
        "daemon_status": "ok",
        "errors": [],
        "warnings": [],
        "process": {"checked": True, "running": issue_code != "daemon_process_not_running"},
        "startup": {"checked": True},
        "projects": {"failing": [], "pending": [], "not_built_recently": []},
    }
    report[issue_kind] = [{"code": issue_code, "message": "RAW C:/secret"}]
    monkeypatch.setattr("graphite.doctor.evaluate_daemon_health", lambda *a, **k: report)
    result = check_daemon(root, tmp_path)
    assert result.status == "degraded"
    assert "RAW" not in json.dumps(result.to_dict())
