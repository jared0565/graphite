"""Typed, bounded system-readiness checks."""
from __future__ import annotations

import json
import math
import os
import queue
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import graphite.doctor as doctor_module

from graphite.config import Config
from graphite.cli import main
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


def _cli_report(*, status: str = "ready", exit_code: int = 0) -> dict:
    return {
        "schema_version": 1,
        "root": "repo",
        "deep": False,
        "llm_included": False,
        "status": status,
        "exit_code": exit_code,
        "checks": [],
    }


def test_doctor_cli_json_forwards_scoped_options_and_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "repo"
    daemon_base = tmp_path / "daemon"
    root.mkdir()
    daemon_base.mkdir()
    observed: dict[str, object] = {}

    def fake_run_doctor(selected: Path, **kwargs: object) -> dict:
        observed.update(root=selected, **kwargs)
        return _cli_report(status="blocked", exit_code=1)

    monkeypatch.setattr("graphite.cli.run_doctor", fake_run_doctor)
    assert main([
        "doctor",
        str(root),
        "--daemon-base",
        str(daemon_base),
        "--deep",
        "--include-llm",
        "--json",
    ]) == 1

    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "blocked"
    assert captured.err == ""
    assert observed["root"] == root.resolve()
    assert observed["daemon_base"] == daemon_base.resolve()
    assert observed["deep"] is True
    assert observed["include_llm"] is True
    cfg = observed["cfg"]
    assert isinstance(cfg, Config)
    assert cfg.output_dir == root.resolve() / "graph-out"
    assert cfg.cache_dir == root.resolve() / ".cache" / "graphite"


def test_doctor_cli_text_is_printed_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert "graphite.doctor_probes" not in sys.modules
    monkeypatch.setattr("graphite.cli.run_doctor", lambda *args, **kwargs: _cli_report())
    monkeypatch.setattr("graphite.cli.format_doctor_text", lambda report: "doctor text\n")

    assert main(["doctor", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "doctor text\n"
    assert "graphite.doctor_probes" not in sys.modules


def test_doctor_cli_rejects_llm_without_deep_before_calling_doctor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def fake_run_doctor(*args: object, **kwargs: object) -> dict:
        nonlocal called
        called = True
        return _cli_report()

    monkeypatch.setattr("graphite.cli.run_doctor", fake_run_doctor)
    with pytest.raises(SystemExit) as exc_info:
        main(["doctor", "--include-llm"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--include-llm requires --deep" in captured.err
    assert captured.out == ""
    assert called is False


def test_doctor_cli_invalid_path_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "SECRET-PROJECT-PATH"
    called = False

    def fake_run_doctor(*args: object, **kwargs: object) -> dict:
        nonlocal called
        called = True
        return _cli_report()

    monkeypatch.setattr("graphite.cli.run_doctor", fake_run_doctor)
    invalid = tmp_path / sentinel
    assert main(["doctor", str(invalid)]) == 1

    captured = capsys.readouterr()
    assert "doctor path must be an existing directory" in captured.err
    assert sentinel not in captured.out + captured.err
    assert called is False


def test_doctor_cli_help_describes_optional_synthetic_llm_probe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["doctor", "--help"])

    output = capsys.readouterr().out
    assert exc_info.value.code == 0
    assert "Check Graphite core and optional integration readiness" in output
    assert "--include-llm" in output
    assert "synthetic LLM connectivity probe" in output


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


def test_deep_probe_result_is_immutable_and_error_is_safe() -> None:
    from graphite.probe_process import ProbeProcessError, ProbeProcessResult

    result = ProbeProcessResult(0, b"out", b"err", 0.1)
    with pytest.raises(FrozenInstanceError):
        result.returncode = 1  # type: ignore[misc]
    error = ProbeProcessError("timeout")
    assert error.code == "timeout"
    assert "RAW" not in str(error)


def test_deep_bounded_runner_sanitizes_environment_and_bounds_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from graphite.probe_process import ProbeProcessError, run_bounded_process, sanitized_probe_environment

    monkeypatch.setenv("DOCTOR_SECRET_SENTINEL", "leak")
    monkeypatch.setenv("OPENAI_API_KEY", "leak")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("NODE_OPTIONS", "--inspect")
    script = "import os,sys; bad=[k for k in os.environ if k in {'DOCTOR_SECRET_SENTINEL','OPENAI_API_KEY','GIT_CONFIG_COUNT','NODE_OPTIONS'}]; sys.stdout.write(','.join(bad))"
    env = sanitized_probe_environment()
    assert all(name not in env for name in ("DOCTOR_SECRET_SENTINEL", "OPENAI_API_KEY", "GIT_CONFIG_COUNT", "NODE_OPTIONS"))
    result = run_bounded_process([sys.executable, "-c", script], cwd=tmp_path, timeout_seconds=5)
    assert result.returncode == 0
    assert result.stdout == b""
    for script in ("import sys;sys.stdout.write('x'*40000)", "import sys;sys.stderr.write('x'*40000)"):
        with pytest.raises(ProbeProcessError) as exc_info:
            run_bounded_process([sys.executable, "-c", script], cwd=tmp_path, timeout_seconds=5)
        assert exc_info.value.code == "output_limit"


def test_deep_bounded_runner_times_out_without_leaking_process_output(tmp_path: Path) -> None:
    from graphite.probe_process import ProbeProcessError, run_bounded_process

    started = time.monotonic()
    with pytest.raises(ProbeProcessError) as exc_info:
        run_bounded_process([sys.executable, "-c", "import time;print('RAW C:/secret', flush=True);time.sleep(5)"], cwd=tmp_path, timeout_seconds=0.1)
    assert time.monotonic() - started < 2
    assert exc_info.value.code == "timeout"
    assert "RAW" not in str(exc_info.value)


def test_deep_bounded_runner_handles_nonzero_and_held_descendant_pipe(tmp_path: Path) -> None:
    from graphite.probe_process import ProbeProcessError, run_bounded_process

    with pytest.raises(ProbeProcessError) as exc_info:
        run_bounded_process([sys.executable, "-c", "raise SystemExit(7)"], cwd=tmp_path, timeout_seconds=5)
    assert exc_info.value.code == "nonzero"

    held_pipe = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)'])"
    threads_before = {thread.ident for thread in threading.enumerate()}
    started = time.monotonic()
    result = run_bounded_process([sys.executable, "-c", held_pipe], cwd=tmp_path, timeout_seconds=2)
    assert result.returncode == 0
    assert time.monotonic() - started < 2
    assert [thread for thread in threading.enumerate() if thread.ident not in threads_before] == []


@pytest.mark.parametrize("timeout", [0, -1, math.nan, math.inf, -math.inf])
def test_deep_bounded_runner_rejects_nonfinite_or_nonpositive_timeout(tmp_path: Path, timeout: float) -> None:
    from graphite.probe_process import ProbeProcessError, run_bounded_process

    started = time.monotonic()
    with pytest.raises(ProbeProcessError) as exc_info:
        run_bounded_process([sys.executable, "-c", "raise AssertionError('must not launch')"], cwd=tmp_path, timeout_seconds=timeout)
    assert exc_info.value.code == "invalid_timeout"
    assert time.monotonic() - started < 0.5


def test_deep_bounded_runner_limits_stdin_and_times_out_nonreader(tmp_path: Path) -> None:
    from graphite.probe_process import ProbeProcessError, run_bounded_process

    with pytest.raises(ProbeProcessError) as exc_info:
        run_bounded_process([sys.executable, "-c", "pass"], cwd=tmp_path, stdin=b"x" * (1024 * 1024 + 1), timeout_seconds=5)
    assert exc_info.value.code == "input_limit"

    started = time.monotonic()
    with pytest.raises(ProbeProcessError) as exc_info:
        run_bounded_process(
            [sys.executable, "-c", "import time;time.sleep(5)"],
            cwd=tmp_path,
            stdin=b"x" * (1024 * 1024),
            timeout_seconds=0.25,
        )
    assert exc_info.value.code == "timeout"
    assert time.monotonic() - started < 1.5


def test_probe_transport_rechecks_late_writer_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import graphite.probe_process as transport

    release = threading.Event()
    class InputPipe:
        def write(self, data: bytes) -> int:
            release.wait(1)
            raise BrokenPipeError("RAW late input")
        def flush(self) -> None: pass
        def close(self) -> None: pass
    class OutputPipe:
        def read(self, size: int) -> bytes: return b""
        def close(self) -> None: pass
    class Process:
        pid = 123
        returncode = 0
        stdin = InputPipe()
        stdout = OutputPipe()
        stderr = OutputPipe()
        def wait(self, timeout: float) -> int: return 0
        def kill(self) -> None: self.returncode = -9
    monkeypatch.setattr(transport, "_launch_process", lambda *a, **k: Process())
    monkeypatch.setattr(transport, "_terminate_process_tree", lambda process, deadline: release.set())
    with pytest.raises(transport.ProbeProcessError) as exc_info:
        transport.run_bounded_process(["python"], cwd=tmp_path, stdin=b"small", timeout_seconds=1)
    assert exc_info.value.code == "input_failed"
    assert "RAW" not in str(exc_info.value)


def test_probe_transport_cleans_process_tree_after_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import graphite.probe_process as transport

    class Pipe:
        def read(self, size: int) -> bytes: return b""
        def close(self) -> None: pass
    class Process:
        pid = 123
        returncode = 0
        stdin = None
        stdout = Pipe()
        stderr = Pipe()
        def wait(self, timeout: float) -> int: return 0
        def kill(self) -> None: self.returncode = -9

    cleaned: list[int] = []
    monkeypatch.setattr(transport, "_launch_process", lambda *a, **k: Process())
    monkeypatch.setattr(transport, "_terminate_process_tree", lambda process, deadline: cleaned.append(process.pid))

    result = transport.run_bounded_process(["python"], cwd=tmp_path, timeout_seconds=1)

    assert result.returncode == 0
    assert cleaned == [123]


@pytest.mark.parametrize("failed_start", [1, 2, 3])
def test_probe_transport_thread_start_failure_cleans_every_resource(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_start: int,
) -> None:
    import graphite.probe_process as transport

    class Pipe:
        closed = False
        def read(self, size: int) -> bytes: return b""
        def close(self) -> None: self.closed = True
    class Process:
        pid = 123
        returncode = None
        stdin, stdout, stderr = Pipe(), Pipe(), Pipe()
        terminated = False
        handles_closed = False
        def poll(self) -> None: return None
        def wait(self, timeout: float) -> int: raise subprocess.TimeoutExpired("fake", timeout)
        def kill(self) -> None: self.returncode = -9
        def terminate_tree(self) -> bool:
            self.terminated = True
            self.returncode = -9
            return True
        def close_handles(self) -> bool:
            self.handles_closed = True
            return True
    process = Process()
    real_thread = transport.threading.Thread
    starts = 0
    class FailingThread(real_thread):
        def start(self) -> None:
            nonlocal starts
            starts += 1
            if starts == failed_start:
                raise RuntimeError("injected thread start failure")
            super().start()
    monkeypatch.setattr(transport, "_launch_process", lambda *args, **kwargs: process)
    monkeypatch.setattr(transport.threading, "Thread", FailingThread)

    with pytest.raises(transport.ProbeProcessError) as exc_info:
        transport.run_bounded_process(["python"], cwd=tmp_path, timeout_seconds=1)

    assert exc_info.value.code == "io_failed"
    assert process.terminated and process.handles_closed
    assert all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))


