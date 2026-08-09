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
from types import SimpleNamespace

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


#: A pid no kernel can allocate, for fake processes handed to real cleanup.
#:
#: `_terminate_process_tree`'s POSIX branch calls `os.killpg(process.pid,
#: SIGTERM)` and then `SIGKILL` for real -- there is no seam between the fake
#: and the syscall. A fake pid that happens to name a live process group
#: therefore aims both signals at an unrelated process. These fakes used
#: `pid = 123`, which on the Linux box this was found on was root's `snapfuse`;
#: only EPERM stopped the signals, and that EPERM is also what made four tests
#: report `cleanup_failed`. A suite running as root -- ordinary in a container
#: -- would have delivered them.
#:
#: `pid_t` is a signed 32-bit int, so INT32_MAX is representable but above every
#: platform's maximum (Linux's `pid_max` caps at 2^22), which makes `killpg`
#: return ESRCH rather than EINVAL. Verified in `test_the_fake_process_pid_can_
#: name_no_real_process_group`.
UNCLAIMABLE_PID = 2**31 - 1


def _fake_process_group(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: type[OSError] | None = ProcessLookupError,
) -> list[tuple[int, int]]:
    """Give a fake process a fake process group, and refuse to signal any other.

    Every other cleanup step is a method the fake implements -- `terminate_tree`,
    `close_handles`, `kill`, `reap`. POSIX tree termination is the exception: it
    reaches the `os.killpg` syscall directly, so a fake's pid is a real pgid and
    the fake has no way to intercept it. `pytest.fail` on any other pgid makes
    that structurally impossible rather than merely unlikely.

    `error` is what the fake group's existence probe and signals raise:
    `ProcessLookupError` for "this group is gone" (the normal case, ESRCH), or
    `PermissionError` for "this group exists and we may not signal it" -- the
    POSIX spelling of a tree that could not be terminated.

    Patching `os.killpg` is process-global, deliberately: `probe_process` calls
    it through the stdlib module, so a module-scoped patch would not reach it.
    Bounded either way -- the graceful loop it feeds is capped by a real clock
    (unlike the frozen one behind graphite#45), so no outcome here can hang.
    """
    signals: list[tuple[int, int]] = []
    if os.name == "nt":  # no process groups; cleanup consults `terminate_tree`
        return signals

    def killpg(process_group_id: int, sig: int) -> None:
        if process_group_id != UNCLAIMABLE_PID:
            pytest.fail(
                f"test aimed signal {sig} at a real process group: pgid={process_group_id}"
            )
        signals.append((process_group_id, sig))
        if error is not None:
            raise error(f"fake process group {process_group_id}")

    # `raising=False` so a test that patches `os.name` to "posix" can reach the
    # POSIX branch from a Windows dev box, where `os.killpg` does not exist.
    # POSIX semantics that differ BETWEEN Unixes (darwin's EPERM, graphite#46)
    # would otherwise be verifiable only on CI, which is where they hid.
    monkeypatch.setattr(os, "killpg", killpg, raising=False)
    return signals


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
    monkeypatch.delitem(sys.modules, "graphite.doctor_probes", raising=False)
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


