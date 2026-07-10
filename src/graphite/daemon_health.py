"""Operational health checks for the Graphite multi-project daemon."""
from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .daemon import read_daemon_status
from .windows_task import DEFAULT_TASK_NAME
from .windows_startup import startup_status


@dataclass(frozen=True)
class HealthOptions:
    max_status_age_seconds: float = 180.0
    max_project_success_age_seconds: float = 86_400.0
    require_process: bool = True
    require_startup: bool = True
    startup_name: str = DEFAULT_TASK_NAME

    def validate(self) -> None:
        if self.max_status_age_seconds <= 0:
            raise ValueError("max status age must be greater than zero")
        if self.max_project_success_age_seconds <= 0:
            raise ValueError("max project success age must be greater than zero")


ProcessChecker = Callable[[Path], dict[str, Any]]
StartupChecker = Callable[[Path, str], dict[str, Any]]


def evaluate_daemon_health(
    base: Path,
    *,
    state_dir: Path | None = None,
    options: HealthOptions | None = None,
    now: datetime | None = None,
    process_checker: ProcessChecker | None = None,
    startup_checker: StartupChecker | None = None,
) -> dict[str, Any]:
    """Evaluate daemon health from status artifacts and local runtime signals."""
    opts = options or HealthOptions()
    opts.validate()
    base = base.resolve()
    current_time = now or datetime.now(timezone.utc)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    status_path = (state_dir or (base / ".graphite-daemon")) / "status.json"

    try:
        status = read_daemon_status(base, state_dir)
    except FileNotFoundError:
        return _finalize(
            base,
            status_path,
            current_time,
            status=None,
            status_age_seconds=None,
            errors=[{"code": "status_missing", "message": f"daemon status not found: {status_path}"}],
            warnings=[],
            process={"checked": False},
            startup={"checked": False},
            project_health={"failing": [], "pending": [], "not_built_recently": []},
        )
    except (OSError, json.JSONDecodeError) as exc:
        return _finalize(
            base,
            status_path,
            current_time,
            status=None,
            status_age_seconds=None,
            errors=[{"code": "status_unreadable", "message": str(exc)}],
            warnings=[],
            process={"checked": False},
            startup={"checked": False},
            project_health={"failing": [], "pending": [], "not_built_recently": []},
        )

    updated_at = _parse_time(str(status.get("updated_at", "")))
    status_age = None
    if updated_at is None:
        errors.append({"code": "status_updated_at_invalid", "message": "status updated_at is missing or invalid"})
    else:
        status_age = max(0.0, (current_time - updated_at).total_seconds())
        if status_age > opts.max_status_age_seconds:
            errors.append({
                "code": "status_stale",
                "message": f"daemon status is {status_age:.1f}s old",
                "age_seconds": round(status_age, 3),
                "max_age_seconds": opts.max_status_age_seconds,
            })

    if status.get("status") not in ("ok", None):
        errors.append({"code": "daemon_degraded", "message": f"daemon reported status {status.get('status')}"})

    project_health = _project_health(status, current_time, opts.max_project_success_age_seconds)
    for project in project_health["failing"]:
        errors.append({
            "code": "project_failing",
            "message": f"project build failing: {project['root']}",
            "project": project,
        })
    for project in project_health["pending"]:
        warnings.append({
            "code": "project_pending_initial_build",
            "message": f"project has not completed initial build: {project['root']}",
            "project": project,
        })
    for project in project_health["not_built_recently"]:
        warnings.append({
            "code": "project_not_built_recently",
            "message": f"project not built recently: {project['root']}",
            "project": project,
        })

    process = {"checked": False}
    if opts.require_process:
        checker = process_checker or check_daemon_process
        process = checker(base)
        if not process.get("running"):
            errors.append({"code": "daemon_process_not_running", "message": "Graphite daemon process is not running"})

    startup = {"checked": False}
    if opts.require_startup:
        checker = startup_checker or check_startup_launcher
        startup = checker(base, opts.startup_name)
        if startup.get("supported") is False:
            warnings.append({"code": "startup_check_unsupported", "message": str(startup.get("error", "startup check unsupported"))})
        elif not startup.get("installed"):
            warnings.append({"code": "startup_not_installed", "message": "Graphite startup launcher is not installed"})

    return _finalize(
        base,
        status_path,
        current_time,
        status=status,
        status_age_seconds=status_age,
        errors=errors,
        warnings=warnings,
        process=process,
        startup=startup,
        project_health=project_health,
    )