@pytest.mark.parametrize("failure", ["terminate", "close"])
def test_probe_transport_cleanup_failure_never_returns_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    import graphite.probe_process as transport

    class Pipe:
        def read(self, size: int) -> bytes: return b""
        def close(self) -> None: pass
    class Process:
        pid = 123
        returncode = 0
        stdin = None
        stdout, stderr = Pipe(), Pipe()
        def wait(self, timeout: float) -> int: return 0
        def kill(self) -> bool: return False
        def terminate_tree(self) -> bool: return failure != "terminate"
        def close_handles(self) -> bool: return failure != "close"
    monkeypatch.setattr(transport, "_launch_process", lambda *args, **kwargs: Process())

    with pytest.raises(transport.ProbeProcessError) as exc_info:
        transport.run_bounded_process(["python"], cwd=tmp_path, timeout_seconds=1)

    assert exc_info.value.code == "cleanup_failed"


def test_probe_transport_accepts_small_input(tmp_path: Path) -> None:
    from graphite.probe_process import run_bounded_process

    result = run_bounded_process(
        [sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        cwd=tmp_path,
        stdin="small input",
        timeout_seconds=5,
    )
    assert result.stdout == b"small input"


def test_probe_transport_can_return_a_bounded_nonzero_result_when_requested(tmp_path: Path) -> None:
    from graphite.probe_process import run_bounded_process

    result = run_bounded_process(
        [sys.executable, "-c", "raise SystemExit(3)"],
        cwd=tmp_path,
        timeout_seconds=5,
        check=False,
    )
    assert result.returncode == 3


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _process_identity_exited(pid: int) -> bool:
    if os.name != "nt":
        return not _pid_exists(pid)
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 87
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0
    finally:
        kernel32.CloseHandle(handle)


def test_deep_bounded_runner_terminates_descendant_tree(tmp_path: Path) -> None:
    from graphite.probe_process import ProbeProcessError, run_bounded_process

    pid_file = tmp_path / "descendant.pid"
    child = "import time;time.sleep(10)"
    parent = (
        "import pathlib,subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}]);"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid));"
        "time.sleep(10)"
    )
    errors: queue.Queue[ProbeProcessError] = queue.Queue()
    transport_timeout = 2.0
    def run() -> None:
        try:
            run_bounded_process([sys.executable, "-c", parent], cwd=tmp_path, timeout_seconds=transport_timeout)
        except ProbeProcessError as error:
            errors.put(error)
    runner = threading.Thread(target=run)
    runner.start()
    ready_deadline = time.monotonic() + transport_timeout * 0.75
    while not pid_file.exists() and time.monotonic() < ready_deadline:
        time.sleep(0.01)
    assert pid_file.exists(), "parent did not publish descendant PID before its bounded deadline"
    pid = int(pid_file.read_text(encoding="utf-8"))
    retained_handle = None
    kernel32 = None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        retained_handle = kernel32.OpenProcess(0x00100000, False, pid)
        assert retained_handle
    runner.join(transport_timeout + 0.5)
    assert not runner.is_alive()
    assert errors.get_nowait().code == "timeout"
    if retained_handle is not None and kernel32 is not None:
        try:
            assert kernel32.WaitForSingleObject(retained_handle, 1000) == 0
        finally:
            kernel32.CloseHandle(retained_handle)
        return
    try:
        deadline = time.monotonic() + 1
        while not _process_identity_exited(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _process_identity_exited(pid)
    finally:
        if os.name != "nt" and _pid_exists(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


@pytest.mark.parametrize("detached_stdio", [False, True])
def test_deep_bounded_runner_kills_descendant_after_leader_exits(tmp_path: Path, detached_stdio: bool) -> None:
    from graphite.probe_process import run_bounded_process

    pid_file = tmp_path / "leader-exited-descendant.pid"
    child = "import time;time.sleep(10)"
    stdio = ",stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL" if detached_stdio else ""
    parent = (
        "import pathlib,subprocess,sys;"
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}]{stdio});"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))"
    )

    result = run_bounded_process([sys.executable, "-c", parent], cwd=tmp_path, timeout_seconds=2)

    assert result.returncode == 0
    pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        deadline = time.monotonic() + 1
        while not _process_identity_exited(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _process_identity_exited(pid)
    finally:
        if os.name != "nt" and _pid_exists(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


def test_windows_job_environment_block_is_sorted_double_nul_and_rejects_nul() -> None:
    from graphite.process_contracts import build_windows_environment_block

    assert build_windows_environment_block({"z": "last", "A": "first"}) == "A=first\0z=last\0\0"
    with pytest.raises(ValueError):
        build_windows_environment_block({"SAFE": "bad\0value"})


def test_windows_contract_builders_import_without_msvcrt(tmp_path: Path) -> None:
    trusted_source = str((Path(__file__).resolve().parents[1] / "src").resolve())
    script = (
        f"import sys;sys.path.insert(0,{trusted_source!r});"
        "import builtins;original=builtins.__import__;"
        "builtins.__import__=lambda name,*a,**k: (_ for _ in ()).throw(ImportError('blocked')) "
        "if name=='msvcrt' else original(name,*a,**k);"
        "import graphite.process_contracts as contracts;"
        "import graphite.windows_job;"
        "assert contracts.build_windows_environment_block({'A':'1'}) == 'A=1\\0\\0'"
    )
    completed = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")


@pytest.mark.skipif(os.name != "nt", reason="Windows native launcher contract")
@pytest.mark.parametrize("failure", ["job_create", "job_configure", "pipe", "pipe_configure", "attribute_init", "attribute_update", "child_configure", "child_configure_restore", "child_restore", "child_restore_close", "process_create", "process_create_restore", "assign", "resume", "file_wrap"])
def test_windows_job_launch_failures_close_each_owned_handle_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    import ctypes
    import graphite.windows_job as native

    class FakeKernel32:
        def __init__(self) -> None:
            self.next_handle = 10
            self.pipe_calls = 0
            self.created: list[int] = []
            self.open_handles: set[int] = set()
            self.inheritable_handles: set[int] = set()
            self.closed: list[int] = []
            self.close_attempts: list[int] = []
            self.terminated: list[int] = []
            self.terminated_processes: list[int] = []
            self.launch_contract: dict[str, object] = {}
            self.handle_list: tuple[int, ...] = ()
            self.environment_block = ""
            self.handle_flags: list[tuple[int, int]] = []
            self.legacy_acquired = threading.Event()
            self.legacy_thread: threading.Thread | None = None
            self.legacy_saw_open_handle: bool | None = None

        def start_legacy_launch(self) -> None:
            def legacy_launch() -> None:
                with native.WINDOWS_PROCESS_CREATION_LOCK:
                    self.legacy_saw_open_handle = bool(self.open_handles & self.inheritable_handles)
                    self.legacy_acquired.set()
            self.legacy_thread = threading.Thread(target=legacy_launch)
            self.legacy_thread.start()
            assert not self.legacy_acquired.wait(0.02)

        def CreateJobObjectW(self, security: object, name: object) -> int:
            return 0 if failure == "job_create" else 1
        def SetInformationJobObject(self, *args: object) -> bool:
            return failure != "job_configure"
        def CreatePipe(self, read: object, write: object, security: object, size: int) -> bool:
            assert not security._obj.bInheritHandle  # type: ignore[attr-defined]
            self.pipe_calls += 1
            if failure == "pipe" and self.pipe_calls == 2:
                return False
            read._obj.value = self.next_handle  # type: ignore[attr-defined]
            write._obj.value = self.next_handle + 1  # type: ignore[attr-defined]
            self.created.extend((self.next_handle, self.next_handle + 1))
            self.open_handles.update((self.next_handle, self.next_handle + 1))
            self.next_handle += 2
            return True
        def SetHandleInformation(self, handle: int, mask: int, flags: int) -> bool:
            self.handle_flags.append((handle, flags))
            child_enables = [entry for entry in self.handle_flags if entry[1] == 1]
            if failure in {"child_configure", "child_configure_restore"} and flags == 1 and len(child_enables) == 2:
                if failure == "child_configure_restore":
                    self.start_legacy_launch()
                return False
            restore_attempts = [entry for entry in self.handle_flags if entry == (13, 0)]
            if failure in {"child_restore", "child_restore_close"} and flags == 0 and child_enables and handle == 13 and len(restore_attempts) == 1:
                return False
            first_handle_restores = [entry for entry in self.handle_flags if entry == (10, 0)]
            if failure == "child_configure_restore" and flags == 0 and handle == 10 and len(first_handle_restores) == 1:
                return False
            if failure == "process_create_restore" and flags == 0 and handle == 13 and len(restore_attempts) == 1:
                return False
            if flags == 1:
                self.inheritable_handles.add(handle)
            else:
                self.inheritable_handles.discard(handle)
            return failure != "pipe_configure"
        def InitializeProcThreadAttributeList(self, pointer: object, count: int, flags: int, size: object) -> bool:
            if pointer is None:
                size._obj.value = 128  # type: ignore[attr-defined]
                return False
            return failure != "attribute_init"
        def UpdateProcThreadAttribute(self, *args: object) -> bool:
            handles = ctypes.cast(args[3], ctypes.POINTER(native.wintypes.HANDLE * 3)).contents
            self.handle_list = tuple(handles)
            return failure != "attribute_update"
        def DeleteProcThreadAttributeList(self, pointer: object) -> None: pass
        def CreateProcessW(self, *args: object) -> bool:
            startup = args[8].contents  # type: ignore[attr-defined]
            environment = args[6]
            characters: list[str] = []
            index = 0
            while len(characters) < 2 or characters[-2:] != ["\0", "\0"]:
                characters.append(environment[index])
                index += 1
            self.environment_block = "".join(characters)
            self.launch_contract = {
                "command": args[1].value,  # type: ignore[attr-defined]
                "inherit": args[4],
                "flags": args[5],
                "cwd": args[7],
                "stdio": (startup.hStdInput, startup.hStdOutput, startup.hStdError),
            }
            if failure in {"resume", "child_restore", "child_restore_close", "process_create_restore"}:
                self.start_legacy_launch()
            if failure in {"process_create", "process_create_restore"}:
                return False
            info = args[-1]._obj  # type: ignore[attr-defined]
            info.hProcess, info.hThread, info.dwProcessId = 30, 31, 32
            return True
        def AssignProcessToJobObject(self, *args: object) -> bool: return failure != "assign"
        def ResumeThread(self, thread: int) -> int: return 0xFFFFFFFF if failure == "resume" else 1
        def TerminateJobObject(self, job: int, code: int) -> bool:
            if failure in {"process_create_restore", "child_configure_restore"}:
                assert self.legacy_acquired.wait(1)
            self.terminated.append(job)
            return True
        def TerminateProcess(self, process: int, code: int) -> bool:
            self.terminated_processes.append(process)
            return True
        def CloseHandle(self, handle: int) -> bool:
            self.close_attempts.append(handle)
            if failure == "child_restore_close" and handle == 13 and self.close_attempts.count(13) == 1:
                return False
            self.closed.append(handle)
            self.open_handles.discard(handle)
            self.inheritable_handles.discard(handle)
            return True

    api = FakeKernel32()
    monkeypatch.setattr(native, "_kernel32", lambda: api)
    closed_fds: list[int] = []
    if failure == "file_wrap":
        import msvcrt
        monkeypatch.setattr(msvcrt, "open_osfhandle", lambda handle, flags: 100)
        monkeypatch.setattr(native.io, "FileIO", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("wrap failed")))
        monkeypatch.setattr(native.os, "close", closed_fds.append)

    with pytest.raises(OSError):
        native.launch(["python", "arg with spaces"], cwd=tmp_path, environment={"A": "1"}, with_stdin=False)

    assert len(api.closed) == len(set(api.closed))
    if failure == "file_wrap":
        assert closed_fds == [100]
        assert 11 not in api.closed
        assert set(api.created) - {11} <= set(api.closed)
    else:
        assert set(api.created) <= set(api.closed)
    if failure == "resume":
        assert api.launch_contract == {
            "command": subprocess.list2cmdline(["python", "arg with spaces"]),
            "inherit": True,
            "flags": (
                native.CREATE_SUSPENDED
                | native.CREATE_NEW_PROCESS_GROUP
                | native.CREATE_UNICODE_ENVIRONMENT
                | native.EXTENDED_STARTUPINFO_PRESENT
            ),
            "cwd": str(tmp_path),
            "stdio": (10, 13, 15),
        }
        assert api.handle_list == (10, 13, 15)
        assert api.environment_block == "A=1\0\0"
        assert api.handle_flags[-6:] == [(10, 1), (13, 1), (15, 1), (10, 0), (13, 0), (15, 0)]
        assert api.legacy_acquired.wait(1)
        assert api.legacy_thread is not None
        api.legacy_thread.join(1)
    if failure in {"process_create", "assign", "resume"}:
        assert api.terminated == [1]
    if failure in {"child_restore", "child_restore_close", "assign", "resume", "file_wrap"}:
        assert api.terminated_processes == [30]
    if failure == "child_configure":
        assert api.handle_flags[-3:] == [(10, 1), (13, 1), (10, 0)]
    if failure == "child_restore":
        assert api.legacy_acquired.wait(1)
        assert api.legacy_saw_open_handle is False
        assert api.closed.count(13) == 1
    if failure == "child_restore_close":
        assert api.legacy_acquired.wait(1)
        assert api.legacy_saw_open_handle is False
        assert api.close_attempts.count(13) == 2
        assert api.closed.count(13) == 1
    if failure in {"process_create_restore", "child_configure_restore"}:
        assert api.legacy_acquired.wait(1)
        assert api.legacy_saw_open_handle is False
        assert api.terminated_processes == []


def test_cancel_synchronous_io_preserves_pointer_width_and_closes_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes
    import graphite.probe_process as transport

    calls: list[tuple[str, int]] = []
    high_handle = 0x1_0000_1234
    class Function:
        argtypes: object = None
        restype: object = None
        def __init__(self, name: str, result: object) -> None:
            self.name, self.result = name, result
        def __call__(self, *args: object) -> object:
            if args:
                calls.append((self.name, int(args[0])))
            return self.result
    class API:
        OpenThread = Function("open", high_handle)
        CancelSynchronousIo = Function("cancel", True)
        CloseHandle = Function("close", True)
    api = API()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api)
    monkeypatch.setattr(transport.os, "name", "nt")
    thread = type("Thread", (), {"native_id": 77})()

    transport._cancel_synchronous_io(thread)  # type: ignore[arg-type]

    assert calls == [("open", 1), ("cancel", high_handle), ("close", high_handle)]
    assert api.OpenThread.restype is ctypes.wintypes.HANDLE