def _iter_strings(value: object):
    """Yield every string leaf in a JSON-shaped structure.

    Deliberately never round-trips through json.dumps: it escapes backslashes,
    so a substring check against a raw Windows path (single backslashes) can
    never match its own JSON-escaped form (doubled backslashes) even when the
    path is genuinely present -- making `needle not in json.dumps(x)` a
    silently vacuous leak guard on this platform. Walking the live structure
    and comparing raw strings has no such gap.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


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
    leaked = list(_iter_strings(result.to_dict()))
    assert not any("RAW" in s for s in leaked)
    assert not any(str(tmp_path) in s for s in leaked)


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

    sentinel = "CREDENTIAL_SENTINEL"
    llm = check_llm_config(Config(llm_mode="none", llm_provider=" Open_AI ", llm_api_key=sentinel))
    encoded = json.dumps(llm.to_dict()) + format_doctor_text(build_report(tmp_path, [llm], deep=False, llm_included=False))
    assert sentinel not in encoded
    assert llm.details == {"mode": "none", "provider": "custom/unknown", "credential_present": True}
    assert "unused" in llm.summary.lower()


def test_enabled_valid_llm_config_is_optional_until_explicitly_probed() -> None:
    check = check_llm_config(
        Config(
            llm_mode="cloud",
            llm_provider="openai-compatible",
            llm_base_url="https://provider.invalid/v1",
            llm_api_key="session-key",
        )
    )

    assert check.status == "optional"
    assert check.details["provider"] == "openai-compatible"


def test_invalid_required_llm_config_remains_degraded() -> None:
    check = check_llm_config(Config(llm_mode="cloud", llm_provider="openai-compatible"))

    assert check.status == "degraded"
    assert "required" in check.summary.lower()


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


def test_run_doctor_deep_llm_ready_supersedes_fast_unverified_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_llm = DoctorCheck("llm", "LLM", "optional", "not yet verified")
    monkeypatch.setattr(
        "graphite.doctor._fast_checks",
        lambda root, cfg, daemon_base: [
            DoctorCheck("python", "Python", "ready", "ok"),
            fast_llm,
        ],
    )

    report = run_doctor(
        tmp_path,
        Config(llm_mode="cloud"),
        deep=True,
        include_llm=True,
        deep_runner=lambda root, *, cfg, include_llm: [
            DoctorCheck("deep_llm", "LLM", "ready", "synthetic connectivity probe succeeded")
        ],
    )

    assert report["status"] == "ready"
    assert [check["code"] for check in report["checks"]] == ["deep_llm", "python"]


def test_run_doctor_disabled_llm_retains_ambient_credential_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graphite.doctor_probes as probes

    sentinel = "ApiKey_SECRET"
    cfg = Config(llm_mode="none", llm_api_key=sentinel)
    fast_llm = check_llm_config(cfg)
    monkeypatch.setattr(
        "graphite.doctor._fast_checks",
        lambda root, configured, daemon_base: [fast_llm],
    )
    provider_calls = 0

    def unexpected_runner(*args: object, **kwargs: object) -> object:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("disabled mode started a provider worker")

    report = run_doctor(
        tmp_path,
        cfg,
        deep=True,
        include_llm=True,
        deep_runner=lambda root, *, cfg, include_llm: [
            probes.probe_llm(cfg, _runner=unexpected_runner)
        ],
    )

    by_code = {check["code"]: check for check in report["checks"]}
    assert set(by_code) == {"llm", "deep_llm"}
    assert by_code["llm"]["details"]["credential_present"] is True
    assert "rotate or remove" in by_code["llm"]["summary"].lower()
    assert provider_calls == 0
    assert sentinel not in json.dumps(report)


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
    assert not any(str(root) in s for s in _iter_strings(result.to_dict()))


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


def test_daemon_treats_permission_limited_process_observation_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    monkeypatch.setattr(
        "graphite.doctor.read_daemon_status",
        lambda *a, **k: {
            "projects": [{"root": str(root), "build_count": 1, "last_error": None}]
        },
    )
    monkeypatch.setattr(
        "graphite.doctor.evaluate_daemon_health",
        lambda *a, **k: {
            "ok": True,
            "status": "warning",
            "daemon_status": "ok",
            "errors": [],
            "warnings": [
                {
                    "code": "daemon_process_check_unavailable",
                    "message": "RAW C:/secret",
                }
            ],
            "process": {
                "checked": True,
                "running": False,
                "error": "RAW C:/secret",
            },
            "startup": {"checked": True},
            "projects": {"failing": [], "pending": [], "not_built_recently": []},
        },
    )

    result = check_daemon(root, tmp_path)

    assert result.status == "ready"
    assert result.details["process_running"] is False
    assert result.details["process_observation_available"] is False
    assert result.details["global_warning_count"] == 0
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


def test_bounded_process_accepts_an_exact_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from graphite.probe_process import run_bounded_process

    monkeypatch.setenv("SECRET_TOKEN", "must-not-inherit")
    script = "import os;print(os.environ.get('GRAPHITE_SENTINEL'));print(os.environ.get('SECRET_TOKEN'))"

    result = run_bounded_process(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        environment={"GRAPHITE_SENTINEL": "safe"},
        timeout_seconds=5,
    )

    assert result.stdout.decode().splitlines() == ["safe", "None"]


def test_bounded_process_treats_an_empty_environment_as_exact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from graphite.probe_process import run_bounded_process

    monkeypatch.setenv("GRAPHITE_EMPTY_ENV_SENTINEL", "must-not-inherit")
    names = ("GRAPHITE_EMPTY_ENV_SENTINEL", "PYTHONPATH", "PYTHONIOENCODING", "PYTHONUTF8")
    script = f"import json,os;print(json.dumps({{name:os.environ.get(name) for name in {names!r}}}))"

    result = run_bounded_process(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        environment={},
        timeout_seconds=5,
    )

    assert json.loads(result.stdout) == {name: None for name in names}


def test_bounded_process_copies_an_exact_environment_before_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import graphite.probe_process as transport

    source = {"GRAPHITE_SENTINEL": "safe"}
    observed: list[object] = []

    def fail_launch(*args: object, **kwargs: object) -> None:
        observed.append(kwargs["environment"])
        raise OSError("injected launch failure")

    monkeypatch.setattr(transport, "_launch_process", fail_launch)

    with pytest.raises(transport.ProbeProcessError, match="launch_failed"):
        transport.run_bounded_process(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            environment=source,
            timeout_seconds=5,
        )

    assert observed == [source]
    assert observed[0] is not source


@pytest.mark.parametrize(
    "environment",
    [
        {"": "value"},
        {"BAD=KEY": "value"},
        {"BAD\0KEY": "value"},
        {"GOOD": "bad\0value"},
        {1: "value"},
        {"GOOD": 1},
    ],
)
def test_bounded_process_rejects_invalid_exact_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environment: dict[object, object],
) -> None:
    import graphite.probe_process as transport

    launched = False

    def fail_if_launched(*args: object, **kwargs: object) -> None:
        nonlocal launched
        launched = True
        raise AssertionError("must not launch")

    monkeypatch.setattr(transport, "_launch_process", fail_if_launched)

    with pytest.raises(transport.ProbeProcessError, match="invalid_environment"):
        transport.run_bounded_process(
            [sys.executable, "-c", "raise AssertionError('must not launch')"],
            cwd=tmp_path,
            environment=environment,  # type: ignore[arg-type]
            timeout_seconds=5,
        )

    assert launched is False


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


def test_deep_bounded_runner_tolerates_child_exiting_before_reading_stdin(tmp_path: Path) -> None:
    """A child that exits without consuming stdin is not a transport failure.

    `_MCP_BOOTSTRAP` validates its argv bindings and raises `SystemExit(70)`
    *before* its first `sys.stdin.buffer.readline()`, so a rejecting bootstrap
    legitimately never reads the input the probe is still writing. Losing that
    race used to surface as `input_failed`, masking the child's real exit
    status -- the intermittent `probe input failed` in CI (issue #29). The
    child's return code is the verdict; bytes it chose not to read are not
    evidence of anything.

    The payload must exceed the pipe buffer so the write is still in flight
    when the child exits; a small payload lands in the buffer and the race
    never happens.
    """
    from graphite.probe_process import run_bounded_process

    result = run_bounded_process(
        [sys.executable, "-c", "raise SystemExit(70)"],
        cwd=tmp_path,
        stdin=b"x" * (1024 * 1024),
        timeout_seconds=15,
        check=False,
    )

    assert result.returncode == 70
    # Tolerating the broken pipe must not erase it: the child saw a short
    # stream, and a later failing probe needs to be able to say so.
    assert result.input_complete is False
    assert result.input_bytes == 1024 * 1024


def test_deep_bounded_runner_reports_input_delivered_in_full(tmp_path: Path) -> None:
    """A child that consumes all of stdin is reported as a complete delivery.

    The negative case alone would pass against a field hardcoded to False, so
    pin the positive one too.
    """
    from graphite.probe_process import run_bounded_process

    payload = b"x" * 4096
    result = run_bounded_process(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        cwd=tmp_path,
        stdin=payload,
        timeout_seconds=15,
    )

    assert result.returncode == 0
    assert result.input_complete is True
    assert result.input_bytes == len(payload)


def test_deep_bounded_runner_defers_stdin_close_until_the_transcript_is_ready(
    tmp_path: Path,
) -> None:
    """stdin must stay open until the caller says it has what it needs.

    Closing it the instant the payload is written makes EOF arrive while the
    child is still working. An MCP server treats that EOF as end-of-session and
    tears the write side down, so a reply already in flight is never emitted --
    `initialize` answered, `tools/list` dropped, exit 0 (graphite issue #29).

    The child reports how long after startup it observed EOF. Deferred, that
    cannot be earlier than the marker the predicate waits for.
    """
    from graphite.probe_process import run_bounded_process

    child = (
        "import sys, time\n"
        "start = time.monotonic()\n"
        "sys.stdout.write('first\\n'); sys.stdout.flush()\n"
        "time.sleep(0.8)\n"
        "sys.stdout.write('go\\n'); sys.stdout.flush()\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.write('eof_after=%.2f\\n' % (time.monotonic() - start))\n"
        "sys.stdout.flush()\n"
    )

    result = run_bounded_process(
        [sys.executable, "-c", child],
        cwd=tmp_path,
        stdin=b"payload\n",
        timeout_seconds=30,
        stdin_close_when=lambda out: b"go\n" in out,
    )

    assert result.returncode == 0, result.stderr
    text = result.stdout.decode()
    observed = float(text.split("eof_after=")[1].split("\n")[0])
    # Closed immediately this reads ~0.0; deferred it cannot precede the marker.
    assert observed >= 0.7, text


def test_deep_bounded_runner_force_closes_stdin_when_the_predicate_never_fires(
    tmp_path: Path,
) -> None:
    """A predicate that never passes must not strand the child on an open pipe.

    Deferring the close trades one failure for a worse one if it can defer
    forever: a child blocked reading stdin never exits, and a probe that would
    have failed fast and explained itself becomes an opaque `timeout`. The close
    is therefore bounded by the run's own budget, not by the predicate.
    """
    from graphite.probe_process import run_bounded_process

    child = "import sys\nsys.stdin.buffer.read()\nsys.stdout.write('done\\n')\nsys.stdout.flush()\n"

    result = run_bounded_process(
        [sys.executable, "-c", child],
        cwd=tmp_path,
        stdin=b"payload\n",
        timeout_seconds=8,
        stdin_close_when=lambda out: b"never" in out,
    )

    assert result.returncode == 0
    assert b"done" in result.stdout


def test_deep_bounded_runner_closes_stdin_immediately_without_a_predicate(
    tmp_path: Path,
) -> None:
    """The default is unchanged: no predicate means close as soon as it is written.

    Pins the opposite of the deferral test, so a change that defers
    unconditionally -- which would slow every other probe by its whole budget --
    fails here rather than in production.
    """
    from graphite.probe_process import run_bounded_process

    child = (
        "import sys, time\n"
        "start = time.monotonic()\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.write('eof_after=%.2f\\n' % (time.monotonic() - start))\n"
        "sys.stdout.flush()\n"
    )

    result = run_bounded_process(
        [sys.executable, "-c", child],
        cwd=tmp_path,
        stdin=b"payload\n",
        timeout_seconds=30,
    )

    assert result.returncode == 0
    observed = float(result.stdout.decode().split("eof_after=")[1].split("\n")[0])
    assert observed < 0.5, result.stdout


def test_mcp_transcript_predicate_waits_for_both_response_ids() -> None:
    """The probe must hold stdin open until BOTH replies are on stdout.

    This is the condition that makes the deferral fix #29 rather than merely
    delay it: `initialize` alone is exactly the transcript every failure
    captured, so treating it as complete would close stdin at precisely the
    moment the race is lost.
    """
    import graphite.doctor_probes as probes

    initialize_only = b'{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"graphite"}}}\n'
    both = initialize_only + b'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n'

    assert probes._mcp_transcript_complete(b"") is False
    assert probes._mcp_transcript_complete(initialize_only) is False
    # A half-written second line must not count as an answer either.
    assert probes._mcp_transcript_complete(initialize_only + b'{"jsonrpc":"2.0","id":2,"resu') is False
    assert probes._mcp_transcript_complete(both) is True


def test_probe_diagnostics_record_input_side_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A short response transcript is ambiguous without the input-side facts.

    `input_complete=False` means the child never received the whole request
    stream, so a missing response is explained. `True` means it got everything
    and still answered less -- a different bug in a different place. Issue #29
    stalled precisely because the captured diagnostic could not tell these
    apart.
    """
    import graphite.doctor_probes as probes
    from graphite.probe_process import ProbeProcessResult

    truncated = ProbeProcessResult(
        0, b'{"jsonrpc":"2.0","id":1}\n', b"", 0.01, input_bytes=4096, input_complete=False
    )
    probes._record_probe_diagnostics(tmp_path, "invalid_response", truncated)
    err = capsys.readouterr().err
    assert "input_bytes=4096" in err
    assert "input_complete=False" in err

    probes._record_probe_diagnostics(tmp_path, "invalid_response", ProbeProcessResult(0, b"", b"", 0.01))
    assert "input_complete=True" in capsys.readouterr().err


def test_probe_diagnostics_excerpt_stderr_from_its_tail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The child's stderr must be excerpted from the END, not the beginning.

    stdout is excerpted head-first because the first responses are the ones
    under test. stderr is the opposite: it carries the child's own log, and the
    interesting entries are the LAST ones it managed to emit before it stopped.
    Head-truncating it drops exactly the evidence the diagnostic exists to
    capture -- a probe would report the startup chatter and cut off at the
    failure (graphite issue #29).
    """
    import graphite.doctor_probes as probes
    from graphite.probe_process import ProbeProcessResult

    noise = b"x" * 4000
    stderr = noise + b"server:Response sent"
    probes._record_probe_diagnostics(
        tmp_path, "invalid_response", ProbeProcessResult(0, b"", stderr, 0.01)
    )
    err = capsys.readouterr().err

    assert "server:Response sent" in err
    # And it must say what it dropped, so a truncated tail is never mistaken
    # for the whole of the child's stderr.
    assert "more chars" in err


def test_probe_diagnostics_detail_stays_within_the_ledger_cap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both excerpts together must fit the ledger's 2048-char record cap.

    stdout and stderr are excerpted independently, so raising either cap can
    silently push the joined detail past the cap and get it truncated by the
    ledger instead -- losing the tail this change exists to preserve.
    """
    import graphite.doctor_probes as probes
    from graphite.probe_process import ProbeProcessResult

    huge = ProbeProcessResult(0, b"o" * 100_000, b"e" * 100_000, 0.01, input_bytes=1)
    probes._record_probe_diagnostics(tmp_path, "invalid_response", huge)
    line = capsys.readouterr().err.strip()

    assert len(line) <= 2048


def test_mcp_child_log_setup_routes_mcp_logs_to_stderr() -> None:
    """The probe child must surface the MCP library's own log to stderr.

    Every #29 mechanism so far was inferred from adjacent evidence because the
    child never said anything: `returncode=0`, empty stderr, and a transcript
    holding only the `initialize` reply. The library logs the facts that
    discriminate the remaining hypotheses ("Dispatching request of type ...",
    "Response sent", "Request N cancelled"), and nothing was listening.
    """
    import graphite.doctor_probes as probes

    # Run it the way the child does -- an isolated `-I -S` interpreter -- rather
    # than exec'ing it in-process. That also proves it works without site
    # packages and without mutating this process's logging config.
    emitted = probes._MCP_CHILD_LOG_SETUP + (
        'import logging\nlogging.getLogger("mcp.server.lowlevel.server").debug("Response sent")\n'
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", emitted],
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    # Child loggers must reach it, and DEBUG must not be filtered out -- the two
    # most useful records ("Response sent", "Dispatching request") are DEBUG.
    # The bare module name is kept as a prefix so the line stays attributable.
    assert "server:Response sent" in result.stderr.decode("utf-8", "replace")


def test_mcp_child_log_setup_bounds_its_own_stderr_volume() -> None:
    """The child's log must be hard-bounded, or the diagnostic breaks the probe.

    `_parse_mcp_responses` rejects the transcript when stdout plus stderr
    exceeds the 32KB output limit, and `run_bounded_process` fails the whole
    probe with `output_limit` past the same bound. An unbounded debug log would
    therefore manufacture the very failure it was added to explain.
    """
    import graphite.doctor_probes as probes

    flood = probes._MCP_CHILD_LOG_SETUP + (
        "import logging\n"
        'log = logging.getLogger("mcp.server")\n'
        "for index in range(5000):\n"
        '    log.debug("chatter %s %s", index, "y" * 500)\n'
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", flood],
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    # Normalise the platform newline the child's text stream adds, so the bound
    # is compared against what the handler actually accounted for.
    written = result.stderr.decode("utf-8", "replace").replace("\r\n", "\n")
    assert 0 < len(written) <= probes._MCP_CHILD_LOG_BUDGET


def test_mcp_bootstrap_installs_the_child_log_setup() -> None:
    """The setup has to be wired into the bootstrap, not merely defined.

    Without this the two tests above would keep passing against a constant the
    child never executes.
    """
    import graphite.doctor_probes as probes

    assert probes._MCP_CHILD_LOG_SETUP in probes._MCP_BOOTSTRAP
    # Before the handoff, or the server starts with nothing listening.
    assert probes._MCP_BOOTSTRAP.index(probes._MCP_CHILD_LOG_SETUP) < probes._MCP_BOOTSTRAP.index(
        'runpy.run_module("graphite.mcp"'
    )


def test_probe_transport_rechecks_late_writer_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import graphite.probe_process as transport

    release = threading.Event()
    class InputPipe:
        def write(self, data: bytes) -> int:
            release.wait(1)
            # A genuine transport fault, deliberately NOT BrokenPipeError: a
            # broken pipe means the child stopped reading, which is legitimate
            # and is now tolerated (see
            # test_deep_bounded_runner_tolerates_child_exiting_before_reading_stdin).
            # What this test pins is the *late* recheck -- the writer fails
            # after cleanup has started -- and that the raw text never leaks.
            raise OSError("RAW late input")
        def flush(self) -> None: pass
        def close(self) -> None: pass
    class OutputPipe:
        def read(self, size: int) -> bytes: return b""
        def close(self) -> None: pass
    class Process:
        pid = UNCLAIMABLE_PID
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
    # No number to report when the OSError carries none, and "unknown" must not
    # be rendered as a code -- `os=None` would read like a real errno 0.
    assert exc_info.value.os_error is None
    assert "os=" not in str(exc_info.value)


def test_probe_transport_reports_the_numeric_code_of_a_writer_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`input_failed` must carry the OS error number that caused it.

    The code alone cannot distinguish a genuine pipe fault from our own
    `_cancel_synchronous_io` aborting the writer's in-flight write during
    cleanup -- and those need opposite handling. graphite#41 is stuck at exactly
    that fork, and #29 lost four rounds to the same blindness: a classified
    error that discards the one fact which discriminates.

    A number is safe to surface where a message is not: it carries no path, no
    argv and no environment.
    """
    import graphite.probe_process as transport

    release = threading.Event()

    class InputPipe:
        def write(self, data: bytes) -> int:
            release.wait(1)
            # Deliberately NOT EINVAL: that number is now tolerated as Windows'
            # spelling of a broken pipe (see
            # test_probe_transport_tolerates_einval_as_a_broken_pipe). This
            # test is about the number travelling, not about which number.
            raise OSError(13, "RAW strerror with a path in it")

        def flush(self) -> None: pass
        def close(self) -> None: pass

    class OutputPipe:
        def read(self, size: int) -> bytes: return b""
        def close(self) -> None: pass

    class Process:
        pid = UNCLAIMABLE_PID
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
    assert exc_info.value.os_error == 13
    assert "os=13" in str(exc_info.value)
    # The number travels; the strerror never does.
    assert "RAW" not in str(exc_info.value)


@pytest.mark.skipif(os.name == "nt", reason="process groups and killpg are POSIX")
def test_the_fake_process_pid_can_name_no_real_process_group() -> None:
    """The transport fakes reach `os.killpg` with no seam in between.

    Two independent conditions, because either alone can pass while the pid is
    still dangerous: the constant must exceed what the kernel can allocate --
    an absence probe alone would pass on any machine that merely happens not to
    be running that pid right now -- and `killpg` must actually answer ESRCH
    rather than EINVAL, which is what makes cleanup report success instead of
    `cleanup_failed`.
    """
    pid_max = Path("/proc/sys/kernel/pid_max")
    if pid_max.exists():
        assert UNCLAIMABLE_PID > int(pid_max.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.killpg(UNCLAIMABLE_PID, 0)


def test_probe_transport_tolerates_einval_as_a_broken_pipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Windows spells "the reader is gone" as EINVAL as well as BrokenPipeError.

    Measured, not assumed: 3 of 10 CI runs failed with `probe input failed
    (os=22)` -- EINVAL -- across two different tests, one underlying transport
    bug (graphite#41). Locally the same write raises `BrokenPipeError` 40/40,
    which is why this never reproduced off CI.

    A child that stopped reading is legitimate and already tolerated; the errno
    it arrives under must not change the verdict. An OSError carrying no errno
    is still a genuine fault -- pinned by
    test_probe_transport_rechecks_late_writer_failure -- so this widens the
    tolerance by exactly one number rather than swallowing the category.
    """
    import graphite.probe_process as transport

    class InputPipe:
        def write(self, data: bytes) -> int:
            raise OSError(22, "Invalid argument")

        def flush(self) -> None: pass
        def close(self) -> None: pass

    class OutputPipe:
        def read(self, size: int) -> bytes: return b""
        def close(self) -> None: pass

    class Process:
        pid = UNCLAIMABLE_PID
        returncode = 0
        stdin = InputPipe()
        stdout = OutputPipe()
        stderr = OutputPipe()
        def wait(self, timeout: float) -> int: return 0
        def kill(self) -> None: self.returncode = -9

    monkeypatch.setattr(transport, "_launch_process", lambda *a, **k: Process())
    _fake_process_group(monkeypatch)

    result = transport.run_bounded_process(
        ["python"], cwd=tmp_path, stdin=b"small", timeout_seconds=5
    )

    assert result.returncode == 0
    # Tolerated, but never erased: the child saw a short stream and a later
    # failing probe has to be able to say so.
    assert result.input_complete is False


def test_probe_transport_cleans_process_tree_after_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import graphite.probe_process as transport

    class Pipe:
        def read(self, size: int) -> bytes: return b""
        def close(self) -> None: pass
    class Process:
        pid = UNCLAIMABLE_PID
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
    assert cleaned == [UNCLAIMABLE_PID]


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
        pid = UNCLAIMABLE_PID
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
    signals = _fake_process_group(monkeypatch)

    with pytest.raises(transport.ProbeProcessError) as exc_info:
        transport.run_bounded_process(["python"], cwd=tmp_path, timeout_seconds=1)

    assert exc_info.value.code == "io_failed"
    # "The tree was terminated" is spelled differently per platform, and only
    # Windows reaches `terminate_tree` -- `_force_kill_process_tree` consults
    # that method inside an `os.name == "nt"` branch. Asserting it unconditionally
    # pinned a mechanism POSIX never uses.
    if os.name == "nt":
        assert process.terminated
    else:
        assert signal.SIGKILL in [sent for _pgid, sent in signals]
    assert process.handles_closed
    assert all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))


def _exited_leader(returncode: int | None = 0) -> SimpleNamespace:
    """The minimum `_terminate_process_tree` reads: a pid, an exit status, a kill."""
    return SimpleNamespace(pid=UNCLAIMABLE_PID, returncode=returncode, kill=lambda: False)


@pytest.mark.parametrize(
    ("platform", "returncode", "contained"),
    [
        # MEASURED on macos-latest 3.12.10, four process states, against
        # ubuntu-latest as the control. darwin's `killpg` returns EPERM for a
        # group whose only remaining member is an unreaped zombie; Linux returns
        # SUCCESS for the identical state. A live descendant in that same group
        # makes darwin return success too, which is what licenses reading EPERM
        # as "nothing left to signal" rather than "not allowed to signal".
        pytest.param("darwin", 0, True, id="darwin-zombie-only-group-is-contained"),
        # The leader still running is a different claim entirely: nothing has
        # exited, so EPERM cannot mean "only zombies left" and stays a failure.
        pytest.param("darwin", None, False, id="darwin-live-leader-eperm-still-fails"),
        # Linux never produces EPERM here, so this reading must not reach it --
        # ubuntu is green at 2843 and is the control this change cannot perturb.
        pytest.param("linux", 0, False, id="linux-eperm-still-fails"),
    ],
)
def test_posix_tree_termination_reads_eperm_the_way_the_platform_means_it(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    returncode: int | None,
    contained: bool,
) -> None:
    """EPERM means opposite things on darwin and Linux, and cleanup must not guess.

    Every `run_bounded_process` call on macOS reported `cleanup_failed` for a
    run that had in fact succeeded, because the group signal that follows a
    clean exit lands on a group holding nothing but this module's deliberately
    unreaped zombie leader -- and darwin spells that EPERM (graphite#46).

    EPERM is genuinely ambiguous on darwin: "forbidden" and "nothing signalable"
    share the errno. This reads it as the latter, and the justification is that
    the transport CREATES the group itself, via `setsid()`, from its own uid and
    a sanitized environment -- so a member it may not signal is not reachable.
    Without that, the change is indistinguishable from swallowing an error.
    """
    import graphite.probe_process as transport

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", platform)
    # Windows has neither name, and the branch under test is POSIX-only. Faking
    # them is what lets a Unix-vs-Unix difference be pinned from any dev box.
    monkeypatch.setattr(signal, "SIGKILL", getattr(signal, "SIGKILL", signal.SIGTERM), raising=False)
    _fake_process_group(monkeypatch, error=PermissionError)

    result = transport._terminate_process_tree(_exited_leader(returncode), time.monotonic() + 1.0)

    assert result is contained


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
        pid = UNCLAIMABLE_PID
        returncode = 0
        stdin = None
        stdout, stderr = Pipe(), Pipe()
        def wait(self, timeout: float) -> int: return 0
        def kill(self) -> bool: return False
        def terminate_tree(self) -> bool: return failure != "terminate"
        def close_handles(self) -> bool: return failure != "close"
    monkeypatch.setattr(transport, "_launch_process", lambda *args, **kwargs: Process())
    # A tree that cannot be terminated: Windows hears it from `terminate_tree`,
    # POSIX from the errno of the group signal.
    #
    # This arm used EPERM, on the reasoning that "a group we may not signal" is
    # the real case. Measurement retired that: on darwin EPERM is ALSO how the
    # kernel reports a group holding nothing but this module's unreaped zombie
    # leader -- the ordinary outcome of every successful probe -- so EPERM can
    # no longer stand for "untermintable" without asserting the opposite of
    # `test_posix_tree_termination_reads_eperm_the_way_the_platform_means_it`.
    #
    # A bare OSError carries no errno and so keeps its old meaning everywhere:
    # some other transport fault, failure on every platform. Reparameterized
    # rather than skipped on darwin, because "cleanup failure never returns
    # success" is most worth testing on the platform where cleanup is fragile.
    _fake_process_group(
        monkeypatch,
        error=OSError if failure == "terminate" else ProcessLookupError,
    )

    with pytest.raises(transport.ProbeProcessError) as exc_info:
        transport.run_bounded_process(["python"], cwd=tmp_path, timeout_seconds=1)

    assert exc_info.value.code == "cleanup_failed"


class _UnkillableProcess:
    """A fake whose run fails AND whose cleanup then fails, on either platform.

    Windows hears the failed containment from `terminate_tree`/`close_handles`;
    POSIX hears it from the errno of the group signal, which
    `_fake_process_group(error=PermissionError)` supplies.
    """

    pid = UNCLAIMABLE_PID
    returncode: int | None = None
    stdin = None

    class _Pipe:
        def read(self, size: int) -> bytes: return b""
        def close(self) -> None: pass

    def __init__(self) -> None:
        self.stdout, self.stderr = self._Pipe(), self._Pipe()

    def wait(self, timeout: float) -> int: raise subprocess.TimeoutExpired([], timeout)
    def kill(self) -> bool: return False
    def terminate_tree(self) -> bool: return False
    def close_handles(self) -> bool: return False


def test_probe_transport_cleanup_failure_never_replaces_the_primary_diagnosis(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A run that already knows why it failed must not be relabelled by its own cleanup.

    Every recheck after cleanup is guarded by `if failure_code is None`; the
    cleanup assignment itself was not, so a determined `timeout` came back as
    `cleanup_failed`. That destroys the diagnosis at exactly the moment it is
    needed, and it is not a cosmetic loss: it collapses every distinct failure
    mode into one word, which is why #46's 62 macOS failures read as a single
    symptom and resisted bucketing.

    The leaked process is still reportable -- it moves to a flag rather than
    overwriting the code, because "cleanup failed" and "why the run failed" are
    two facts and the transport is the only place that knows both.
    """
    import graphite.probe_process as transport

    monkeypatch.setattr(transport, "_launch_process", lambda *args, **kwargs: _UnkillableProcess())
    _fake_process_group(monkeypatch, error=PermissionError)

    with pytest.raises(transport.ProbeProcessError) as exc_info:
        transport.run_bounded_process(["python"], cwd=tmp_path, timeout_seconds=0.25)

    assert exc_info.value.code == "timeout"
    assert exc_info.value.cleanup_failed is True


def test_probe_transport_cleanup_failure_never_replaces_the_childs_own_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An exit status is a diagnosis too, and cleanup must not overwrite it either.

    Ordering, stated once so it is not re-derived: a transport failure wins,
    then the child's own non-zero exit, and `cleanup_failed` is the code only
    when there is nothing else to report. `cleanup_failed` still never returns
    success -- `test_probe_transport_cleanup_failure_never_returns_success` is
    the counterweight that pins that end of the rule.
    """
    import graphite.probe_process as transport

    class Process(_UnkillableProcess):
        returncode = 1
        def wait(self, timeout: float) -> int: return 1

    monkeypatch.setattr(transport, "_launch_process", lambda *args, **kwargs: Process())
    _fake_process_group(monkeypatch, error=PermissionError)

    with pytest.raises(transport.ProbeProcessError) as exc_info:
        transport.run_bounded_process(["python"], cwd=tmp_path, timeout_seconds=1)

    assert exc_info.value.code == "nonzero"
    assert exc_info.value.cleanup_failed is True


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


@pytest.mark.skipif(os.name != "nt", reason="Windows CancelSynchronousIo handle contract")
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
    import ctypes
    import msvcrt
    from ctypes import wintypes
    from graphite.probe_process import run_bounded_process

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetFinalPathNameByHandleW.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    api.GetFinalPathNameByHandleW.restype = wintypes.DWORD

    def canonical_path_for_handle(handle: int) -> str:
        size = 512
        while True:
            buffer = ctypes.create_unicode_buffer(size)
            length = api.GetFinalPathNameByHandleW(handle, buffer, size, 0)
            if length == 0:
                raise ctypes.WinError(ctypes.get_last_error())
            if length < size:
                return buffer.value
            size = length + 1

    sentinel_path = tmp_path / "unrelated-handle-sentinel"
    sentinel_path.touch()
    script = """
import ctypes
import json
import sys
from ctypes import wintypes

api = ctypes.WinDLL("kernel32", use_last_error=True)
api.GetFinalPathNameByHandleW.argtypes = (
    wintypes.HANDLE,
    wintypes.LPWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
)
api.GetFinalPathNameByHandleW.restype = wintypes.DWORD

size = 512
path = None
while True:
    buffer = ctypes.create_unicode_buffer(size)
    length = api.GetFinalPathNameByHandleW(int(sys.argv[1]), buffer, size, 0)
    if length == 0:
        break
    if length < size:
        path = buffer.value
        break
    size = length + 1
print(json.dumps(path))
"""
    with sentinel_path.open("rb") as sentinel_file:
        handle = msvcrt.get_osfhandle(sentinel_file.fileno())
        sentinel_identity = canonical_path_for_handle(handle)
        os.set_handle_inheritable(handle, True)
        try:
            result = run_bounded_process(
                [sys.executable, "-c", script, str(handle)],
                cwd=tmp_path,
                timeout_seconds=5,
            )
        finally:
            os.set_handle_inheritable(handle, False)

    child_identity = json.loads(result.stdout)
    assert child_identity != sentinel_identity


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
    assert not any(str(tmp_path) in s for s in _iter_strings(check.to_dict()))


def test_core_deep_probe_uses_exact_offline_command_contract(tmp_path: Path) -> None:
    """Exact argv, and `-P` is part of the contract rather than incidental.

    `-B` was here alone and reads like a hardening flag: it suppresses bytecode
    writing and does nothing to `sys.path`. These launch `-m graphite` with a
    working directory the probe controls, so a `graphite.py` reachable as
    `sys.path[0]` would win -- measured, `python -B -m graphite` runs the shadow.

    NOT `-I`, which the MCP and LLM probes nearby do use. `-I` also implies
    `-E`, and these commands are the real graphite CLI, which reads its
    `GRAPHITE_*` configuration from the environment. Isolating that would fix
    the shadow by breaking the build.
    """
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
        (["PYTHON", "-B", "-P", "-m", "graphite", "--output-dir", str(out), "--cache-dir", str(cache), "--llm", "none", "build", str(repo)], work),
        (["PYTHON", "-B", "-P", "-m", "graphite", "validate", "--graph-json", str(graph), "--json"], work),
        (["PYTHON", "-B", "-P", "-m", "graphite", "query", "stats", "--graph-json", str(graph)], work),
    ]
    assert all("include-llm" not in argv for argv, _cwd in calls)


@pytest.mark.parametrize(("failure_code", "expected_type"), [("timeout", "timeout"), ("output_limit", "output_limit"), ("nonzero", "process")])
def test_core_deep_probe_maps_runner_failures_safely(tmp_path: Path, failure_code: str, expected_type: str) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_process import ProbeProcessError

    def fail(*args: object, **kwargs: object) -> object:
        raise ProbeProcessError(failure_code)
    check = probes.probe_core_pipeline(tmp_path, timeout_seconds=1, _runner=fail)
    leaked = list(_iter_strings(check.to_dict()))
    assert check.status == "blocked"
    assert check.details == {"error_type": expected_type, "code": failure_code}
    assert not any("RAW" in s for s in leaked)
    assert not any(str(tmp_path) in s for s in leaked)


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
    claimed_temp = tmp_path / "claimed-temp"
    claimed_temp.mkdir()
    os_temp = tmp_path / "os-temp"
    os_temp.mkdir()
    monkeypatch.setattr(probes.tempfile, "gettempdir", lambda: str(os_temp))
    lease = SimpleNamespace(
        path=outside,
        temp_root=claimed_temp,
        validate=lambda: None,
        cleanup=lambda: None,
    )
    check = probes.probe_core_pipeline(
        selected,
        _workspace_factory=lambda: lease,
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
    lease = SimpleNamespace(path=work, temp_root=tmp_path, validate=lambda: None, cleanup=lambda: None)
    check = probes.probe_core_pipeline(
        selected,
        _workspace_factory=lambda: lease,
    )
    assert check.status == "blocked"
    assert check.details == {"error_type": "isolation", "code": "overlapping_temp_path"}
    assert list(work.iterdir()) == []


@pytest.mark.parametrize("relationship", ["equal", "ancestor"])
def test_core_deep_probe_rejects_selected_root_containing_temp_before_creation(tmp_path: Path, relationship: str) -> None:
    import graphite.doctor_probes as probes

    temp_root = Path(tempfile.gettempdir()).resolve()
    selected = temp_root if relationship == "equal" else temp_root.parent
    created = False
    def factory() -> object:
        nonlocal created
        created = True
        raise AssertionError("temporary directory must not be created")
    check = probes.probe_core_pipeline(selected, _workspace_factory=factory)
    assert check.status == "blocked"
    assert check.details == {"error_type": "isolation", "code": "selected_contains_temp"}
    assert created is False


def test_core_deep_probe_allows_selected_sibling_under_temp(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    selected = tmp_path / "selected"
    selected.mkdir()
    outputs = iter([b"", b'{"ok":true,"error_count":0,"errors":[],"node_count":2,"edge_count":1}', b'{"node_count":2,"edge_count":1}'])
    from graphite.probe_workspace import ProbeWorkspaceLease
    def factory() -> object:
        return ProbeWorkspaceLease.acquire(temp_root=tmp_path)
    def run(*args: object, **kwargs: object) -> object:
        return probes.ProbeProcessResult(0, next(outputs), b"", 0.01)
    check = probes.probe_core_pipeline(selected, _runner=run, _workspace_factory=factory)
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
    from graphite.probe_workspace import ProbeWorkspaceLease

    created: list[Path] = []
    def tracking_factory() -> object:
        lease = ProbeWorkspaceLease.acquire()
        created.append(lease.parent_path)
        return lease

    assert probes.probe_core_pipeline(tmp_path, _workspace_factory=tracking_factory).status == "ready"
    assert created and not created[-1].exists()
    def fail(*args: object, **kwargs: object) -> object:
        raise ProbeProcessError("nonzero")
    assert probes.probe_core_pipeline(tmp_path, _runner=fail, _workspace_factory=tracking_factory).status == "blocked"
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
        # Must exceed the COARSEST monotonic tick on any supported interpreter,
        # not just this one. The budget is clock-derived, so a sleep shorter than
        # one tick lets two successive reads land in the same tick and the strict
        # `>` below compares a float to itself.
        #
        # Measured: Windows `time.monotonic()` is `GetTickCount64()` at ~15.6ms
        # resolution on 3.11/3.12, and `QueryPerformanceCounter()` at 1e-07 from
        # 3.13. The old 0.01 sleep sat UNDER the 3.11/3.12 tick, so this test
        # failed there with `assert 0.7840000000000487 > 0.7840000000000487` --
        # invisible on the 3.14 the gate runs. Found by the portability matrix.
        time.sleep(0.05)
        return probes.ProbeProcessResult(0, next(outputs), b"", 0.01)
    assert probes.probe_core_pipeline(tmp_path, timeout_seconds=1, _runner=run).status == "ready"
    assert timeouts[0] > timeouts[1] > timeouts[2] > 0

    outputs = iter([b"", b'{"ok":true,"error_count":0,"errors":[],"node_count":2,"edge_count":1}', b'{"node_count":2,"edge_count":1}'])
    from graphite.probe_workspace import ProbeWorkspaceLease
    lease = ProbeWorkspaceLease.acquire()
    def fail_cleanup() -> None:
        lease.cleanup()
        raise RuntimeError("RAW cleanup path")
    wrapped = SimpleNamespace(
        path=lease.path,
        temp_root=lease.temp_root,
        validate=lease.validate,
        cleanup=fail_cleanup,
    )
    check = probes.probe_core_pipeline(tmp_path, _runner=run, _workspace_factory=lambda: wrapped)
    assert check.status == "blocked"
    assert check.details == {"error_type": "cleanup", "code": "cleanup_failed"}
    assert "RAW" not in json.dumps(check.to_dict())


def test_core_probe_deadline_includes_temp_creation_and_verified_cleanup(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_workspace import ProbeWorkspaceLease

    selected = tmp_path / "selected"
    selected.mkdir()
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    created: list[Path] = []
    now = [0.0]
    def factory() -> object:
        lease = ProbeWorkspaceLease.acquire(temp_root=temp_root)
        created.append(lease.parent_path)
        now[0] = 0.95
        return lease
    check = probes.probe_core_pipeline(
        selected,
        timeout_seconds=1,
        _clock=lambda: now[0],
        _workspace_factory=factory,
    )
    assert check.status == "blocked"
    assert check.details == {"error_type": "timeout", "code": "timeout"}
    assert created and not created[0].exists()


def test_core_probe_blocking_cleanup_is_bounded_and_never_ready(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_workspace import ProbeWorkspaceLease

    release = threading.Event()
    created: list[Path] = []
    outputs = iter([
        b"",
        b'{"ok":true,"error_count":0,"errors":[],"node_count":2,"edge_count":1}',
        b'{"node_count":2,"edge_count":1}',
    ])
    lease = ProbeWorkspaceLease.acquire()
    created.append(lease.parent_path)
    def cleanup() -> None:
        release.wait(2)
        lease.cleanup()
    def run(*args: object, **kwargs: object) -> object:
        return probes.ProbeProcessResult(0, next(outputs), b"", 0.01)
    started = time.monotonic()
    wrapped = SimpleNamespace(
        path=lease.path,
        temp_root=lease.temp_root,
        validate=lease.validate,
        cleanup=cleanup,
    )
    check = probes.probe_core_pipeline(
        tmp_path,
        timeout_seconds=1.0,
        _runner=run,
        _workspace_factory=lambda: wrapped,
    )
    elapsed = time.monotonic() - started
    release.set()
    assert check.status == "blocked"
    assert check.details == {"error_type": "cleanup", "code": "cleanup_timeout"}
    assert elapsed < 2
    deadline = time.monotonic() + 1
    while created[0].exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not created[0].exists()


def test_core_probe_cleanup_timeout_uses_single_global_slot_and_recovers(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_workspace import ProbeWorkspaceLease

    release = threading.Event()
    lease = ProbeWorkspaceLease.acquire()
    acquisitions = 0
    response_index = 0
    responses = [
        b"",
        b'{"ok":true,"error_count":0,"errors":[],"node_count":2,"edge_count":1}',
        b'{"node_count":2,"edge_count":1}',
    ]

    def cleanup() -> None:
        release.wait(5)
        lease.cleanup()

    wrapped = SimpleNamespace(
        path=lease.path,
        temp_root=lease.temp_root,
        validate=lease.validate,
        cleanup=cleanup,
    )

    def factory() -> object:
        nonlocal acquisitions
        acquisitions += 1
        return wrapped if acquisitions == 1 else ProbeWorkspaceLease.acquire()

    def run(*args: object, **kwargs: object) -> object:
        nonlocal response_index
        result = probes.ProbeProcessResult(0, responses[response_index % 3], b"", 0.01)
        response_index += 1
        return result

    first = probes.probe_core_pipeline(
        tmp_path,
        timeout_seconds=0.5,
        _runner=run,
        _workspace_factory=factory,
    )
    try:
        second = probes.probe_core_pipeline(
            tmp_path,
            timeout_seconds=0.5,
            _runner=run,
            _workspace_factory=factory,
        )
        assert first.details == {"error_type": "cleanup", "code": "cleanup_timeout"}
        assert second.details == {"error_type": "cleanup", "code": "cleanup_busy"}
        assert acquisitions == 1
    finally:
        release.set()

    deadline = time.monotonic() + 2
    while lease.parent_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not lease.parent_path.exists()
    assert probes.probe_core_pipeline(tmp_path, _runner=run).status == "ready"


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


def test_run_deep_probes_reports_core_mcp_typescript_and_llm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import graphite.doctor_probes as probes

    core = DoctorCheck("deep_core", "Deterministic pipeline", "ready", "ok")
    mcp = DoctorCheck("deep_mcp", "MCP", "ready", "ok")
    typescript = DoctorCheck("deep_typescript", "TypeScript", "ready", "ok")
    llm = DoctorCheck("deep_llm", "LLM", "ready", "ok")
    monkeypatch.setattr(probes, "probe_core_pipeline", lambda root: core)
    monkeypatch.setattr(probes, "probe_mcp", lambda root: mcp)
    monkeypatch.setattr(probes, "probe_typescript", lambda root: typescript)
    monkeypatch.setattr(probes, "probe_llm", lambda cfg: llm)
    assert probes.run_deep_probes(
        tmp_path,
        cfg=Config(llm_mode="cloud"),
        include_llm=True,
    ) == [core, mcp, typescript, llm]


def test_llm_worker_uses_exact_synthetic_content_once_and_never_returns_response() -> None:
    from graphite.llm import CompletionResult
    from graphite.llm_probe import run_synthetic_probe

    calls: list[tuple[str, str]] = []
    worker_limits: list[int] = []

    class RecordingProvider:
        name = "fake"

        def complete(self, system: str, user: str) -> CompletionResult:
            calls.append((system, user))
            return CompletionResult(text="unexpected probabilistic response with private data")

    def factory(cfg: Config) -> RecordingProvider:
        worker_limits.append(cfg.llm_max_output_tokens)
        return RecordingProvider()

    result = run_synthetic_probe(
        Config(llm_mode="cloud", llm_provider="fake"),
        provider_factory=factory,
    )

    assert calls == [
        (
            "You are a connectivity probe. Reply with READY only.",
            "Synthetic Graphite connectivity test. No repository data is included.",
        )
    ]
    assert worker_limits == [16]
    assert result == {"status": "ready", "response_present": True}
    assert "private data" not in json.dumps(result)


def test_llm_worker_maps_unknown_exception_to_fixed_provider_error() -> None:
    from graphite.llm import CompletionResult
    from graphite.llm_probe import run_synthetic_probe

    sentinel = "ApiKey_SECRET"
    unsafe_error = type(sentinel, (Exception,), {})

    class FailingProvider:
        name = "fake"

        def complete(self, system: str, user: str) -> CompletionResult:
            del system, user
            raise unsafe_error(f"{sentinel} https://private.invalid?key={sentinel}")

    result = run_synthetic_probe(
        Config(llm_mode="cloud", llm_provider="fake"),
        provider_factory=lambda cfg: FailingProvider(),
    )

    assert result == {"status": "degraded", "category": "provider_error"}
    assert sentinel not in json.dumps(result)


def test_llm_worker_rejects_missing_or_empty_completion_text() -> None:
    from graphite.llm_probe import run_synthetic_probe

    for completion in (None, SimpleNamespace(), SimpleNamespace(text=""), SimpleNamespace(text=7)):
        provider = SimpleNamespace(complete=lambda system, user, value=completion: value)
        result = run_synthetic_probe(
            Config(llm_mode="cloud", llm_provider="fake"),
            provider_factory=lambda cfg, value=provider: value,  # type: ignore[arg-type,return-value]
        )
        assert result == {"status": "degraded", "category": "provider_error"}


def test_llm_parent_uses_isolated_bounded_worker_and_canonicalizes_provider(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    sentinel = "ApiKey_SECRET"
    captured: dict[str, object] = {}

    def run(argv: list[str], **kwargs: object) -> probes.ProbeProcessResult:
        captured.update(argv=argv, **kwargs)
        return probes.ProbeProcessResult(
            0,
            b'{"status":"ready","response_present":true}',
            f"ignored stderr {sentinel}".encode(),
            0.01,
        )

    check = probes.probe_llm(
        Config(
            llm_mode="cloud",
            llm_provider=f"https://provider.invalid?secret={sentinel}",
            llm_base_url="https://provider.invalid/v1",
            llm_api_key=sentinel,
            llm_timeout_seconds=2.5,
        ),
        _runner=run,
    )

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[1:4] == ["-I", "-S", "-B"]
    assert argv[-1].endswith("llm_probe.py")
    assert sentinel not in " ".join(argv)
    assert captured["cwd"] == Path(argv[0]).resolve().parent
    assert captured["timeout_seconds"] == 2.5
    assert captured["max_output_bytes"] == 4096
    assert captured["check"] is False
    worker_input = json.loads(captured["stdin"])
    assert worker_input["system"] == "You are a connectivity probe. Reply with READY only."
    assert worker_input["user"] == "Synthetic Graphite connectivity test. No repository data is included."
    assert check.status == "ready"
    assert check.details == {"provider": "custom/unknown", "response_present": True}
    assert sentinel not in json.dumps(check.to_dict())


@pytest.mark.parametrize(
    ("result", "expected_category"),
    [
        (b'{"status":"degraded","category":"authentication"}', "authentication"),
        (b'{"status":"degraded","category":"ApiKey_SECRET"}', "provider_error"),
        (b'{"status":"ready","response_present":false}', "provider_error"),
        (b'{"status":"ready","response_present":true,"response":"SECRET"}', "provider_error"),
        (b"not-json SECRET https://private.invalid", "provider_error"),
    ],
)
def test_llm_parent_accepts_only_fixed_worker_schema(
    result: bytes,
    expected_category: str,
) -> None:
    import graphite.doctor_probes as probes

    check = probes.probe_llm(
        Config(llm_mode="cloud", llm_provider="ollama"),
        _runner=lambda *args, **kwargs: probes.ProbeProcessResult(0, result, b"SECRET", 0.01),
    )

    assert check.status == "degraded"
    assert check.summary == "synthetic connectivity probe failed"
    assert check.details == {"category": expected_category}
    assert "SECRET" not in json.dumps(check.to_dict())


def test_llm_deep_probe_disabled_never_starts_worker() -> None:
    import graphite.doctor_probes as probes

    def unexpected_runner(*args: object, **kwargs: object) -> object:
        raise AssertionError("disabled probe started a worker")

    check = probes.probe_llm(Config(llm_mode="none"), _runner=unexpected_runner)

    assert check.status == "optional"


def test_llm_parent_maps_transport_timeout_and_output_overflow_to_fixed_categories() -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_process import ProbeProcessError

    for code, category in (("timeout", "timeout"), ("output_limit", "provider_error")):
        def fail(*args: object, **kwargs: object) -> object:
            raise ProbeProcessError(code)

        check = probes.probe_llm(
            Config(llm_mode="cloud", llm_provider="ollama"),
            _runner=fail,
        )
        assert check.details == {"category": category}


def test_llm_parent_maps_nonzero_worker_exit_without_stderr_leakage() -> None:
    import graphite.doctor_probes as probes

    sentinel = "ApiKey_SECRET"
    check = probes.probe_llm(
        Config(llm_mode="cloud", llm_provider="ollama"),
        _runner=lambda *args, **kwargs: probes.ProbeProcessResult(
            9,
            b"",
            sentinel.encode(),
            0.01,
        ),
    )

    assert check.details == {"category": "provider_error"}
    assert sentinel not in json.dumps(check.to_dict())


def test_llm_parent_timeout_is_wall_clock_bounded_and_leaves_no_orphan(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_process import run_bounded_process

    sentinel = tmp_path / "llm-orphan.txt"
    grandchild = (
        "import pathlib,sys,time;time.sleep(1);"
        "pathlib.Path(sys.argv[1]).write_text('orphan',encoding='utf-8')"
    )
    child = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r},sys.argv[1]]);"
        "time.sleep(5)"
    )

    def contained_runner(*args: object, **kwargs: object) -> probes.ProbeProcessResult:
        return run_bounded_process(
            [sys.executable, "-c", child, str(sentinel)],
            cwd=tmp_path,
            timeout_seconds=0.4,
            max_output_bytes=4096,
            check=False,
        )

    started = time.monotonic()
    check = probes.probe_llm(
        Config(llm_mode="cloud", llm_provider="ollama", llm_timeout_seconds=0.4),
        _runner=contained_runner,
    )

    assert time.monotonic() - started < 1.5
    assert check.details == {"category": "timeout"}
    time.sleep(1.1)
    assert not sentinel.exists()


def test_llm_real_isolated_worker_rejects_unsupported_provider_without_network() -> None:
    import graphite.doctor_probes as probes

    check = probes.probe_llm(
        Config(
            llm_mode="cloud",
            llm_provider="unsupported-provider",
            llm_timeout_seconds=2,
        )
    )

    assert check.status == "degraded"
    assert check.details == {"category": "configuration"}


def test_llm_parent_rejects_encoded_payload_over_transport_limit_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graphite.doctor_probes as probes

    monkeypatch.setattr(probes, "INPUT_LIMIT_BYTES", 1024, raising=False)
    runner_calls = 0

    def unexpected_runner(*args: object, **kwargs: object) -> object:
        nonlocal runner_calls
        runner_calls += 1
        raise AssertionError("oversized encoded payload started worker")

    check = probes.probe_llm(
        Config(
            llm_mode="cloud",
            llm_provider="ollama",
            llm_api_key="\U0001f4a5" * 100,
        ),
        _runner=unexpected_runner,
    )

    assert check.details == {"category": "configuration"}
    assert runner_calls == 0


def test_run_deep_probes_not_requested_makes_zero_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import graphite.doctor_probes as probes

    monkeypatch.setattr(probes, "probe_core_pipeline", lambda root: DoctorCheck("deep_core", "core", "ready", "ok"))
    monkeypatch.setattr(probes, "probe_mcp", lambda root: DoctorCheck("deep_mcp", "mcp", "ready", "ok"))
    monkeypatch.setattr(probes, "probe_typescript", lambda root: DoctorCheck("deep_typescript", "ts", "optional", "ok"))
    llm_calls = 0

    def unexpected_probe(cfg: Config) -> DoctorCheck:
        nonlocal llm_calls
        llm_calls += 1
        raise AssertionError("LLM probe must not run")

    monkeypatch.setattr(probes, "probe_llm", unexpected_probe)

    checks = probes.run_deep_probes(
        tmp_path,
        cfg=Config(llm_mode="cloud", llm_provider="fake"),
        include_llm=False,
    )

    assert llm_calls == 0
    assert checks[-1] == DoctorCheck(
        "deep_llm",
        "LLM",
        "optional",
        "not requested; use --deep --include-llm",
    )


def _mcp_probe_output(*messages: dict[str, object]) -> bytes:
    pending: queue.Queue[dict[str, object]] = queue.Queue()
    for message in messages:
        pending.put(message)
    return b"".join(json.dumps(pending.get_nowait()).encode("utf-8") + b"\n" for _ in messages)


def _mcp_tools_result() -> dict[str, object]:
    return {
        "tools": [
            {"name": "graphite_query"},
            {"name": "graphite_summary"},
            {"name": "graphite_community"},
            {"name": "graphite_refresh"},
        ]
    }


def _bounded_manifest_builder_runner(selected: Path) -> object:
    import graphite.doctor_probes as probes

    dependency_root = selected.parent / f".{selected.name}-bounded-mcp-dependencies"
    dependency_root.mkdir()
    files: list[Path] = []
    package_roots: dict[str, Path] = {}
    metadata_roots: dict[str, Path] = {}
    packages: dict[str, dict[str, object]] = {}
    distributions: dict[str, dict[str, object]] = {}
    for name in ("mcp", "networkx"):
        package_root = dependency_root / name
        package_root.mkdir()
        package_roots[name] = package_root
        origin = package_root / "__init__.py"
        origin.write_text("", encoding="utf-8")
        files.append(origin)
        metadata_root = dependency_root / f"{name}-1.0.dist-info"
        metadata_root.mkdir()
        metadata_roots[name] = metadata_root
    search_binding = probes._path_binding(dependency_root, require_directory=True)
    for name, origin in zip(("mcp", "networkx"), files, strict=True):
        package_root = package_roots[name]
        metadata_root = metadata_roots[name]
        distributions[name] = probes._path_binding(metadata_root, require_directory=True)
        packages[name] = {
            "origin": probes._path_binding(origin, require_directory=False),
            "root": probes._path_binding(package_root, require_directory=True),
            "search": search_binding,
        }
    entries: list[list[object]] = []
    for path in files:
        stat = path.stat()
        entries.append(
            [
                path.relative_to(dependency_root).as_posix(),
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
            ]
        )
    manifest = {
        "distributions": distributions,
        "files": [
            {
                "root": probes._path_binding(dependency_root, require_directory=True),
                "entries": entries,
            }
        ],
        "packages": packages,
    }
    trusted = Path(probes.__file__).absolute().parent.parent
    envelope = {
        "bindings": {
            "doctor": probes._path_binding(
                trusted / "graphite" / "doctor_probes.py",
                require_directory=False,
            ),
            "init": probes._path_binding(
                trusted / "graphite" / "__init__.py",
                require_directory=False,
            ),
            "mcp": probes._path_binding(
                trusted / "graphite" / "mcp.py",
                require_directory=False,
            ),
            "selected": probes._path_binding(selected, require_directory=True),
            "trusted": probes._path_binding(trusted, require_directory=True),
        },
        "manifest": manifest,
    }
    payload = json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    def run(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return probes.ProbeProcessResult(0, payload, b"", 0.01)

    return run


def _trusted_mcp_child_bindings(selected: Path) -> tuple[dict[str, object], ...]:
    import graphite.doctor_probes as probes

    trusted = Path(probes.__file__).absolute().parent.parent
    return (
        probes._path_binding(trusted, require_directory=True),
        probes._path_binding(trusted / "graphite" / "__init__.py", require_directory=False),
        probes._path_binding(trusted / "graphite" / "mcp.py", require_directory=False),
        probes._path_binding(selected, require_directory=True),
    )


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

    def build(argv: list[str], **kwargs: object) -> object:
        """Run the real manifest builder, but under a real interpreter.

        `probe_mcp` launches two children: this builder and the server. Only
        the server was stubbed, so the builder really executed `"PYTHON"` --
        which resolved on Windows alone, where the filesystem is
        case-insensitive and PATHEXT appends `.EXE`. On POSIX it is ENOENT, so
        the probe reported `launch_failed` before the stub was ever reached.

        The sentinel cannot simply become `sys.executable`: it is what proves
        `python_executable` is forwarded verbatim, and a bug that ignored the
        parameter would fall back to `sys.executable` and go unnoticed. So keep
        the sentinel, assert it arrives here too, and substitute a real
        interpreter only for the launch -- which is exactly what Windows was
        doing by accident. The manifest below is therefore still a real one.
        """
        assert argv[0] == "PYTHON"
        return probes.run_bounded_process([sys.executable, *argv[1:]], **kwargs)

    check = probes.probe_mcp(
        tmp_path,
        python_executable="PYTHON",
        timeout_seconds=20,
        _runner=run,
        _builder_runner=build,
    )

    assert check.status == "ready"
    assert check.details == {"server_name": "graphite", "tool_count": 4}
    argv = captured["argv"]
    assert argv[0:5] == ["PYTHON", "-I", "-S", "-B", "-c"]
    assert "graphite.mcp" in argv[5]
    trusted_binding = json.loads(argv[6])
    selected_binding = json.loads(argv[9])
    assert Path(trusted_binding["lexical"]).is_absolute()
    assert Path(trusted_binding["canonical"]).is_absolute()
    assert Path(selected_binding["canonical"]) == tmp_path.resolve()
    assert captured["cwd"] == tmp_path
    assert 0 < captured["timeout_seconds"] <= 20
    assert captured["max_output_bytes"] == 1024 * 1024
    # Length-prefixed framing: a digit header, then exactly that many manifest
    # bytes, then the protocol input. The child must be able to find the
    # boundary without scanning, or it buffers the protocol away from the
    # server's own reader.
    header, separator, rest = captured["stdin"].partition(b"\n")
    assert separator == b"\n"
    assert header.isdigit()
    manifest = json.loads(rest[: int(header)])
    assert "networkx" in manifest["packages"]
    assert "mcp" in manifest["packages"]
    assert manifest["files"]
    requests = [json.loads(line) for line in rest[int(header) :].splitlines()]
    assert requests[0]["method"] == "initialize"
    assert requests[0]["params"]["protocolVersion"] == "2024-11-05"
    assert requests[1]["method"] == "notifications/initialized"
    assert requests[2]["method"] == "tools/list"
    assert all(request.get("params", {}).get("name") != "graphite_refresh" for request in requests)


def test_mcp_import_manifest_excludes_arbitrary_pth_added_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import graphite.doctor_probes as probes

    selected = tmp_path / "selected"
    selected.mkdir()
    arbitrary = tmp_path / "external-package-source"
    arbitrary.mkdir()
    monkeypatch.setattr(probes.sys, "path", [str(arbitrary), *probes.sys.path])

    manifest = probes._mcp_import_manifest(selected)

    assert not any(str(arbitrary.resolve()) in s for s in _iter_strings(manifest))


def test_mcp_requirement_marker_evaluates_and_or_compounds() -> None:
    import graphite.doctor_probes as probes

    applies = probes._requirement_applies
    assert applies("dep>=1; python_version >= '3.11' and python_version < '3'") is False
    assert applies("dep>=1; python_version < '3' or python_version >= '3.11'") is True
    assert applies(
        "dep>=1; python_version >= '3.11' or python_version >= '3.11' and python_version < '3'"
    ) is True
    expected = sys.platform == "win32" and sys.version_info[:2] < (3, 14)
    assert applies("pywin32>=310; sys_platform == 'win32' and python_version < '3.14'") is expected
    assert applies("dep>=1; extra == 'test' and sys_platform == 'win32'") is False
    # A single parenthesised comparison is now unwrapped and evaluated: real
    # metadata pairs one with an `extra` marker, and raising took the whole
    # distribution walk down with it.
    wrapped = sys.platform == "win32" and sys.version_info[:2] < (3, 14)
    assert applies("dep>=1; (sys_platform == 'win32') and python_version < '3.14'") is wrapped
    # Genuinely nested boolean sub-expressions remain unsupported and fail closed.
    with pytest.raises(ValueError):
        applies("dep>=1; python_version >= '3' and (sys_platform == 'win32' or python_version < '3.14')")


def test_mcp_deep_probe_rejects_user_site_dependency_shadow_without_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import graphite.doctor_probes as probes

    selected = tmp_path / "selected"
    selected.mkdir()
    fake_user_site = tmp_path / "user-site"
    fake_user_site.mkdir()
    sentinel = tmp_path / "dependency-shadow-executed.txt"
    (fake_user_site / "networkx.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    (fake_user_site / "unrelated.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('unrelated')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(probes.sys, "path", [str(fake_user_site), *probes.sys.path])

    check = probes.probe_mcp(selected, timeout_seconds=20)

    assert check.status == "degraded"
    assert check.details == {"code": "probe_failed"}
    assert not sentinel.exists()


def test_mcp_manifest_builder_never_imports_metadata_root_shadows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import graphite.doctor_probes as probes

    selected = tmp_path / "selected"
    selected.mkdir()
    metadata_root = tmp_path / "metadata-root"
    metadata_root.mkdir()
    sentinel = tmp_path / "metadata-shadow-executed.txt"
    shadow = (
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text(__name__, encoding='utf-8')\n"
        "raise RuntimeError('metadata roots are data only')\n"
    )
    for name in ("platform.py", "shutil.py", "threading.py", "sitecustomize.py"):
        (metadata_root / name).write_text(shadow, encoding="utf-8")
    futures = metadata_root / "concurrent" / "futures"
    futures.mkdir(parents=True)
    (metadata_root / "concurrent" / "__init__.py").write_text(shadow, encoding="utf-8")
    (futures / "__init__.py").write_text(shadow, encoding="utf-8")
    monkeypatch.setattr(probes.sys, "path", [str(metadata_root), *probes.sys.path])
    output = _mcp_probe_output(
        {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "graphite"}}},
        {"jsonrpc": "2.0", "id": 2, "result": _mcp_tools_result()},
    )

    check = probes.probe_mcp(
        selected,
        timeout_seconds=20,
        _runner=lambda *a, **k: probes.ProbeProcessResult(0, output, b"", 0.01),
    )

    assert not sentinel.exists()
    assert check.status in {"ready", "degraded"}
    if check.status == "degraded":
        assert check.details == {"code": "probe_failed"}


def test_mcp_manifest_builder_rejects_metadata_root_inside_selected_without_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import graphite.doctor_probes as probes

    selected = tmp_path / "selected"
    metadata_root = selected / "metadata-root"
    metadata_root.mkdir(parents=True)
    dist_info = metadata_root / "attacker-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: attacker\nVersion: 1.0\n",
        encoding="utf-8",
    )
    sentinel = tmp_path / "selected-metadata-shadow-executed.txt"
    (metadata_root / "platform.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(probes.sys, "path", [str(metadata_root), *probes.sys.path])

    check = probes.probe_mcp(selected, timeout_seconds=20)

    assert check.status == "degraded"
    assert check.details == {"code": "probe_failed"}
    assert not sentinel.exists()


def test_mcp_bootstrap_rejects_manifest_files_inside_selected_root(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_process import run_bounded_process

    selected = tmp_path / "selected"
    selected.mkdir()
    repository_file = selected / "dependency.py"
    repository_file.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
    stat = repository_file.stat()
    manifest = {
        "distributions": {},
        "files": [
            {
                "root": probes._path_binding(selected, require_directory=True),
                "entries": [
                    [
                        repository_file.name,
                        stat.st_dev,
                        stat.st_ino,
                        stat.st_size,
                        stat.st_mtime_ns,
                    ]
                ],
            }
        ],
        "packages": {},
    }
    trusted, package_init, mcp_source, selected_binding = _trusted_mcp_child_bindings(selected)

    result = run_bounded_process(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            probes._MCP_BOOTSTRAP,
            json.dumps(trusted, separators=(",", ":")),
            json.dumps(package_init, separators=(",", ":")),
            json.dumps(mcp_source, separators=(",", ":")),
            json.dumps(selected_binding, separators=(",", ":")),
        ],
        cwd=selected,
        stdin=json.dumps(manifest).encode("utf-8") + b"\n",
        timeout_seconds=5,
        check=False,
    )

    assert result.returncode == 70


def test_mcp_manifest_builder_is_hard_bounded_and_leaves_no_orphan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import graphite.doctor_probes as probes

    sentinel = tmp_path / "slow-builder-survived.txt"
    script = (
        "import time;from pathlib import Path;time.sleep(0.3);"
        f"Path({str(sentinel)!r}).write_text('orphan')"
    )
    parent_bindings: list[object] = []

    def reject_parent_binding(*args: object, **kwargs: object) -> object:
        parent_bindings.append((args, kwargs))
        raise AssertionError("parent path binding must not run")

    monkeypatch.setattr(probes, "_path_binding", reject_parent_binding)
    monkeypatch.setattr(probes, "_validate_path_binding", reject_parent_binding)
    started = time.monotonic()

    check = probes.probe_mcp(tmp_path, timeout_seconds=0.05, _builder_script=script)

    assert time.monotonic() - started < 0.3
    assert check.status == "degraded"
    assert check.details == {"code": "timeout"}
    assert parent_bindings == []
    time.sleep(0.35)
    assert not sentinel.exists()


def test_cached_path_binding_rejects_symlink_retarget(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    approved = tmp_path / "approved.py"
    replacement = tmp_path / "replacement.py"
    approved.write_text("VALUE = 'approved'\n", encoding="utf-8")
    replacement.write_text("VALUE = 'replacement'\n", encoding="utf-8")
    link = tmp_path / "dependency.py"
    try:
        link.symlink_to(approved)
    except OSError:
        pytest.skip("file symlinks are unavailable")
    binding = probes._path_binding(link, require_directory=False)
    probes._validate_path_binding(binding, require_directory=False)
    link.unlink()
    link.symlink_to(replacement)

    with pytest.raises(ValueError):
        probes._validate_path_binding(binding, require_directory=False)


def test_cached_mcp_manifest_revalidation_rejects_root_retarget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import graphite.doctor_probes as probes

    selected = tmp_path / "selected"
    selected.mkdir()
    base_runner = _bounded_manifest_builder_runner(selected)
    base_result = base_runner()
    assert isinstance(base_result, probes.ProbeProcessResult)
    envelope = json.loads(base_result.stdout)
    approved = tmp_path / ".selected-bounded-mcp-dependencies"
    replacement = tmp_path / "replacement-dependencies"
    shutil.copytree(approved, replacement)
    link = tmp_path / "dependency-root"
    try:
        link.symlink_to(approved, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    def rewrite_lexical(value: object) -> None:
        if isinstance(value, dict):
            if set(value) == {"canonical", "identity", "lexical"}:
                lexical = str(value["lexical"])
                value["lexical"] = lexical.replace(str(approved), str(link), 1)
            else:
                for item in value.values():
                    rewrite_lexical(item)
        elif isinstance(value, list):
            for item in value:
                rewrite_lexical(item)

    rewrite_lexical(envelope["manifest"])
    cached_result = probes.ProbeProcessResult(
        0,
        json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
        b"",
        0.01,
    )
    output = _mcp_probe_output(
        {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "graphite"}}},
        {"jsonrpc": "2.0", "id": 2, "result": _mcp_tools_result()},
    )
    monkeypatch.setattr(probes, "_MCP_MANIFEST_CACHE", None)

    first = probes.probe_mcp(
        selected,
        timeout_seconds=20,
        _builder_runner=lambda *a, **k: cached_result,
        _runner=lambda *a, **k: probes.ProbeProcessResult(0, output, b"", 0.01),
    )
    assert first.status == "ready"
    link.unlink()
    link.symlink_to(replacement, target_is_directory=True)

    child_called = False

    def unexpected_child(*args: object, **kwargs: object) -> object:
        nonlocal child_called
        child_called = True
        raise AssertionError("invalid cached manifest must not reach MCP child")

    second = probes.probe_mcp(selected, timeout_seconds=20, _runner=unexpected_child)

    assert second.status == "degraded"
    assert second.details == {"code": "probe_failed"}
    assert child_called is False


def test_mcp_manifest_fits_bounded_child_input(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_process import INPUT_LIMIT_BYTES

    manifest = probes._mcp_import_manifest(tmp_path)
    protocol_reserve = 4096

    assert len(json.dumps(manifest, separators=(",", ":")).encode("utf-8")) < (
        INPUT_LIMIT_BYTES - protocol_reserve
    )


def test_mcp_manifest_builder_rejects_trusted_source_symlink_escape(
    tmp_path: Path,
) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_process import run_bounded_process

    trusted = tmp_path / "trusted"
    package = trusted / "graphite"
    package.mkdir(parents=True)
    selected = tmp_path / "selected"
    selected.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "mcp.py").write_text("", encoding="utf-8")
    sentinel = tmp_path / "trusted-source-executed.txt"
    outside = tmp_path / "outside-doctor-probes.py"
    outside.write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    try:
        (package / "doctor_probes.py").symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    result = run_bounded_process(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            probes._MCP_MANIFEST_BUILDER_BOOTSTRAP,
            str(trusted),
            str(selected),
            "[]",
        ],
        cwd=selected,
        stdin=b"",
        timeout_seconds=5,
        check=False,
    )

    assert result.returncode == 70
    assert not sentinel.exists()


def test_mcp_bootstrap_rejects_mcp_symlink_escape(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_process import run_bounded_process

    trusted = tmp_path / "trusted"
    package = trusted / "graphite"
    package.mkdir(parents=True)
    selected = tmp_path / "selected"
    selected.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    sentinel = tmp_path / "escaped-mcp-executed.txt"
    outside = tmp_path / "outside-mcp.py"
    outside.write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    try:
        (package / "mcp.py").symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    bindings = [
        probes._path_binding(trusted, require_directory=True),
        probes._path_binding(package / "__init__.py", require_directory=False),
        probes._path_binding(package / "mcp.py", require_directory=False),
        probes._path_binding(selected, require_directory=True),
    ]
    result = run_bounded_process(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            probes._MCP_BOOTSTRAP,
            *(json.dumps(binding, separators=(",", ":")) for binding in bindings),
        ],
        cwd=selected,
        stdin=b'{"distributions":{},"files":[],"packages":{}}\n',
        timeout_seconds=5,
        check=False,
    )

    assert result.returncode == 70
    assert not sentinel.exists()


def test_mcp_bootstrap_rejects_canonical_binding_mismatch(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_process import run_bounded_process

    selected = tmp_path / "selected"
    selected.mkdir()
    lexical = tmp_path / "dependency.py"
    canonical = tmp_path / "different.py"
    lexical.write_text("VALUE = 1\n", encoding="utf-8")
    canonical.write_text("VALUE = 2\n", encoding="utf-8")
    stat = canonical.stat()
    manifest = {
        "distributions": {},
        "files": [
            {
                "root": {
                    "lexical": str(lexical),
                    "canonical": str(canonical),
                    "identity": [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns],
                },
                "entries": [],
            }
        ],
        "packages": {},
    }
    trusted, package_init, mcp_source, selected_binding = _trusted_mcp_child_bindings(selected)

    result = run_bounded_process(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            probes._MCP_BOOTSTRAP,
            json.dumps(trusted, separators=(",", ":")),
            json.dumps(package_init, separators=(",", ":")),
            json.dumps(mcp_source, separators=(",", ":")),
            json.dumps(selected_binding, separators=(",", ":")),
        ],
        cwd=selected,
        stdin=json.dumps(manifest).encode("utf-8") + b"\n",
        timeout_seconds=5,
        check=False,
    )

    assert result.returncode == 70


@pytest.mark.parametrize("failure_code", ["timeout", "output_limit", "nonzero", "io_failed"])
def test_mcp_deep_probe_maps_transport_failures_to_degraded(tmp_path: Path, failure_code: str) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_process import ProbeProcessError

    def fail(*args: object, **kwargs: object) -> object:
        raise ProbeProcessError(failure_code)

    builder_runner = _bounded_manifest_builder_runner(tmp_path)
    started = time.monotonic()
    check = probes.probe_mcp(
        tmp_path,
        timeout_seconds=0.2,
        _runner=fail,
        _builder_script="pass",
        _builder_runner=builder_runner,
    )
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


@pytest.mark.parametrize(
    "output",
    [
        _mcp_probe_output(
            {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "graphite"}}},
            {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "graphite"}}},
            {"jsonrpc": "2.0", "id": 2, "result": _mcp_tools_result()},
        ),
        _mcp_probe_output(
            {"jsonrpc": "1.0", "id": 1, "result": {"serverInfo": {"name": "graphite"}}},
            {"jsonrpc": "2.0", "id": 2, "result": _mcp_tools_result()},
        ),
        _mcp_probe_output(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "secret"}},
            {"jsonrpc": "2.0", "id": 2, "result": _mcp_tools_result()},
        ),
        _mcp_probe_output(
            {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "graphite"}}},
            {"jsonrpc": "2.0", "id": 2, "result": _mcp_tools_result()},
            {"jsonrpc": "2.0", "id": 3, "result": {}},
        ),
        _mcp_probe_output(
            {"jsonrpc": "2.0", "method": 7, "params": {}},
            {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "graphite"}}},
            {"jsonrpc": "2.0", "id": 2, "result": _mcp_tools_result()},
        ),
        _mcp_probe_output(
            {"jsonrpc": "2.0", "id": True, "result": {}},
            {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "graphite"}}},
            {"jsonrpc": "2.0", "id": 2, "result": _mcp_tools_result()},
        ),
        _mcp_probe_output(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"serverInfo": {"name": "graphite"}},
                "error": {"code": -1},
            },
            {"jsonrpc": "2.0", "id": 2, "result": _mcp_tools_result()},
        ),
        (
            b'{"jsonrpc":"2.0","method":"progress","params":[NaN]}\n'
            + _mcp_probe_output(
                {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "graphite"}}},
                {"jsonrpc": "2.0", "id": 2, "result": _mcp_tools_result()},
            )
        ),
        (
            b'{"jsonrpc":"2.0","method":"progress","params":{"value":1e9999}}\n'
            + _mcp_probe_output(
                {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "graphite"}}},
                {"jsonrpc": "2.0", "id": 2, "result": _mcp_tools_result()},
            )
        ),
    ],
    ids=[
        "duplicate-id",
        "wrong-jsonrpc-version",
        "error-response",
        "unexpected-id",
        "malformed-notification",
        "boolean-id",
        "result-and-error",
        "non-json-number",
        "overflow-number",
    ],
)
def test_mcp_deep_probe_strictly_validates_protocol_envelopes(tmp_path: Path, output: bytes) -> None:
    import graphite.doctor_probes as probes

    check = probes.probe_mcp(
        tmp_path,
        _runner=lambda *a, **k: probes.ProbeProcessResult(0, output, b"", 0.01),
    )
    assert check.status == "degraded"
    assert check.details == {"code": "invalid_response"}
    assert "secret" not in check.summary


