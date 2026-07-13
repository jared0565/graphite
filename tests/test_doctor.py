"""Typed, bounded system-readiness checks."""
from __future__ import annotations

import json
import math
import os
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
    monkeypatch.setattr(transport.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(transport, "_terminate_process_tree", lambda process, deadline: release.set())
    with pytest.raises(transport.ProbeProcessError) as exc_info:
        transport.run_bounded_process(["python"], cwd=tmp_path, stdin=b"small", timeout_seconds=1)
    assert exc_info.value.code == "input_failed"
    assert "RAW" not in str(exc_info.value)


def test_probe_transport_accepts_small_input(tmp_path: Path) -> None:
    from graphite.probe_process import run_bounded_process

    result = run_bounded_process(
        [sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        cwd=tmp_path,
        stdin="small input",
        timeout_seconds=5,
    )
    assert result.stdout == b"small input"


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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
    with pytest.raises(ProbeProcessError) as exc_info:
        run_bounded_process([sys.executable, "-c", parent], cwd=tmp_path, timeout_seconds=0.5)
    assert exc_info.value.code == "timeout"
    pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        deadline = time.monotonic() + 1
        while _pid_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _pid_exists(pid)
    finally:
        if _pid_exists(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


def test_deep_probe_taskkill_path_does_not_trust_ambient_systemroot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import graphite.probe_process as probes

    poisoned = tmp_path / "System32" / "taskkill.exe"
    poisoned.parent.mkdir()
    poisoned.write_bytes(b"not trusted")
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setenv("WINDIR", str(tmp_path))
    assert probes._trusted_taskkill() != poisoned.resolve()


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


def test_run_deep_probes_currently_reports_only_core(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    expected = DoctorCheck("deep_core", "Deterministic pipeline", "ready", "ok")
    monkeypatch.setattr(probes, "probe_core_pipeline", lambda root: expected)
    assert probes.run_deep_probes(tmp_path, cfg=Config(), include_llm=True) == [expected]