def test_posix_cleanup_skips_sigkill_when_group_exits_during_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    import graphite.probe_process as transport

    signals: list[int] = []
    def killpg(pid: int, sig: int) -> None:
        signals.append(sig)
        if sig == 0:
            raise ProcessLookupError
    process = type("Process", (), {"pid": 42, "returncode": 0, "kill": lambda self: None})()
    monkeypatch.setattr(transport.os, "name", "posix")
    monkeypatch.setattr(transport.os, "killpg", killpg, raising=False)

    transport._terminate_process_tree(process, time.monotonic() + 1)  # type: ignore[arg-type]

    assert signals[0] == signal.SIGTERM
    assert all(sig != 9 for sig in signals)


def test_posix_cleanup_graces_surviving_group_then_sigkills(monkeypatch: pytest.MonkeyPatch) -> None:
    import graphite.probe_process as transport

    signals: list[int] = []
    ticks = iter((0.0, 0.0, 0.04, 0.11, 0.11))
    process = type("Process", (), {"pid": 42, "returncode": 0, "kill": lambda self: None})()
    monkeypatch.setattr(transport.os, "name", "posix")
    monkeypatch.setattr(transport.os, "killpg", lambda pid, sig: signals.append(sig), raising=False)
    monkeypatch.setattr(transport.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(transport.time, "sleep", lambda duration: None)

    transport._terminate_process_tree(process, 1.0)  # type: ignore[arg-type]

    assert signals[-1] == 9
    assert signals.count(0) >= 2


def test_windows_launch_failure_never_falls_back_to_popen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import graphite.probe_process as transport
    import graphite.windows_job as native

    monkeypatch.setattr(native, "launch", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("native failed")))
    monkeypatch.setattr(transport.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fallback")))

    with pytest.raises(transport.ProbeProcessError) as exc_info:
        transport.run_bounded_process(["python"], cwd=tmp_path, timeout_seconds=1)

    assert exc_info.value.code == "launch_failed"


@pytest.mark.skipif(os.name != "nt", reason="Windows CreateProcessW argv contract")
def test_windows_native_launcher_round_trips_hostile_argv(tmp_path: Path) -> None:
    from graphite.probe_process import run_bounded_process

    hostile = ["", "space value", "trailing\\", 'quote"inside', 'slashes\\\\\"quote']
    script = "import json,sys;print(json.dumps(sys.argv[1:]))"

    result = run_bounded_process([sys.executable, "-c", script, *hostile], cwd=tmp_path, timeout_seconds=5)

    assert json.loads(result.stdout) == hostile


@pytest.mark.skipif(os.name != "nt", reason="Windows CreateProcessW environment contract")
def test_windows_native_launcher_child_observes_only_sanitized_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from graphite.probe_process import run_bounded_process, sanitized_probe_environment

    monkeypatch.setenv("DOCTOR_UNRELATED_SECRET", "must-not-inherit")
    expected = sanitized_probe_environment()
    script = "import json,os;print(json.dumps(dict(os.environ),sort_keys=True))"

    result = run_bounded_process([sys.executable, "-c", script], cwd=tmp_path, timeout_seconds=5)

    assert json.loads(result.stdout) == expected
    assert "DOCTOR_UNRELATED_SECRET" not in result.stdout.decode("utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-list inheritance contract")
def test_windows_native_launcher_does_not_inherit_unrelated_inheritable_handle(tmp_path: Path) -> None:
    import msvcrt
    from graphite.probe_process import run_bounded_process

    read_fd, write_fd = os.pipe()
    handle = msvcrt.get_osfhandle(read_fd)
    os.set_handle_inheritable(handle, True)
    script = (
        "import ctypes,sys;from ctypes import wintypes;"
        "api=ctypes.WinDLL('kernel32',use_last_error=True);"
        "api.GetHandleInformation.argtypes=(wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD));"
        "api.GetHandleInformation.restype=wintypes.BOOL;flags=wintypes.DWORD();"
        "print(int(bool(api.GetHandleInformation(int(sys.argv[1]),ctypes.byref(flags)))))"
    )
    try:
        result = run_bounded_process([sys.executable, "-c", script, str(handle)], cwd=tmp_path, timeout_seconds=5)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert result.stdout.strip() == b"0"


def test_windows_job_process_is_idempotent_and_preserves_unsigned_exit_status() -> None:
    import ctypes
    import graphite.windows_job as native

    calls: list[tuple[str, int]] = []
    class API:
        def GetExitCodeProcess(self, handle: int, code: object) -> bool:
            calls.append(("poll", handle))
            ctypes.cast(code, ctypes.POINTER(native.wintypes.DWORD)).contents.value = 0xFFFFFFFE
            return True
        def WaitForSingleObject(self, handle: int, timeout: int) -> int: return native.WAIT_OBJECT_0
        def TerminateJobObject(self, handle: int, code: int) -> bool:
            calls.append(("kill", handle))
            return True
        def CloseHandle(self, handle: int) -> bool:
            calls.append(("close", handle))
            return True
    pipe = type("Pipe", (), {})()
    process = native.JobProcess(API(), 0x1_0000_0002, 0x1_0000_0001, 7, None, pipe, pipe)

    assert process.poll() == 0xFFFFFFFE
    assert process.poll() == 0xFFFFFFFE
    assert process.wait(0) == 0xFFFFFFFE
    assert process.kill()
    assert process.close_handles()
    assert process.close_handles()
    assert calls.count(("poll", 0x1_0000_0002)) == 1
    assert calls.count(("close", 0x1_0000_0001)) == 1
    assert calls.count(("close", 0x1_0000_0002)) == 1


def test_posix_process_observes_before_cleanup_and_reaps_afterward(monkeypatch: pytest.MonkeyPatch) -> None:
    import graphite.probe_process as transport

    events: list[str] = []
    leader = type("Leader", (), {"pid": 44, "stdin": None, "stdout": None, "stderr": None, "returncode": None})()
    observed = type("WaitId", (), {"si_status": 0, "si_code": 1})()
    monkeypatch.setattr(transport.os, "CLD_EXITED", 1, raising=False)
    monkeypatch.setattr(transport.os, "P_PID", 1, raising=False)
    monkeypatch.setattr(transport.os, "WEXITED", 1, raising=False)
    monkeypatch.setattr(transport.os, "WNOHANG", 2, raising=False)
    monkeypatch.setattr(transport.os, "WNOWAIT", 4, raising=False)
    monkeypatch.setattr(transport.os, "waitid", lambda *args: events.append("observe") or observed, raising=False)
    monkeypatch.setattr(transport.os, "waitpid", lambda *args: events.append("reap") or (44, 0), raising=False)
    monkeypatch.setattr(transport.os, "waitstatus_to_exitcode", lambda status: 0, raising=False)
    process = transport._PosixProcess(leader)  # type: ignore[arg-type]
    assert process.wait(0) == 0
    monkeypatch.setattr(transport, "_terminate_process_tree", lambda *args: events.append("contain") or True)

    assert transport._cleanup_process_transport(process, [], [], time.monotonic() + 1)

    assert events == ["observe", "contain", "reap"]


def test_posix_reap_is_deadline_bounded_when_child_is_not_waitable(monkeypatch: pytest.MonkeyPatch) -> None:
    import graphite.probe_process as transport

    leader = type("Leader", (), {"pid": 44, "stdin": None, "stdout": None, "stderr": None, "returncode": None})()
    observed = type("WaitId", (), {"si_status": 0, "si_code": 1})()
    ticks = iter((0.0, 0.0, 0.01, 0.02, 0.03))
    monkeypatch.setattr(transport.os, "CLD_EXITED", 1, raising=False)
    monkeypatch.setattr(transport.os, "P_PID", 1, raising=False)
    monkeypatch.setattr(transport.os, "WEXITED", 1, raising=False)
    monkeypatch.setattr(transport.os, "WNOHANG", 2, raising=False)
    monkeypatch.setattr(transport.os, "WNOWAIT", 4, raising=False)
    monkeypatch.setattr(transport.os, "waitid", lambda *args: observed, raising=False)
    monkeypatch.setattr(transport.os, "waitpid", lambda *args: (0, 0), raising=False)
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(transport.time, "sleep", lambda duration: None)
    process = transport._PosixProcess(leader)  # type: ignore[arg-type]
    assert process.wait(0) == 0
    monkeypatch.setattr(transport, "_terminate_process_tree", lambda *args: True)

    assert not transport._cleanup_process_transport(process, [], [], 0.025)
    assert not process._reaped


@pytest.mark.parametrize(
    ("raw_status", "expected", "returned_flags"),
    [(7 << 8, 7, 0x84000000), (9, -9, 0x84000000), (7 << 8, None, 0x80000000)],
)
def test_posix_observer_uses_macos_kqueue_when_waitid_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    raw_status: int,
    expected: int | None,
    returned_flags: int,
) -> None:
    import graphite.probe_process as transport

    controls: list[tuple[int, int]] = []
    note_exit, note_exit_status = 0x80000000, 0x04000000
    event = type("Event", (), {"data": raw_status, "fflags": returned_flags})()
    registrations: list[int] = []
    class Kqueue:
        def control(self, changes: object, max_events: int, timeout: int) -> list[object]:
            controls.append((max_events, timeout))
            return [] if changes is not None else [event]
        def close(self) -> None: controls.append((-1, -1))
    fake_select = type("Select", (), {
        "KQ_FILTER_PROC": -5, "KQ_EV_ADD": 1, "KQ_EV_ENABLE": 2,
        "KQ_EV_ONESHOT": 4, "KQ_NOTE_EXIT": note_exit,
        "KQ_NOTE_EXITSTATUS": note_exit_status,
        "kqueue": lambda self: Kqueue(),
        "kevent": lambda self, *args, **kwargs: registrations.append(kwargs["fflags"]) or object(),
    })()
    monkeypatch.delattr(transport.os, "waitid", raising=False)
    monkeypatch.setattr(transport.sys, "platform", "darwin")
    monkeypatch.setattr(transport, "select", fake_select)
    leader = type("Leader", (), {"pid": 44, "stdin": None, "stdout": None, "stderr": None, "returncode": None})()

    process = transport._PosixProcess(leader)  # type: ignore[arg-type]

    if expected is None:
        with pytest.raises(OSError):
            process.poll()
    else:
        assert process.poll() == expected
    assert registrations == [note_exit | note_exit_status]
    assert controls[:2] == [(0, 0), (1, 0)]