def test_mcp_deep_probe_rejects_excessive_lines_and_nesting_before_json_decode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import graphite.doctor_probes as probes

    too_many_lines = _mcp_probe_output(
        *({"jsonrpc": "2.0", "method": "progress", "params": {}} for _ in range(65))
    )
    assert probes.probe_mcp(
        tmp_path,
        _runner=lambda *a, **k: probes.ProbeProcessResult(0, too_many_lines, b"", 0.01),
    ).status == "degraded"

    deeply_nested = (
        b'{"jsonrpc":"2.0","method":"progress","params":'
        + b"[" * 80
        + b"0"
        + b"]" * 80
        + b"}\n"
    )
    called = False
    real_loads = probes.json.loads

    def tracked_loads(value: object, *args: object, **kwargs: object) -> object:
        nonlocal called
        if value == deeply_nested.decode("utf-8"):
            called = True
        return real_loads(value, *args, **kwargs)

    monkeypatch.setattr(probes.json, "loads", tracked_loads)
    check = probes.probe_mcp(
        tmp_path,
        _runner=lambda *a, **k: probes.ProbeProcessResult(0, deeply_nested, b"", 0.01),
    )
    assert check.status == "degraded"
    assert called is False


# --- real-server deep probe (issue #29, formerly quarantined) ----------------
#
# These two drive the real MCP server over stdio. They spent time under an
# `xfail(CI, strict=False)` quarantine while #29 was open, failing ~38% of CI
# executions (13 of 34). They are GATING again as of the fix in `7db0d40`.
#
# Three causes were found and fixed, and the order matters because each was
# hidden by the one before it:
#
#   1. The bootstrap read its manifest with `sys.stdin.buffer.readline()`, which
#      over-read the protocol input into a buffer the server's own fd-0 reader
#      could never see (`cb82b73`). Deterministic under mcp 2.x.
#   2. A cold manifest build ate the whole 20s budget and reported `timeout`
#      before starting a child -- separate budgets plus a resolve memo
#      (`c7985c5`).
#   3. The real one: the probe closed the child's stdin the instant the payload
#      was written, so EOF landed while the child was still starting up. mcp's
#      receive loop closes the WRITE stream on that EOF, dropping a reply still
#      in flight. `initialize` is answered inline inside the receive loop and so
#      could never be lost; `tools/list` is dispatched to a concurrent task and
#      so could. stdin is now held open until both response ids arrive.
#
# Measured after: 8 dispatch runs, 32 executions, 0 failures. P(that | the rate
# were still 38%) = 2.3e-07. `outlived_close_s` fell 5.14s -> 0.18s (mcp 1.23.3)
# and 5.04s -> 0.23s (2.0.0), which is the mechanism check rather than the
# outcome: the close now follows the child's work instead of preceding it.
#
# Do not re-quarantine these without a mechanism. The lesson this issue actually
# taught is that a quarantine sized for a flake silently absorbed a hard
# regression -- widening the mcp bound made both fail deterministically, and CI
# would have gone green over a broken deep probe. If they flake again, they are
# telling the truth about something.
#
# Local green is not evidence for this pair: they passed 10/10 on mcp 1.23.3 and
# 10/10 on 2.0.0 while CI was failing ~38%. Sample with `workflow_dispatch`.