def check_daemon_process(base: Path) -> dict[str, Any]:
    """Check whether a daemon process for the base path is running."""
    if platform.system().lower() != "windows":
        return {"checked": True, "supported": False, "running": None, "processes": [], "error": "process check currently supports Windows"}
    marker = str(base.resolve())
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -like '*graphite*daemon*' "
        "-and $_.CommandLine -like '*" + marker.replace("'", "''") + "*' "
        "-and $_.CommandLine -notlike '*daemon-status*' "
        "-and $_.CommandLine -notlike '*daemon-health*' "
        "-and $_.CommandLine -notlike '*daemon-install*' "
        "-and $_.CommandLine -notlike '*daemon-uninstall*' } | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"checked": True, "supported": True, "running": False, "processes": [], "error": str(exc)}
    if result.returncode != 0:
        return {"checked": True, "supported": True, "running": False, "processes": [], "error": result.stderr.strip() or result.stdout.strip()}
    stdout = result.stdout.strip()
    if not stdout:
        processes: list[dict[str, Any]] = []
    else:
        try:
            parsed = json.loads(stdout)
            processes = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            processes = []
    return {"checked": True, "supported": True, "running": bool(processes), "process_count": len(processes), "processes": processes}


def check_startup_launcher(base: Path, name: str) -> dict[str, Any]:
    if platform.system().lower() != "windows":
        return {"checked": True, "supported": False, "installed": None, "error": "startup check only applies on Windows"}
    try:
        status = startup_status(base, name=name)
    except Exception as exc:
        return {"checked": True, "supported": True, "installed": False, "error": str(exc)}
    return {"checked": True, "supported": True, **status}


def format_health_text(report: dict[str, Any]) -> str:
    lines = [
        f"[graphite] daemon health: {report['status']} ({'ok' if report['ok'] else 'attention required'})",
        f"  base: {report['base_path']}",
        f"  status file: {report['status_path']}",
    ]
    if report.get("status_age_seconds") is not None:
        lines.append(f"  status age: {report['status_age_seconds']:.1f}s")
    summary = report["summary"]
    lines.append(
        "  projects: "
        f"{summary['project_count']} total, {summary['failing_count']} failing, "
        f"{summary['pending_count']} pending, {summary['not_built_recently_count']} not built recently"
    )
    process = report.get("process", {})
    if process.get("checked"):
        lines.append(f"  process running: {process.get('running')} ({process.get('process_count', 0)} processes)")
    startup = report.get("startup", {})
    if startup.get("checked"):
        lines.append(f"  startup installed: {startup.get('installed')}")
    if report["errors"]:
        lines.append("Errors:")
        for issue in report["errors"][:20]:
            lines.append(f"  - {issue['code']}: {issue['message']}")
    if report["warnings"]:
        lines.append("Warnings:")
        for issue in report["warnings"][:20]:
            lines.append(f"  - {issue['code']}: {issue['message']}")
    return "\n".join(lines) + "\n"


def _project_health(status: dict[str, Any], now: datetime, max_success_age_seconds: float) -> dict[str, list[dict[str, Any]]]:
    failing: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    old: list[dict[str, Any]] = []
    for item in status.get("projects", []):
        project = _project_summary(item, now)
        if item.get("last_error") or int(item.get("failure_count") or 0) > 0:
            failing.append(project)
        if item.get("needs_initial_build") or int(item.get("build_count") or 0) == 0:
            pending.append(project)
        age = project.get("last_success_age_seconds")
        if age is None or age > max_success_age_seconds:
            old.append(project)
    return {"failing": failing, "pending": pending, "not_built_recently": old}


def _project_summary(item: dict[str, Any], now: datetime) -> dict[str, Any]:
    last_success = _parse_time(str(item.get("last_success_at") or ""))
    age = None if last_success is None else max(0.0, (now - last_success).total_seconds())
    return {
        "root": item.get("root"),
        "file_count": item.get("file_count"),
        "build_count": item.get("build_count"),
        "failure_count": item.get("failure_count"),
        "last_error": item.get("last_error"),
        "last_success_at": item.get("last_success_at"),
        "last_success_age_seconds": None if age is None else round(age, 3),
        "needs_initial_build": item.get("needs_initial_build"),
    }


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _finalize(
    base: Path,
    status_path: Path,
    now: datetime,
    *,
    status: dict[str, Any] | None,
    status_age_seconds: float | None,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    process: dict[str, Any],
    startup: dict[str, Any],
    project_health: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    health_status = "degraded" if errors else "warning" if warnings else "ok"
    raw_project_count = status.get("project_count") if status else 0
    project_count = int(raw_project_count or 0)
    return {
        "ok": not errors,
        "status": health_status,
        "checked_at": now.isoformat(timespec="seconds"),
        "base_path": str(base),
        "status_path": str(status_path),
        "status_age_seconds": None if status_age_seconds is None else round(status_age_seconds, 3),
        "daemon_status": status.get("status") if status else None,
        "summary": {
            "project_count": project_count,
            "failing_count": len(project_health.get("failing", [])),
            "pending_count": len(project_health.get("pending", [])),
            "not_built_recently_count": len(project_health.get("not_built_recently", [])),
            "error_count": len(errors),
            "warning_count": len(warnings),
        },
        "errors": errors,
        "warnings": warnings,
        "process": process,
        "startup": startup,
        "projects": project_health,
    }