def test_posix_launch_fails_closed_before_spawn_without_safe_observer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import graphite.probe_process as transport

    monkeypatch.delattr(transport.os, "waitid", raising=False)
    monkeypatch.setattr(transport.os, "name", "posix")
    monkeypatch.setattr(transport.sys, "platform", "freebsd")
    monkeypatch.setattr(
        transport.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    with pytest.raises(transport.ProbeProcessError) as exc_info:
        transport.run_bounded_process(["python"], cwd=tmp_path, timeout_seconds=1)

    assert exc_info.value.code == "launch_failed"


def test_core_deep_probe_runs_real_pipeline_without_touching_selected_root(tmp_path: Path) -> None:
    from graphite.doctor_probes import probe_core_pipeline

    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "keep.txt").write_text("unchanged", encoding="utf-8")
    before = [(entry.name, entry.read_bytes()) for entry in selected.iterdir()]
    check = probe_core_pipeline(selected, timeout_seconds=30)
    after = [(entry.name, entry.read_bytes()) for entry in selected.iterdir()]
    assert check.status == "ready"
    assert check.code == "deep_core"
    assert check.label == "Deterministic pipeline"
    assert check.details["node_count"] > 0
    assert check.details["commands_completed"] == 3
    assert set(check.details) == {"node_count", "edge_count", "duration_ms", "commands_completed"}
    assert before == after
    assert str(tmp_path) not in json.dumps(check.to_dict())