def test_mcp_deep_probe_real_server_ignores_project_import_shadows(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    sentinel = tmp_path / "executed.txt"
    shadow = tmp_path / "graphite"
    shadow.mkdir()
    (shadow / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('package')\n",
        encoding="utf-8",
    )
    (shadow / "mcp.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('mcp')\n",
        encoding="utf-8",
    )
    for customization in ("sitecustomize.py", "usercustomize.py"):
        (tmp_path / customization).write_text(
            f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('customize')\n",
            encoding="utf-8",
        )

    check = probes.probe_mcp(tmp_path, timeout_seconds=20)

    assert check.status == "ready"
    assert not sentinel.exists()


def test_mcp_deep_probe_real_server_supports_trusted_source_inside_selected_repo() -> None:
    """Launches a real MCP server, so it inherits a real precondition.

    The manifest builder refuses any package-metadata root that lives inside
    the selected repository and actually contains distributions -- deliberately,
    since the selected repo is untrusted and must not be able to inject
    distributions into the probe. `trusted_source` itself is exempt, which is
    the property this test is named for.

    A virtual environment created *inside* a clone therefore fails this test
    through no fault of the code: its `site-packages` overlaps the selected
    root, the builder exits 70, and `probe_mcp` flattens that to `probe_failed`
    with no stream to read, because `run_bounded_process` raises before
    returning. Measured, and it cost real time to chase: the same commit passes
    from a venv outside the clone and fails from one inside it. Put the
    environment outside the repository before suspecting this test.
    """
    import graphite.doctor_probes as probes

    trusted_source = Path(probes.__file__).resolve(strict=True).parent.parent
    selected_repo = trusted_source.parent

    check = probes.probe_mcp(selected_repo, timeout_seconds=20)

    assert check.status == "ready", check.details
    assert check.details["server_name"] == "graphite"


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

    builder_runner = _bounded_manifest_builder_runner(tmp_path)
    started = time.monotonic()
    check = probes.probe_mcp(
        tmp_path,
        timeout_seconds=0.35,
        _runner=transport.run_bounded_process,
        _builder_script="pass",
        _builder_runner=builder_runner,
    )

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
    builder_runner = _bounded_manifest_builder_runner(tmp_path)
    started = time.monotonic()
    check = probes.probe_mcp(
        tmp_path,
        timeout_seconds=0.35,
        _runner=transport.run_bounded_process,
        _builder_script="pass",
        _builder_runner=builder_runner,
    )
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
    builder_runner = _bounded_manifest_builder_runner(tmp_path)
    started = time.monotonic()
    check = probes.probe_mcp(
        tmp_path,
        timeout_seconds=0.35,
        _runner=transport.run_bounded_process,
        _builder_script="pass",
        _builder_runner=builder_runner,
    )
    assert time.monotonic() - started < 1
    assert check.status == "degraded"
    assert check.details == {"code": "invalid_response"}


def test_typescript_deep_probe_is_optional_when_node_is_missing(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    check = probes.probe_typescript(tmp_path, _node_resolver=lambda root: None)
    assert check.status == "optional"
    assert check.details == {}


def test_typescript_deep_probe_rejects_node_from_selected_root_ancestor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import graphite.doctor_probes as probes

    selected = tmp_path / "repo"
    selected.mkdir()
    node = tmp_path / ("node.exe" if os.name == "nt" else "node")
    node.write_bytes(b"")
    if os.name != "nt":
        node.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert probes._resolve_external_node(selected) is None


def test_typescript_deep_probe_gives_exact_activation_guidance_when_module_is_missing(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    check = probes.probe_typescript(
        tmp_path,
        _node_resolver=lambda root: Path("NODE"),
        _runner=lambda *a, **k: probes.ProbeProcessResult(0, b'{"missing_module":"typescript"}', b"", 0.01),
    )
    assert check.status == "optional"
    # Exact-tuple guard kept deliberately -- the point of this test is that the
    # guidance is EXACT, not merely present. Updated, not relaxed: the first
    # step used to name one developer's `.codex_state` directory, which is a
    # dead path on every other machine and shipped that username in the wheel.
    assert check.remediation == (
        "Confirm the package resolves from the target project: "
        "node -e \"require.resolve('typescript')\"",
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


def test_typescript_deep_probe_detects_module_without_loading_it(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    captured: dict[str, object] = {}
    def run(argv: list[str], **kwargs: object) -> object:
        captured.update(argv=argv, **kwargs)
        return probes.ProbeProcessResult(0, b'{"detected":true}', b"", 0.01)

    check = probes.probe_typescript(
        tmp_path,
        timeout_seconds=3,
        _node_resolver=lambda root: Path("NODE"),
        _runner=run,
    )
    assert check.status == "optional"
    assert check.details == {}
    assert check.summary == (
        "TypeScript was detected but intentionally not executed because project dependencies "
        "are outside the doctor trust boundary."
    )
    assert captured["argv"][0:2] == ["NODE", "-e"]
    assert "require.resolve('typescript')" in captured["argv"][2]
    assert "require('typescript')" not in captured["argv"][2]
    assert "import('typescript')" not in captured["argv"][2]
    assert captured["cwd"] == tmp_path
    assert captured["timeout_seconds"] == 3


@pytest.mark.parametrize("result", [b"not-json", b'{}', b'{"detected":1}', b'{"detected":true,"path":"secret"}'])
def test_typescript_deep_probe_invalid_result_is_degraded(tmp_path: Path, result: bytes) -> None:
    import graphite.doctor_probes as probes

    check = probes.probe_typescript(
        tmp_path,
        _node_resolver=lambda root: Path("NODE"),
        _runner=lambda *a, **k: probes.ProbeProcessResult(0, result, b"", 0.01),
    )
    assert check.status == "degraded"


def test_typescript_deep_probe_real_fake_package_is_never_executed(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    if probes._resolve_external_node(tmp_path) is None:
        pytest.skip("external Node is unavailable")
    sentinel = tmp_path / "typescript-executed.txt"
    package = tmp_path / "node_modules" / "typescript"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"name":"typescript","main":"index.js"}', encoding="utf-8")
    (package / "index.js").write_text(
        f"require('fs').writeFileSync({str(sentinel)!r}, 'executed'); module.exports={{}};",
        encoding="utf-8",
    )

    check = probes.probe_typescript(tmp_path, timeout_seconds=5)

    assert check.status == "optional"
    assert "node_modules" not in check.summary
    assert not sentinel.exists()


def test_run_deep_probes_contains_each_probe_failure_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import graphite.doctor_probes as probes

    observed: list[str] = []

    def fail_core(root: Path) -> DoctorCheck:
        del root
        observed.append("core")
        raise MemoryError("sensitive")

    def fail_mcp(root: Path) -> DoctorCheck:
        del root
        observed.append("mcp")
        raise RecursionError("sensitive")

    def typescript(root: Path) -> DoctorCheck:
        del root
        observed.append("typescript")
        return DoctorCheck("deep_typescript", "TypeScript", "optional", "ok")

    def fail_llm(cfg: Config) -> DoctorCheck:
        del cfg
        observed.append("llm")
        raise OSError("sensitive provider path")

    monkeypatch.setattr(probes, "probe_core_pipeline", fail_core)
    monkeypatch.setattr(probes, "probe_mcp", fail_mcp)
    monkeypatch.setattr(probes, "probe_typescript", typescript)
    monkeypatch.setattr(probes, "probe_llm", fail_llm)

    checks = probes.run_deep_probes(
        tmp_path,
        cfg=Config(llm_mode="cloud"),
        include_llm=True,
    )

    assert observed == ["core", "mcp", "typescript", "llm"]
    assert [check.status for check in checks] == ["blocked", "degraded", "optional", "degraded"]
    assert all("sensitive" not in check.summary for check in checks)


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


def _write_dist_info(root: Path, name: str, version: str, *requires: str) -> None:
    """Write a minimal installed-distribution record for closure tests."""
    dist_info = root / f"{name}-{version}.dist-info"
    dist_info.mkdir()
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    lines.extend(f"Requires-Dist: {requirement}" for requirement in requires)
    (dist_info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_extra_requested_by_a_dependent_enters_the_distribution_closure(
    tmp_path: Path,
) -> None:
    """mcp 2.x needs `pyjwt[crypto]`; the extra's own deps must be bootstrappable.

    The closure feeds an isolated `-I -S` subprocess, so a distribution missing
    here is an ImportError at probe time, not a cosmetic omission.
    """
    import graphite.doctor_probes as probes

    root = tmp_path / "site-packages"
    root.mkdir()
    _write_dist_info(root, "mcp", "2.0.0", "pyjwt[crypto]>=2.9.0")
    _write_dist_info(root, "networkx", "3.0")
    _write_dist_info(root, "pyjwt", "2.9.0", 'cryptography>=3.4.0; extra == "crypto"')
    _write_dist_info(root, "cryptography", "42.0.0")

    closure = probes._mcp_distribution_closure((root,))

    assert "cryptography" in closure


def test_extra_no_dependent_requested_stays_out_of_the_closure(tmp_path: Path) -> None:
    """The fail-closed boundary: an extra nobody asked for grants no reachability.

    `_requirement_applies` used to reject every `extra` marker outright. Now that
    it evaluates them, an unrequested extra must still contribute nothing --
    otherwise the closure would bootstrap code the dependency graph never asked
    for into the isolated probe subprocess.
    """
    import graphite.doctor_probes as probes

    root = tmp_path / "site-packages"
    root.mkdir()
    # mcp requests NO extras of pyjwt.
    _write_dist_info(root, "mcp", "2.0.0", "pyjwt>=2.9.0")
    _write_dist_info(root, "networkx", "3.0")
    _write_dist_info(root, "pyjwt", "2.9.0", 'unrequested>=1.0; extra == "crypto"')
    _write_dist_info(root, "unrequested", "1.0")

    closure = probes._mcp_distribution_closure((root,))

    assert "pyjwt" in closure
    assert "unrequested" not in closure


def test_parenthesised_condition_beside_an_extra_marker_is_evaluated(
    tmp_path: Path,
) -> None:
    """Real metadata combines a parenthesised comparison with an `extra` marker.

    While every `extra` marker was rejected outright these never reached the
    comparison parser. Now that they do, a wrapped sub-condition must parse --
    a ValueError here aborts the whole closure, not just one requirement.
    """
    import graphite.doctor_probes as probes

    root = tmp_path / "site-packages"
    root.mkdir()
    _write_dist_info(root, "mcp", "2.0.0", "pyjwt[crypto]>=2.9.0")
    _write_dist_info(root, "networkx", "3.0")
    _write_dist_info(
        root,
        "pyjwt",
        "2.9.0",
        f'cryptography>=3.4.0; (sys_platform == "{sys.platform}") and extra == "crypto"',
    )
    _write_dist_info(root, "cryptography", "42.0.0")

    closure = probes._mcp_distribution_closure((root,))

    assert "cryptography" in closure


def test_unparseable_extra_marker_skips_the_requirement_without_aborting(
    tmp_path: Path,
) -> None:
    """A nested marker must not take the whole closure down with it.

    Real metadata contains markers like `(a and b) or extra == "x"`, which this
    deliberately simple parser cannot evaluate. Before extras were understood,
    every such marker was rejected outright and the walk continued. That must
    remain true: skip the requirement, do not raise.
    """
    import graphite.doctor_probes as probes

    root = tmp_path / "site-packages"
    root.mkdir()
    _write_dist_info(root, "mcp", "2.0.0", "pyjwt[crypto]>=2.9.0")
    _write_dist_info(root, "networkx", "3.0")
    _write_dist_info(
        root,
        "pyjwt",
        "2.9.0",
        'nested>=1.0; (sys_platform != "nonesuch" and python_version < "9.9")'
        ' or extra == "crypto"',
    )
    _write_dist_info(root, "nested", "1.0")

    closure = probes._mcp_distribution_closure((root,))

    assert "pyjwt" in closure
    # Unevaluable -> contributes nothing, exactly as the blanket rejection did.
    assert "nested" not in closure


def test_python_full_version_marker_is_evaluated(tmp_path: Path) -> None:
    """`cryptography` gates cffi on `python_full_version`; the closure walks it now."""
    import graphite.doctor_probes as probes

    root = tmp_path / "site-packages"
    root.mkdir()
    _write_dist_info(root, "mcp", "2.0.0", "pyjwt>=2.9.0")
    _write_dist_info(root, "networkx", "3.0")
    _write_dist_info(root, "pyjwt", "2.9.0", 'cffi>=2.0.0; python_full_version >= "3.9"')
    _write_dist_info(root, "cffi", "2.0.0")

    closure = probes._mcp_distribution_closure((root,))

    assert "cffi" in closure


def test_python_full_version_star_match_is_evaluated(tmp_path: Path) -> None:
    """A PEP 440 `3.8.*` wildcard must compare by prefix, not crash on int('*')."""
    import graphite.doctor_probes as probes

    root = tmp_path / "site-packages"
    root.mkdir()
    _write_dist_info(root, "mcp", "2.0.0", "pyjwt>=2.9.0")
    _write_dist_info(root, "networkx", "3.0")
    _write_dist_info(root, "pyjwt", "2.9.0", 'legacy>=1.0; python_full_version == "3.8.*"')
    _write_dist_info(root, "legacy", "1.0")

    closure = probes._mcp_distribution_closure((root,))

    assert sys.version_info[:2] != (3, 8), "test assumes it is not run on 3.8"
    assert "legacy" not in closure


def test_implementation_name_marker_is_evaluated(tmp_path: Path) -> None:
    """`cffi` requires `pycparser; implementation_name != "PyPy"`.

    On CPython that marker is true, so the dependency is genuinely needed --
    skipping an unevaluable marker here would omit it and break the probe.
    """
    import graphite.doctor_probes as probes

    root = tmp_path / "site-packages"
    root.mkdir()
    _write_dist_info(root, "mcp", "2.0.0", "cffi>=1.0")
    _write_dist_info(root, "networkx", "3.0")
    _write_dist_info(root, "cffi", "1.17.0", 'pycparser; implementation_name != "PyPy"')
    _write_dist_info(root, "pycparser", "2.22")

    closure = probes._mcp_distribution_closure((root,))

    assert sys.implementation.name == "cpython", "test assumes CPython"
    assert "pycparser" in closure


def test_prerelease_python_full_version_does_not_abort_the_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`python_full_version` is `3.14.0rc1` on a pre-release interpreter.

    That component is not an int. Raising here would propagate out of a
    non-extra marker and abort the whole distribution walk, so the probe would
    die on any interpreter Python ships as a release candidate.
    """
    import graphite.doctor_probes as probes

    monkeypatch.setattr(probes.platform, "python_version", lambda: "3.14.0rc1")

    assert probes._requirement_applies("dep>=1; python_full_version >= '3.9'") is True
    assert probes._requirement_applies("dep>=1; python_full_version < '3.9'") is False


def test_probe_stdin_length_prefixes_the_manifest(tmp_path: Path) -> None:
    """The manifest must be framed by length, not by a trailing newline.

    A newline frame forces the child to find the boundary with
    `readline()`, which over-reads the protocol bytes into a private buffer
    that the MCP server's own fd-0 reader can never see. A length prefix lets
    the child read exactly the manifest off the raw stream and leave the
    protocol input in the pipe.
    """
    import graphite.doctor_probes as probes

    captured: dict[str, bytes] = {}

    def runner(command: object, **kwargs: object) -> object:
        captured["stdin"] = kwargs["stdin"]  # type: ignore[assignment]
        return probes.ProbeProcessResult(0, b"", b"", 0.01)

    probes.probe_mcp(tmp_path, _runner=runner)

    header, separator, rest = captured["stdin"].partition(b"\n")
    assert separator == b"\n"
    assert header.isdigit(), f"manifest is not length-prefixed: {header[:60]!r}"
    length = int(header)
    manifest = json.loads(rest[:length])
    assert set(manifest) == {"distributions", "files", "packages"}
    # Everything after the manifest is protocol input, byte-addressable
    # without any scanning the child would have to buffer for.
    assert rest[length:].startswith(b'{"jsonrpc"')


def test_manifest_build_time_does_not_consume_the_server_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A slow manifest build and an unresponsive child are different faults.

    They shared one deadline, so a cold build (~8.7s over 1758 files) ate most
    of the 20s budget and the probe reported `timeout` before ever starting a
    child -- graphite#39. Each phase now gets its own allowance.
    """
    import time as _time

    import graphite.doctor_probes as probes

    # The fake clock must ADVANCE, and this is not a style preference.
    #
    # `probes.time` IS the stdlib `time` module, so patching an attribute on it
    # replaces `time.monotonic` for EVERY module in the process, not just this
    # one. A clock frozen at a constant therefore reaches
    # `probe_process._terminate_process_tree`, whose POSIX grace loop computes
    # `remaining = grace_deadline - time.monotonic()`. Against a frozen clock
    # that value is permanently 0.1, the `remaining <= 0` exit can never be
    # reached, and cleanup spins forever on a real `time.sleep`.
    #
    # That is graphite#45 in full: the suite hung on Linux and every ubuntu leg
    # was killed at the 45-minute CI timeout. It cannot happen on Windows,
    # because `_terminate_process_tree` guards that whole branch with
    # `os.name != "nt"` -- which is exactly why it survived a Windows-only gate.
    #
    # Offsetting a real clock keeps the determinism this test needs (the jump is
    # still exactly 15.0) while leaving `time.monotonic` monotonic, which is the
    # contract every other module is entitled to rely on. Production code is NOT
    # the right place to defend against a clock that does not advance.
    real_monotonic = _time.monotonic
    clock = {"offset": 0.0}
    monkeypatch.setattr(probes.time, "monotonic", lambda: real_monotonic() + clock["offset"])

    original = probes._build_mcp_manifest_bounded

    def slow_build(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)
        clock["offset"] += 15.0  # the build burns 15 of the 20 seconds
        return result

    monkeypatch.setattr(probes, "_build_mcp_manifest_bounded", slow_build)

    captured: dict[str, object] = {}

    def runner(command: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return probes.ProbeProcessResult(0, b"", b"", 0.01)

    probes.probe_mcp(tmp_path, timeout_seconds=20.0, _runner=runner)

    assert captured["timeout_seconds"] == pytest.approx(20.0)


def test_manifest_build_does_not_resolve_far_more_paths_than_it_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Path resolution is the dominant cost of the build (graphite#39).

    On Windows every `resolve()` is a `_getfinalpathname` syscall. The top-level
    package directory was resolved once per *declared file*, so a package with
    500 files resolved its own root 500 times. Resolving a couple of paths per
    recorded entry is inherent; resolving five times as many is waste.
    """
    import graphite.doctor_probes as probes

    calls = {"n": 0}
    original = Path.resolve

    def counting(self: Path, *args: object, **kwargs: object) -> Path:
        calls["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counting)
    probes._mcp_import_inventory.cache_clear()
    try:
        manifest = probes._mcp_import_manifest(tmp_path)
    finally:
        probes._mcp_import_inventory.cache_clear()

    entries = sum(len(group["entries"]) for group in manifest["files"])
    assert entries > 0
    # A few resolves per recorded entry are inherent: one to bind the path, and
    # one when the parent re-validates the manifest the builder subprocess
    # produced -- that second pass is a security check against the child's
    # claims, not waste. Resolving the same top-level directory once per
    # declared file was waste, and put this ratio at 5.5.
    assert calls["n"] < entries * 4, (
        f"{calls['n']} resolve() calls for {entries} recorded entries"
    )


def test_deep_bounded_runner_reports_how_long_the_child_outlived_stdin_close(
    tmp_path: Path,
) -> None:
    """#29 evidence: does the child die *because* stdin closed?

    `write_input` closes stdin the instant the payload is written. The observed
    failure is a child that answers `initialize`, never answers `tools/list`,
    and exits cleanly -- consistent with EOF being read as end-of-session and
    teardown beating the messages already buffered. Distinguishing that from an
    unrelated early exit needs the interval between the close and the exit, so
    the runner has to report it.
    """
    from graphite.probe_process import run_bounded_process

    result = run_bounded_process(
        [sys.executable, "-c", "import sys, time; sys.stdin.buffer.read(); time.sleep(0.5)"],
        cwd=tmp_path,
        stdin=b"payload\n",
        timeout_seconds=20,
        check=False,
    )

    assert result.returncode == 0
    # The child deliberately outlives the close by ~0.5s.
    assert result.stdin_close_to_exit_seconds >= 0.25


def test_deep_bounded_runner_reports_a_prompt_exit_after_stdin_close(
    tmp_path: Path,
) -> None:
    """The positive case alone would pass against a hardcoded large value.

    A child that exits as soon as it sees EOF must report a small interval, or
    the field cannot discriminate the #29 shape from a slow shutdown.
    """
    from graphite.probe_process import run_bounded_process

    result = run_bounded_process(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        cwd=tmp_path,
        stdin=b"payload\n",
        timeout_seconds=20,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdin_close_to_exit_seconds < 0.25
