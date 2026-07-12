"""Typed, bounded system-readiness diagnostics."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Literal, Mapping

from .config import Config, default_projects_root
from .daemon_health import HealthOptions, evaluate_daemon_health
from .freshness import check_graph_freshness
from .git import GitError, GitRunner
from .validation import validate_graph_bundle

DoctorStatus = Literal["ready", "optional", "degraded", "blocked"]
STATUSES: tuple[DoctorStatus, ...] = ("ready", "optional", "degraded", "blocked")
_RANK = {name: rank for rank, name in enumerate(STATUSES)}
_ARTIFACT_LIMIT = 128 * 1024 * 1024
_TEXT_LIMIT = 500


@dataclass(frozen=True)
class DoctorCheck:
    code: str
    label: str
    status: DoctorStatus
    summary: str
    details: Mapping[str, Any] = field(default_factory=dict)
    remediation: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in _RANK:
            raise ValueError("invalid doctor check status")
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
        object.__setattr__(self, "remediation", tuple(self.remediation))

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "label": self.label, "status": self.status, "summary": self.summary, "details": dict(self.details), "remediation": list(self.remediation)}


def build_report(root: Path, checks: Iterable[DoctorCheck], deep: bool, llm_included: bool) -> dict[str, Any]:
    ordered = sorted(checks, key=lambda check: check.code)
    if any(check.status not in _RANK for check in ordered):
        raise ValueError("invalid doctor check status")
    status = max((check.status for check in ordered), key=_RANK.get, default="ready")
    return {"schema_version": 1, "root": root.resolve().name, "deep": bool(deep), "llm_included": bool(llm_included), "status": status, "exit_code": 1 if status == "blocked" else 0, "checks": [check.to_dict() for check in ordered]}


def format_doctor_text(report: Mapping[str, Any]) -> str:
    lines = [f"[graphite] doctor: {report.get('status', 'ready')}"]
    for item in report.get("checks", []):
        lines.append(f"  [{item['status']}] {item['label']}: {item['summary']}")
        for remediation in item.get("remediation", []):
            lines.append(f"    - {remediation}")
    return "\n".join(lines) + "\n"


def check_python() -> DoctorCheck:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ready = sys.version_info >= (3, 11)
    return DoctorCheck("python", "Python", "ready" if ready else "blocked", f"Python {version} is {'supported' if ready else 'unsupported'}.", {"version": version}, (() if ready else ("Use Python 3.11 or newer.",)))


def check_git(root: Path) -> DoctorCheck:
    try:
        result = GitRunner(root).run(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], timeout_seconds=10.0, max_stdout_bytes=16 * 1024 * 1024)
        if result.returncode != 0:
            raise GitError("Git command failed")
        if result.stdout and not result.stdout.endswith(b"\0"):
            raise GitError("Git returned malformed output")
        count = result.stdout.count(b"\0")
        return DoctorCheck("git", "Git", "ready", "Git repository inventory is readable.", {"record_count": count})
    except (GitError, OSError):
        return DoctorCheck("git", "Git", "blocked", "Git repository inventory is unavailable.", remediation=("Verify Git 2.38+ and repository access.",))


def _scoped_output(root: Path, cfg: Config) -> Path:
    return cfg.output_dir if cfg.output_dir.is_absolute() else root / cfg.output_dir


def _artifact_size(path: Path) -> int:
    return path.stat().st_size


def _read_json_bounded(path: Path, limit: int) -> Any:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("artifact too large")
    return json.loads(data.decode("utf-8"))


def check_graph(root: Path, cfg: Config) -> DoctorCheck:
    graph_path = _scoped_output(root, cfg) / "graph.json"
    if not graph_path.exists():
        return DoctorCheck("graph", "Graph", "degraded", "Graph artifact is missing.", remediation=("Run `graphite build .`.",))
    try:
        if _artifact_size(graph_path) > _ARTIFACT_LIMIT:
            return DoctorCheck("graph", "Graph", "blocked", "Graph artifact exceeds the 128 MiB diagnostic limit.", remediation=("Rebuild or reduce the graph artifact.",))
        bundle = _read_json_bounded(graph_path, _ARTIFACT_LIMIT)
        if not isinstance(bundle, dict):
            raise ValueError("invalid graph")
        validation = validate_graph_bundle(bundle)
        if not validation.get("ok"):
            return DoctorCheck("graph", "Graph", "blocked", "Graph artifact validation failed.", {"node_count": validation.get("node_count", 0), "edge_count": validation.get("edge_count", 0), "warning_count": validation.get("warning_count", 0), "stale": False}, ("Rebuild the graph artifact.",))
        scoped = Config(**{**cfg.to_dict(), "output_dir": _scoped_output(root, cfg)})
        freshness = check_graph_freshness(root, scoped)
        stale = bool(freshness.get("stale"))
        details = {"node_count": validation.get("node_count", 0), "edge_count": validation.get("edge_count", 0), "warning_count": validation.get("warning_count", 0), "stale": stale}
        return DoctorCheck("graph", "Graph", "degraded" if stale else "ready", "Graph artifact is stale." if stale else "Graph artifact is valid and fresh.", details, (("Run `graphite build .`.",) if stale else ()))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return DoctorCheck("graph", "Graph", "blocked", "Graph artifact is unreadable or malformed.", remediation=("Rebuild the graph artifact.",))


def check_daemon(base: Path) -> DoctorCheck:
    try:
        report = evaluate_daemon_health(base, options=HealthOptions())
        errors = len(report.get("errors", []))
        warnings = len(report.get("warnings", []))
        status_found = report.get("daemon_status") is not None
        details = {"status_found": status_found, "error_count": errors, "warning_count": warnings, "process_checked": bool(report.get("process", {}).get("checked")), "startup_checked": bool(report.get("startup", {}).get("checked"))}
        if not status_found and any(item.get("code") == "status_missing" for item in report.get("errors", [])):
            return DoctorCheck("daemon", "Daemon", "optional", "Daemon status is not present; core operation is unaffected.", details)
        degraded = errors > 0 or warnings > 0 or not report.get("ok", False)
        return DoctorCheck("daemon", "Daemon", "degraded" if degraded else "ready", "Daemon health needs attention." if degraded else "Daemon health is clean.", details)
    except Exception:
        return DoctorCheck("daemon", "Daemon", "degraded", "Daemon health could not be evaluated.")


def check_mcp() -> DoctorCheck:
    package = importlib.util.find_spec("mcp") is not None
    command = shutil.which("graphite-mcp") is not None
    details = {"python_package": package, "command": command}
    if package and command:
        return DoctorCheck("mcp", "MCP", "ready", "MCP integration is available.", details)
    return DoctorCheck("mcp", "MCP", "optional", "MCP integration is not fully activated.", details, ("Install the Graphite MCP extra and ensure `graphite-mcp` is on PATH if MCP is needed.",))


def check_typescript(root: Path, *, timeout_seconds: float = 5.0) -> DoctorCheck:
    if not shutil.which("node"):
        return DoctorCheck("typescript", "TypeScript", "optional", "Node.js is unavailable; TypeScript compiler resolution is optional.")
    script = "try{const p=require('typescript/package.json');process.stdout.write(JSON.stringify({ok:true,version:p.version}))}catch(e){process.stdout.write(JSON.stringify({ok:false,reason:'missing'}))}"
    try:
        returncode, stdout, timed_out = _run_node_probe(root, script, timeout_seconds=timeout_seconds)
        if timed_out:
            return DoctorCheck("typescript", "TypeScript", "degraded", "TypeScript probe timed out.")
        if returncode != 0:
            return DoctorCheck("typescript", "TypeScript", "degraded", "TypeScript probe failed unexpectedly.")
        data = json.loads(stdout.decode("utf-8"))
        if data.get("ok") is True and isinstance(data.get("version"), str):
            return DoctorCheck("typescript", "TypeScript", "ready", "TypeScript compiler is available.", {"version": data["version"][:64]})
        if data.get("reason") == "missing":
            return DoctorCheck("typescript", "TypeScript", "optional", "TypeScript compiler module is unavailable.", remediation=("Add TypeScript to the selected repository if compiler resolution is needed.",))
        return DoctorCheck("typescript", "TypeScript", "degraded", "TypeScript probe returned an invalid response.")
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        return DoctorCheck("typescript", "TypeScript", "degraded", "TypeScript probe returned an invalid response.")


def _run_node_probe(root: Path, script: str, *, timeout_seconds: float) -> tuple[int, bytes, bool]:
    process = subprocess.Popen(
        ["node", "-e", script],
        cwd=root,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    output: list[bytes] = []

    def read_bounded() -> None:
        if process.stdout is not None:
            output.append(process.stdout.read(_TEXT_LIMIT + 1))

    reader = threading.Thread(target=read_bounded, daemon=True)
    reader.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        reader.join(timeout=1.0)
        return process.returncode or -1, b"", True
    reader.join(timeout=1.0)
    data = output[0] if output else b""
    if reader.is_alive() or len(data) > _TEXT_LIMIT:
        process.kill()
        return returncode, b"", False
    return returncode, data, False


def check_llm_config(cfg: Config) -> DoctorCheck:
    mode = cfg.llm_mode.strip().lower()
    provider = cfg.llm_provider.strip().lower().replace("_", "-")
    credential = bool(cfg.llm_api_key)
    details = {"mode": mode, "provider": provider, "credential_present": credential}
    if mode == "none":
        summary = "LLM enrichment is disabled. An ambient credential is unused; rotate or remove it." if credential else "LLM enrichment is disabled."
        return DoctorCheck("llm", "LLM", "optional", summary, details)
    return DoctorCheck("llm", "LLM", "degraded", "LLM is configured but requires a deep connectivity test.", details, ("Run doctor with deep LLM checks when network access is approved.",))


def _fast_checks(root: Path, cfg: Config, daemon_base: Path | None) -> list[DoctorCheck]:
    checks: list[tuple[str, str, Callable[[], DoctorCheck]]] = [("python", "Python", check_python), ("git", "Git", lambda: check_git(root)), ("graph", "Graph", lambda: check_graph(root, cfg)), ("daemon", "Daemon", lambda: check_daemon(daemon_base or root)), ("mcp", "MCP", check_mcp), ("typescript", "TypeScript", lambda: check_typescript(root)), ("llm", "LLM", lambda: check_llm_config(cfg))]
    results = []
    for code, label, check in checks:
        try:
            results.append(check())
        except Exception:
            results.append(DoctorCheck(code, label, "degraded", "The readiness check failed safely."))
    return results


def run_doctor(root: Path, cfg: Config, daemon_base: Path | None = None, deep: bool = False, include_llm: bool = False, deep_runner: Callable[[Path, Config, bool], Iterable[DoctorCheck]] | None = None) -> dict[str, Any]:
    selected = root.resolve()
    selected_daemon_base = (daemon_base or default_projects_root()).resolve()
    checks = _fast_checks(selected, cfg, selected_daemon_base)
    if deep:
        if deep_runner is None:
            from .doctor_probes import run_deep_probes
            deep_runner = run_deep_probes
        checks.extend(deep_runner(selected, cfg=cfg, include_llm=include_llm))
    return build_report(selected, checks, deep, bool(deep and include_llm))