def test_core_deep_probe_uses_exact_offline_command_contract(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    selected = tmp_path / "selected"
    selected.mkdir()
    calls: list[tuple[list[str], Path]] = []
    outputs = iter([
        b"",
        b'{"ok":true,"error_count":0,"errors":[],"node_count":2,"edge_count":1}',
        b'{"node_count":2,"edge_count":1}',
    ])
    def run(argv: list[str], *, cwd: Path, **kwargs: object) -> object:
        calls.append((argv, cwd))
        return probes.ProbeProcessResult(0, next(outputs), b"", 0.01)
    check = probes.probe_core_pipeline(selected, python_executable="PYTHON", _runner=run)
    assert check.status == "ready"
    work = calls[0][1]
    repo, out, cache, graph = work / "repo", work / "out", work / "cache", work / "out" / "graph.json"
    assert calls == [
        (["PYTHON", "-B", "-m", "graphite", "--output-dir", str(out), "--cache-dir", str(cache), "--llm", "none", "build", str(repo)], work),
        (["PYTHON", "-B", "-m", "graphite", "validate", "--graph-json", str(graph), "--json"], work),
        (["PYTHON", "-B", "-m", "graphite", "query", "stats", "--graph-json", str(graph)], work),
    ]
    assert all("include-llm" not in argv for argv, _cwd in calls)


@pytest.mark.parametrize(("failure_code", "expected_type"), [("timeout", "timeout"), ("output_limit", "output_limit"), ("nonzero", "process")])
def test_core_deep_probe_maps_runner_failures_safely(tmp_path: Path, failure_code: str, expected_type: str) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_process import ProbeProcessError

    def fail(*args: object, **kwargs: object) -> object:
        raise ProbeProcessError(failure_code)
    check = probes.probe_core_pipeline(tmp_path, timeout_seconds=1, _runner=fail)
    encoded = json.dumps(check.to_dict())
    assert check.status == "blocked"
    assert check.details == {"error_type": expected_type, "code": failure_code}
    assert "RAW" not in encoded
    assert str(tmp_path) not in encoded


def test_core_deep_probe_blocks_malformed_and_invalid_validation(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    outputs = iter([probes.ProbeProcessResult(0, b"", b"", 0.01), probes.ProbeProcessResult(0, b'{"ok":false,"error_count":1}', b"", 0.01)])
    check = probes.probe_core_pipeline(tmp_path, _runner=lambda *a, **k: next(outputs))
    assert check.status == "blocked"
    assert check.details == {"error_type": "validation", "code": "validation_failed"}
    malformed = probes.probe_core_pipeline(tmp_path, _runner=lambda *a, **k: probes.ProbeProcessResult(0, b"not-json", b"", 0.01))
    assert malformed.status == "blocked"
    assert malformed.details == {"error_type": "response", "code": "malformed_output"}


def test_core_deep_probe_rejects_temp_path_outside_os_temp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    selected = tmp_path / "selected"
    selected.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    def unsafe_factory(**kwargs: object) -> str:
        return str(outside)
    check = probes.probe_core_pipeline(
        selected,
        _temp_factory=unsafe_factory,
        _temp_root_resolver=lambda: tmp_path / "claimed-temp",
    )
    assert check.status == "blocked"
    assert check.details == {"error_type": "isolation", "code": "unsafe_temp_path"}
    assert list(outside.iterdir()) == []


def test_core_deep_probe_rejects_work_overlapping_selected_before_writes(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    selected = tmp_path / "selected"
    selected.mkdir()
    work = selected / "work"
    work.mkdir()
    def overlapping_factory(**kwargs: object) -> str:
        return str(work)
    check = probes.probe_core_pipeline(
        selected,
        _temp_factory=overlapping_factory,
        _temp_root_resolver=lambda: tmp_path,
    )
    assert check.status == "blocked"
    assert check.details == {"error_type": "isolation", "code": "overlapping_temp_path"}
    assert list(work.iterdir()) == []


@pytest.mark.parametrize("relationship", ["equal", "ancestor"])
def test_core_deep_probe_rejects_selected_root_containing_temp_before_creation(tmp_path: Path, relationship: str) -> None:
    import graphite.doctor_probes as probes

    selected = tmp_path
    temp_root = tmp_path if relationship == "equal" else tmp_path / "os-temp"
    temp_root.mkdir(exist_ok=True)
    created = False
    def factory(**kwargs: object) -> object:
        nonlocal created
        created = True
        raise AssertionError("temporary directory must not be created")
    check = probes.probe_core_pipeline(selected, _temp_factory=factory, _temp_root_resolver=lambda: temp_root)
    assert check.status == "blocked"
    assert check.details == {"error_type": "isolation", "code": "selected_contains_temp"}
    assert created is False


def test_core_deep_probe_allows_selected_sibling_under_temp(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    selected = tmp_path / "selected"
    selected.mkdir()
    outputs = iter([b"", b'{"ok":true,"error_count":0,"errors":[],"node_count":2,"edge_count":1}', b'{"node_count":2,"edge_count":1}'])
    def factory(**kwargs: object) -> object:
        return tempfile.mkdtemp(dir=tmp_path, **kwargs)
    def run(*args: object, **kwargs: object) -> object:
        return probes.ProbeProcessResult(0, next(outputs), b"", 0.01)
    check = probes.probe_core_pipeline(selected, _runner=run, _temp_factory=factory, _temp_root_resolver=lambda: tmp_path)
    assert check.status == "ready"
    assert list(selected.iterdir()) == []


@pytest.mark.parametrize(
    "validation",
    [
        {"ok": False, "valid": False, "error_count": 1, "errors": []},
        {"ok": True, "error_count": 0, "errors": [{"code": "RAW"}], "node_count": 2},
        {"ok": True, "error_count": 0, "errors": []},
        {"ok": True, "error_count": 0, "errors": [], "node_count": 0},
        {"ok": True, "error_count": 0, "errors": [], "node_count": "2"},
    ],
)
def test_core_deep_probe_rejects_invalid_validation_contract(tmp_path: Path, validation: dict[str, object]) -> None:
    import graphite.doctor_probes as probes

    outputs = iter([b"", json.dumps(validation).encode(), b'{"node_count":2,"edge_count":1}'])
    check = probes.probe_core_pipeline(tmp_path, _runner=lambda *a, **k: probes.ProbeProcessResult(0, next(outputs), b"", 0.01))
    assert check.status == "blocked"
    assert check.details == {"error_type": "validation", "code": "validation_failed"}
    assert "RAW" not in json.dumps(check.to_dict())


def test_core_deep_probe_cleans_temp_after_success_and_failure(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_process import ProbeProcessError

    created: list[Path] = []
    def tracking_factory(**kwargs: object) -> str:
        value = tempfile.mkdtemp(**kwargs)
        created.append(Path(value))
        return value

    assert probes.probe_core_pipeline(tmp_path, _temp_factory=tracking_factory).status == "ready"
    assert created and not created[-1].exists()
    def fail(*args: object, **kwargs: object) -> object:
        raise ProbeProcessError("nonzero")
    assert probes.probe_core_pipeline(tmp_path, _runner=fail, _temp_factory=tracking_factory).status == "blocked"
    assert not created[-1].exists()


def test_core_deep_probe_uses_one_total_budget_and_maps_cleanup_error(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    timeouts: list[float] = []
    outputs = iter([
        b"",
        b'{"ok":true,"error_count":0,"errors":[],"node_count":2,"edge_count":1}',
        b'{"node_count":2,"edge_count":1}',
    ])
    def run(*args: object, timeout_seconds: float, **kwargs: object) -> object:
        timeouts.append(timeout_seconds)
        time.sleep(0.01)
        return probes.ProbeProcessResult(0, next(outputs), b"", 0.01)
    assert probes.probe_core_pipeline(tmp_path, timeout_seconds=1, _runner=run).status == "ready"
    assert timeouts[0] > timeouts[1] > timeouts[2] > 0

    def cleanup_failure(path: Path) -> None:
        shutil.rmtree(path)
        raise RuntimeError("RAW cleanup path")
    outputs = iter([b"", b'{"ok":true,"error_count":0,"errors":[],"node_count":2,"edge_count":1}', b'{"node_count":2,"edge_count":1}'])
    check = probes.probe_core_pipeline(tmp_path, _runner=run, _temp_cleanup=cleanup_failure)
    assert check.status == "blocked"
    assert check.details == {"error_type": "cleanup", "code": "cleanup_failed"}
    assert "RAW" not in json.dumps(check.to_dict())


def test_core_probe_deadline_includes_temp_creation_and_verified_cleanup(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    selected = tmp_path / "selected"
    selected.mkdir()
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    created: list[Path] = []
    now = [0.0]
    def factory(**kwargs: object) -> str:
        path = Path(tempfile.mkdtemp(dir=temp_root, prefix="graphite-doctor-"))
        created.append(path)
        now[0] = 0.95
        return str(path)
    check = probes.probe_core_pipeline(
        selected,
        timeout_seconds=1,
        _clock=lambda: now[0],
        _temp_factory=factory,
        _temp_root_resolver=lambda: temp_root,
    )
    assert check.status == "blocked"
    assert check.details == {"error_type": "timeout", "code": "timeout"}
    assert created and not created[0].exists()


def test_core_probe_blocking_cleanup_is_bounded_and_never_ready(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    release = threading.Event()
    created: list[Path] = []
    outputs = iter([
        b"",
        b'{"ok":true,"error_count":0,"errors":[],"node_count":2,"edge_count":1}',
        b'{"node_count":2,"edge_count":1}',
    ])
    def factory(**kwargs: object) -> str:
        path = Path(tempfile.mkdtemp(prefix="graphite-doctor-"))
        created.append(path)
        return str(path)
    def cleanup(path: Path) -> None:
        release.wait(2)
        shutil.rmtree(path)
    def run(*args: object, **kwargs: object) -> object:
        return probes.ProbeProcessResult(0, next(outputs), b"", 0.01)
    started = time.monotonic()
    check = probes.probe_core_pipeline(tmp_path, timeout_seconds=0.3, _runner=run, _temp_factory=factory, _temp_cleanup=cleanup)
    elapsed = time.monotonic() - started
    release.set()
    assert check.status == "blocked"
    assert check.details == {"error_type": "cleanup", "code": "cleanup_timeout"}
    assert elapsed < 1
    deadline = time.monotonic() + 1
    while created[0].exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not created[0].exists()


@pytest.mark.parametrize(
    ("validation", "stats"),
    [
        ({"ok": True, "error_count": 0, "errors": [], "node_count": 2}, {"node_count": 2, "edge_count": 1}),
        ({"ok": True, "error_count": 0, "errors": [], "node_count": 2, "edge_count": True}, {"node_count": 2, "edge_count": 1}),
        ({"ok": True, "error_count": 0, "errors": [], "node_count": 2, "edge_count": -1}, {"node_count": 2, "edge_count": 1}),
        ({"ok": True, "error_count": 0, "errors": [], "node_count": 2, "edge_count": 1}, {"node_count": 3, "edge_count": 1}),
        ({"ok": True, "error_count": 0, "errors": [], "node_count": 2, "edge_count": 1}, {"node_count": 2, "edge_count": 0}),
    ],
)
def test_core_probe_rejects_invalid_or_mismatched_counts(tmp_path: Path, validation: dict[str, object], stats: dict[str, object]) -> None:
    import graphite.doctor_probes as probes

    outputs = iter([b"", json.dumps(validation).encode(), json.dumps(stats).encode()])
    check = probes.probe_core_pipeline(tmp_path, _runner=lambda *a, **k: probes.ProbeProcessResult(0, next(outputs), b"", 0.01))
    assert check.status == "blocked"


def test_run_deep_probes_reports_core_mcp_and_typescript(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    core = DoctorCheck("deep_core", "Deterministic pipeline", "ready", "ok")
    mcp = DoctorCheck("deep_mcp", "MCP", "ready", "ok")
    typescript = DoctorCheck("deep_typescript", "TypeScript", "ready", "ok")
    monkeypatch.setattr(probes, "probe_core_pipeline", lambda root: core)
    monkeypatch.setattr(probes, "probe_mcp", lambda root: mcp)
    monkeypatch.setattr(probes, "probe_typescript", lambda root: typescript)
    assert probes.run_deep_probes(tmp_path, cfg=Config(), include_llm=True) == [core, mcp, typescript]


def _mcp_probe_output(*messages: dict[str, object]) -> bytes:
    pending: queue.Queue[dict[str, object]] = queue.Queue()
    for message in messages:
        pending.put(message)
    return b"".join(json.dumps(pending.get_nowait()).encode("utf-8") + b"\n" for _ in messages)


def test_mcp_deep_probe_initializes_and_lists_required_tools(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    captured: dict[str, object] = {}
    output = _mcp_probe_output(
        {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "graphite"}}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {"name": "graphite_query"},
                    {"name": "graphite_summary"},
                    {"name": "graphite_community"},
                    {"name": "graphite_refresh"},
                ]
            },
        },
    )

    def run(argv: list[str], **kwargs: object) -> object:
        captured.update(argv=argv, **kwargs)
        return probes.ProbeProcessResult(0, output, b"", 0.01)

    check = probes.probe_mcp(tmp_path, python_executable="PYTHON", timeout_seconds=2, _runner=run)

    assert check.status == "ready"
    assert check.details == {"server_name": "graphite", "tool_count": 4}
    assert captured["argv"] == ["PYTHON", "-B", "-m", "graphite.mcp"]
    assert captured["cwd"] == tmp_path
    assert captured["timeout_seconds"] == 2
    assert captured["max_output_bytes"] == 1024 * 1024
    requests = [json.loads(line) for line in captured["stdin"].decode("utf-8").splitlines()]
    assert requests[0]["method"] == "initialize"
    assert requests[0]["params"]["protocolVersion"] == "2024-11-05"
    assert requests[1]["method"] == "notifications/initialized"
    assert requests[2]["method"] == "tools/list"
    assert all(request.get("params", {}).get("name") != "graphite_refresh" for request in requests)


@pytest.mark.parametrize("failure_code", ["timeout", "output_limit", "nonzero", "io_failed"])
def test_mcp_deep_probe_maps_transport_failures_to_degraded(tmp_path: Path, failure_code: str) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_process import ProbeProcessError

    def fail(*args: object, **kwargs: object) -> object:
        raise ProbeProcessError(failure_code)

    started = time.monotonic()
    check = probes.probe_mcp(tmp_path, timeout_seconds=0.2, _runner=fail)
    assert time.monotonic() - started < 1
    assert check.status == "degraded"
    assert check.details == {"code": failure_code}


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"not-json\n",
        _mcp_probe_output({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "other"}}}),
    ],
)
def test_mcp_deep_probe_rejects_closed_or_malformed_responses(tmp_path: Path, output: bytes) -> None:
    import graphite.doctor_probes as probes

    check = probes.probe_mcp(
        tmp_path,
        _runner=lambda *a, **k: probes.ProbeProcessResult(0, output, b"", 0.01),
    )
    assert check.status == "degraded"
    assert check.details == {"code": "invalid_response"}


class _QueuedProbePipe:
    def __init__(self) -> None:
        self.items: queue.Queue[bytes | None] = queue.Queue()

    def read(self, size: int) -> bytes:
        del size
        item = self.items.get(timeout=2)
        return b"" if item is None else item

    def send(self, value: bytes) -> None:
        self.items.put(value)

    def close(self) -> None:
        self.items.put(None)


class _QueuedProbeInput:
    def __init__(self, process: "_QueuedProbeProcess", behavior: str) -> None:
        self.process = process
        self.behavior = behavior
        self.data = bytearray()
        self.flushed = False

    def write(self, data: bytes) -> int:
        self.data.extend(data)
        return len(data)

    def flush(self) -> None:
        if self.flushed:
            return
        self.flushed = True
        if self.behavior == "overflow":
            self.process.stdout.send(b"x" * (1024 * 1024 + 1))
        elif self.behavior == "closed":
            self.process.stdout.close()
            self.process.finish(0)
        elif self.behavior == "nonzero":
            self.process.stdout.close()
            self.process.finish(9)

    def close(self) -> None:
        pass


class _QueuedProbeProcess:
    def __init__(self, behavior: str, events: list[str]) -> None:
        self.pid = 12345
        self.returncode: int | None = None
        self.stdout = _QueuedProbePipe()
        self.stderr = _QueuedProbePipe()
        self.stdin = _QueuedProbeInput(self, behavior)
        self._done = threading.Event()
        self.events = events

    def finish(self, returncode: int) -> None:
        self.returncode = returncode
        self._done.set()
        self.stderr.close()

    def wait(self, timeout: float) -> int:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("fake-probe", timeout)
        assert self.returncode is not None
        return self.returncode

    def send_signal(self, signal_number: int) -> None:
        del signal_number
        self.events.append("terminate")

    def terminate(self) -> None:
        self.events.append("terminate")

    def kill(self) -> None:
        self.events.append("kill")
        self.finish(-9)
        self.stdout.close()


@pytest.mark.parametrize(
    ("behavior", "expected_code"),
    [("overflow", "output_limit"), ("timeout", "timeout")],
)
def test_mcp_deep_probe_real_transport_bounds_failures_and_escalates_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    behavior: str,
    expected_code: str,
) -> None:
    import graphite.doctor_probes as probes
    import graphite.probe_process as transport

    events: list[str] = []
    process = _QueuedProbeProcess(behavior, events)
    monkeypatch.setattr(transport, "_launch_process", lambda *a, **k: process)
    monkeypatch.setattr(
        transport,
        "_gracefully_signal_process_tree",
        lambda target: target.terminate() is None,
    )
    monkeypatch.setattr(
        transport,
        "_force_kill_process_tree",
        lambda target, deadline: target.kill(),
    )

    started = time.monotonic()
    check = probes.probe_mcp(tmp_path, timeout_seconds=0.35, _runner=transport.run_bounded_process)

    assert time.monotonic() - started < 1
    assert check.status == "degraded"
    assert check.details == {"code": expected_code}
    assert events.index("terminate") < events.index("kill")


def test_mcp_deep_probe_real_transport_handles_nonzero_without_hanging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import graphite.doctor_probes as probes
    import graphite.probe_process as transport

    process = _QueuedProbeProcess("nonzero", [])
    monkeypatch.setattr(transport, "_launch_process", lambda *a, **k: process)
    started = time.monotonic()
    check = probes.probe_mcp(tmp_path, timeout_seconds=0.35, _runner=transport.run_bounded_process)
    assert time.monotonic() - started < 1
    assert check.status == "degraded"
    assert check.details == {"code": "nonzero"}


def test_mcp_deep_probe_real_transport_handles_closed_stdout_without_hanging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import graphite.doctor_probes as probes
    import graphite.probe_process as transport

    process = _QueuedProbeProcess("closed", [])
    monkeypatch.setattr(transport, "_launch_process", lambda *a, **k: process)
    started = time.monotonic()
    check = probes.probe_mcp(tmp_path, timeout_seconds=0.35, _runner=transport.run_bounded_process)
    assert time.monotonic() - started < 1
    assert check.status == "degraded"
    assert check.details == {"code": "invalid_response"}


def test_typescript_deep_probe_is_optional_when_node_is_missing(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    check = probes.probe_typescript(tmp_path, _node_resolver=lambda root: None)
    assert check.status == "optional"
    assert check.details == {}


def test_typescript_deep_probe_gives_exact_activation_guidance_when_module_is_missing(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    check = probes.probe_typescript(
        tmp_path,
        _node_resolver=lambda root: Path("NODE"),
        _runner=lambda *a, **k: probes.ProbeProcessResult(0, b'{"missing_module":"typescript"}', b"", 0.01),
    )
    assert check.status == "optional"
    assert check.remediation == (
        "Validate package: node C:/Users/fbmac/atlas/Codex/.codex_state/user_home/scripts/validate-packages.cjs typescript",
        "Then add typescript with the target project's existing package manager.",
    )


def test_typescript_deep_probe_runtime_exit_one_is_degraded(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    check = probes.probe_typescript(
        tmp_path,
        _node_resolver=lambda root: Path("NODE"),
        _runner=lambda *a, **k: probes.ProbeProcessResult(1, b"", b"", 0.01),
    )
    assert check.status == "degraded"
    assert check.details == {"code": "invalid_result"}


def test_typescript_deep_probe_transpiles_synthetic_constant_and_reports_version_only(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    captured: dict[str, object] = {}
    def run(argv: list[str], **kwargs: object) -> object:
        captured.update(argv=argv, **kwargs)
        return probes.ProbeProcessResult(0, b'{"version":"5.8.2"}', b"", 0.01)

    check = probes.probe_typescript(
        tmp_path,
        timeout_seconds=3,
        _node_resolver=lambda root: Path("NODE"),
        _runner=run,
    )
    assert check.status == "ready"
    assert check.details == {"version": "5.8.2"}
    assert captured["argv"][0:2] == ["NODE", "-e"]
    assert captured["argv"][2] == (
        "try{require.resolve('typescript')}catch(error){"
        "if(error&&error.code==='MODULE_NOT_FOUND'){"
        "process.stdout.write(JSON.stringify({missing_module:'typescript'}));process.exit(0)}"
        "process.exit(4)} "
        "const ts=require('typescript'); const source='const value: number = 1'; "
        "const result=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022}}); "
        "if(!result.outputText.includes('const value = 1')) process.exit(3); "
        "process.stdout.write(JSON.stringify({version:ts.version}));"
    )
    assert captured["cwd"] == tmp_path
    assert captured["timeout_seconds"] == 3


@pytest.mark.parametrize("result", [b"not-json", b'{}', b'{"version":1}'])
def test_typescript_deep_probe_invalid_result_is_degraded(tmp_path: Path, result: bytes) -> None:
    import graphite.doctor_probes as probes

    check = probes.probe_typescript(
        tmp_path,
        _node_resolver=lambda root: Path("NODE"),
        _runner=lambda *a, **k: probes.ProbeProcessResult(0, result, b"", 0.01),
    )
    assert check.status == "degraded"


def test_typescript_deep_probe_timeout_is_degraded(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_process import ProbeProcessError

    def timeout(*args: object, **kwargs: object) -> object:
        raise ProbeProcessError("timeout")

    check = probes.probe_typescript(
        tmp_path,
        _node_resolver=lambda root: Path("NODE"),
        _runner=timeout,
    )
    assert check.status == "degraded"
