"""Typed, bounded system-readiness checks."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite.config import Config
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


def _bundle() -> dict:
    return {"nodes": [], "edges": [], "clusters": [], "analysis": {}, "metadata": {"node_count": 0, "edge_count": 0, "community_count": 0}}


def test_graph_states_size_limit_and_safe_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = Config(output_dir=Path("out"))
    assert check_graph(tmp_path, cfg).status == "degraded"
    graph = tmp_path / "out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text(json.dumps(_bundle()), encoding="utf-8")
    monkeypatch.setattr("graphite.doctor.check_graph_freshness", lambda root, cfg: {"stale": False})
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


def test_mcp_typescript_and_llm_never_leak_raw_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("graphite.doctor.importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr("graphite.doctor.shutil.which", lambda name: None)
    assert check_mcp().status == "optional"

    monkeypatch.setattr("graphite.doctor.shutil.which", lambda name: "node")
    captured = {}
    def node_probe(root, script, *, timeout_seconds):
        captured["timeout"] = timeout_seconds
        return 0, b'{"ok":true,"version":"5.0"}', False
    monkeypatch.setattr("graphite.doctor._run_node_probe", node_probe)
    ts = check_typescript(tmp_path, timeout_seconds=1.25)
    assert ts.status == "ready"
    assert captured == {"timeout": 1.25}
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
    result = check_daemon(tmp_path)
    assert result.status == "optional"
    assert result.details == {"status_found": False, "error_count": 1, "warning_count": 0, "process_checked": False, "startup_checked": False}
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
