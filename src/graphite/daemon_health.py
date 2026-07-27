"""Operational health checks for the Graphite multi-project daemon."""
from __future__ import annotations

import json
import platform
import subprocess
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .daemon import DaemonStatusInvalidError, DaemonStatusTooLargeError, read_daemon_status
from .routing.lifecycle import LifecycleReasonCode, ProviderLifecycleState
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

_MAX_INPUT_PROJECTS = 1_000
_MAX_CATEGORY_DETAILS = 50
_MAX_ROOT_LENGTH = 512
_MAX_LAST_ERROR_LENGTH = 2_048
_MAX_TIMESTAMP_LENGTH = 128
_MAX_STATUS_LENGTH = 128
_MAX_REPORT_ISSUES = 100
_MAX_TEXT_FIELD_LENGTH = 2_048
_PROVIDER_STATUS_VALUES = frozenset({"observing", "ok", "degraded"})
_PROVIDER_REASON_CODES = frozenset(reason.value for reason in LifecycleReasonCode) | frozenset(
    {
        "lifecycle_invalidation_failed",
        "lifecycle_observation_invalid",
        "lifecycle_persistence_failed",
        "observer_cycle_failed",
        "probe_auth_unhealthy",
        "probe_capability_missing",
        "probe_dns_busy",
        "probe_endpoint_invalid",
        "probe_executable_invalid",
        "probe_failed",
        "probe_http_status",
        "probe_identity_changed",
        "probe_model_unavailable",
        "probe_protocol_invalid",
        "probe_request_invalid",
        "probe_response_too_large",
        "probe_timeout",
        "probe_unavailable",
        "probe_version_invalid",
        "verification_manifest_invalid",
    }
)
_PROVIDER_STATE_CODES = frozenset(state.value for state in ProviderLifecycleState)


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

    try:
        raw_status = read_daemon_status(base, state_dir)
    except FileNotFoundError:
        return _with_incidents(
            _finalize(
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
            ),
            [],
        )
    except DaemonStatusTooLargeError:
        return _with_incidents(
            _finalize(
                base,
                status_path,
                current_time,
                status=None,
                status_age_seconds=None,
                errors=[{
                    "code": "status_too_large",
                    "message": "daemon status exceeds the maximum allowed size",
                }],
                warnings=[],
                process={"checked": False},
                startup={"checked": False},
                project_health={"failing": [], "pending": [], "not_built_recently": []},
            ),
            [],
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DaemonStatusInvalidError):
        return _with_incidents(
            _finalize(
                base,
                status_path,
                current_time,
                status=None,
                status_age_seconds=None,
                errors=[{"code": "status_unreadable", "message": "daemon status is unreadable"}],
                warnings=[],
                process={"checked": False},
                startup={"checked": False},
                project_health={"failing": [], "pending": [], "not_built_recently": []},
            ),
            [],
        )

    status, schema_issues, status_meta = _normalize_status(raw_status)
    errors.extend(schema_issues)

    updated_at_value = status.get("updated_at")
    updated_at = _parse_time(updated_at_value if isinstance(updated_at_value, str) else "")
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

    provider_lifecycle = status.get("provider_lifecycle")
    if isinstance(provider_lifecycle, dict) and (
        provider_lifecycle["status"] == "degraded"
        or provider_lifecycle["failed"] > 0
        or provider_lifecycle["state_counts"].get("unavailable", 0) > 0
    ):
        warnings.append({
            "code": "provider_observation_degraded",
            "message": "one or more provider lifecycle observations require attention",
        })

    project_health, project_counts = _project_health(
        status,
        current_time,
        opts.max_project_success_age_seconds,
    )
    for project in project_health["failing"]:
        _append_bounded(errors, {
            "code": "project_failing",
            "message": f"project build failing: {project['root']}",
            "project": project,
        })
    for project in project_health["pending"]:
        _append_bounded(warnings, {
            "code": "project_pending_initial_build",
            "message": f"project has not completed initial build: {project['root']}",
            "project": project,
        })
    for project in project_health["not_built_recently"]:
        _append_bounded(warnings, {
            "code": "project_not_built_recently",
            "message": f"project not built recently: {project['root']}",
            "project": project,
        })
    # A nested git repo is never discovered, so it can never appear in the two
    # warnings above -- health would otherwise report ok while that repo's
    # graph goes stale indefinitely (#6).
    for root in status.get("unsupervised_nested_repos") or []:
        _append_bounded(warnings, {
            "code": "project_nested_repo_unsupervised",
            "message": (
                f"nested git repo is not supervised and its graph may be stale: {root} "
                "-- build it directly or run the daemon on it"
            ),
            "project": {"root": root},
        })

    process = {"checked": False}
    if opts.require_process:
        checker = process_checker or check_daemon_process
        process = checker(base)
        if process.get("error"):
            warnings.append({
                "code": "daemon_process_check_unavailable",
                "message": "Graphite daemon process observation is unavailable",
            })
        elif not process.get("running"):
            errors.append({"code": "daemon_process_not_running", "message": "Graphite daemon process is not running"})

    startup = {"checked": False}
    if opts.require_startup:
        checker = startup_checker or check_startup_launcher
        startup = checker(base, opts.startup_name)
        if startup.get("supported") is False:
            warnings.append({"code": "startup_check_unsupported", "message": str(startup.get("error", "startup check unsupported"))})
        elif not startup.get("installed"):
            warnings.append({"code": "startup_not_installed", "message": "Graphite startup launcher is not installed"})

    project_roots = [item["root"] for item in status["projects"]]
    return _with_incidents(
        _finalize(
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
            project_counts=project_counts,
            status_meta=status_meta,
        ),
        project_roots,
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


_MAX_GROUP_DETAILS = 5
_MAX_ISSUE_LINES = 20


def _count_phrase(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _health_head_line(report: dict[str, Any]) -> str:
    status = _safe_text(report["status"])
    if report["status"] == "ok":
        return "[graphite] daemon health: ok"
    summary = report.get("summary", {})
    error_count = summary.get("error_count", len(report.get("errors", [])))
    warning_count = summary.get("warning_count", len(report.get("warnings", [])))
    if error_count:
        detail = f"{_count_phrase(error_count, 'error')}, {_count_phrase(warning_count, 'warning')}"
    else:
        detail = f"{_count_phrase(warning_count, 'warning')}, no errors"
    return f"[graphite] daemon health: {status} ({detail})"


def _issue_lines(
    label: str, issues: list[dict[str, Any]], *, cap: int = _MAX_ISSUE_LINES
) -> list[str]:
    shown = issues[:cap]
    dropped = len(issues) - len(shown)
    lines = [f"{label}:"]
    groups: dict[str, list[dict[str, Any]]] = {}
    for issue in shown:
        groups.setdefault(str(issue.get("code")), []).append(issue)
    for code, members in groups.items():
        if len(members) == 1:
            lines.append(f"  - {_safe_text(code)}: {_safe_text(members[0].get('message'))}")
            continue
        lines.append(f"  - {_safe_text(code)} ({len(members)}):")
        for issue in members[:_MAX_GROUP_DETAILS]:
            lines.append(f"      {_safe_text(issue.get('message'))}")
        if len(members) > _MAX_GROUP_DETAILS:
            lines.append(f"      ... {len(members) - _MAX_GROUP_DETAILS} more")
    if dropped > 0:
        lines.append(f"  ... {dropped} more")
    return lines


def format_health_text(report: dict[str, Any]) -> str:
    lines = [
        _health_head_line(report),
        f"  base: {_safe_text(report['base_path'])}",
        f"  status file: {_safe_text(report['status_path'])}",
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
        lines.extend(_issue_lines("Errors", report["errors"]))
    if report["warnings"]:
        lines.extend(_issue_lines("Warnings", report["warnings"]))
    return "\n".join(lines) + "\n"


def _project_health(
    status: dict[str, Any],
    now: datetime,
    max_success_age_seconds: float,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    failing: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    old: list[dict[str, Any]] = []
    counts = {"failing": 0, "pending": 0, "not_built_recently": 0}
    for item in status.get("projects", []):
        project = _project_summary(item, now)
        if item.get("last_error") is not None:
            counts["failing"] += 1
            if len(failing) < _MAX_CATEGORY_DETAILS:
                failing.append(project)
        if item.get("needs_initial_build") or int(item.get("build_count") or 0) == 0:
            counts["pending"] += 1
            if len(pending) < _MAX_CATEGORY_DETAILS:
                pending.append(project)
        age = project.get("last_success_age_seconds")
        if age is None or age > max_success_age_seconds:
            counts["not_built_recently"] += 1
            if len(old) < _MAX_CATEGORY_DETAILS:
                old.append(project)
    return {"failing": failing, "pending": pending, "not_built_recently": old}, counts


_PROJECT_COUNT_FIELDS = ("build_count", "file_count", "failure_count")
_MAX_SCHEMA_ISSUES = 20


def _schema_issue(field: str, index: int | None = None) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "code": "status_schema_invalid",
        "message": "daemon status schema is invalid",
        "field": field,
    }
    if index is not None:
        issue["index"] = index
    return issue


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _bounded_clean_string(value: object, limit: int, *, nullable: bool) -> bool:
    if value is None:
        return nullable
    return (
        isinstance(value, str)
        and len(value) <= limit
        and all(unicodedata.category(character) not in {"Cc", "Cf"} for character in value)
    )


def _append_bounded(target: list[dict[str, Any]], issue: dict[str, Any]) -> None:
    if len(target) < _MAX_REPORT_ISSUES:
        target.append(issue)


def _safe_text(value: object) -> str:
    raw = str(value)
    escaped: list[str] = []
    for character in raw[:_MAX_TEXT_FIELD_LENGTH]:
        codepoint = ord(character)
        if character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif unicodedata.category(character) in {"Cc", "Cf"}:
            if codepoint <= 0xFFFF:
                escaped.append(f"\\u{codepoint:04x}")
            else:
                escaped.append(f"\\U{codepoint:08x}")
        else:
            escaped.append(character)
    if len(raw) > _MAX_TEXT_FIELD_LENGTH:
        escaped.append("…")
    return "".join(escaped)


def _invalid_project_field(item: Mapping[str, Any]) -> str | None:
    root = item.get("root")
    if (
        not _bounded_clean_string(root, _MAX_ROOT_LENGTH, nullable=False)
        or not root.strip()
    ):
        return "root"
    for field in _PROJECT_COUNT_FIELDS:
        if field not in item or not _nonnegative_int(item[field]):
            return field
    if "needs_initial_build" not in item or not isinstance(item["needs_initial_build"], bool):
        return "needs_initial_build"
    for field in ("last_error", "last_success_at"):
        limit = _MAX_LAST_ERROR_LENGTH if field == "last_error" else _MAX_TIMESTAMP_LENGTH
        if field not in item or not _bounded_clean_string(item[field], limit, nullable=True):
            return field
    return None


def _normalize_status(
    raw_status: object,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    """Return the bounded, non-leaking subset safe for health classification."""
    if not isinstance(raw_status, Mapping):
        return {"projects": [], "project_count": 0}, [_schema_issue("status")], {
            "processed_count": 0,
            "truncated_count": 0,
        }

    status = {
        "status": raw_status.get("status"),
        "updated_at": raw_status.get("updated_at"),
        "project_count": raw_status.get("project_count"),
    }
    issues: list[dict[str, Any]] = []
    if "provider_lifecycle" in raw_status:
        provider_lifecycle = _normalize_provider_lifecycle(
            raw_status.get("provider_lifecycle")
        )
        if provider_lifecycle is None:
            issues.append(_schema_issue("provider_lifecycle"))
        else:
            status["provider_lifecycle"] = provider_lifecycle
    # Bounded like every other field here: this dict is the non-leaking subset
    # health is allowed to classify on, so a new key must be normalized
    # explicitly or it is silently dropped (#6).
    raw_nested = raw_status.get("unsupervised_nested_repos")
    if isinstance(raw_nested, list):
        nested = [
            str(item)[:_MAX_ROOT_LENGTH]
            for item in raw_nested[:_MAX_INPUT_PROJECTS]
            if isinstance(item, str)
        ]
        if nested:
            status["unsupervised_nested_repos"] = nested
    elif raw_nested is not None:
        issues.append(_schema_issue("unsupervised_nested_repos"))
    raw_projects = raw_status.get("projects")
    valid_projects: list[dict[str, Any]] = []
    processed_count = 0
    truncated_count = 0
    if not isinstance(raw_projects, list):
        issues.append(_schema_issue("projects"))
    else:
        for index, item in enumerate(raw_projects):
            if index >= _MAX_INPUT_PROJECTS:
                truncated_count = len(raw_projects) - _MAX_INPUT_PROJECTS
                break
            processed_count += 1
            if not isinstance(item, Mapping):
                if len(issues) < _MAX_SCHEMA_ISSUES:
                    issues.append(_schema_issue("project", index))
                continue
            invalid_field = _invalid_project_field(item)
            if invalid_field is not None:
                if len(issues) < _MAX_SCHEMA_ISSUES:
                    issues.append(_schema_issue(invalid_field, index))
                continue
            valid_projects.append({
                "root": item["root"],
                "file_count": item["file_count"],
                "build_count": item["build_count"],
                "failure_count": item["failure_count"],
                "last_error": item["last_error"],
                "last_success_at": item["last_success_at"],
                "needs_initial_build": item["needs_initial_build"],
            })
        if truncated_count:
            issues.insert(0, {
                "code": "status_truncated",
                "message": "daemon status projects were truncated",
                "processed_count": processed_count,
                "truncated_count": truncated_count,
            })

    raw_project_count = raw_status.get("project_count")
    if not _nonnegative_int(raw_project_count):
        if len(issues) < _MAX_SCHEMA_ISSUES:
            issues.append(_schema_issue("project_count"))
        status["project_count"] = len(valid_projects)

    raw_daemon_status = raw_status.get("status")
    if raw_daemon_status is not None and (
        not isinstance(raw_daemon_status, str)
        or len(raw_daemon_status) > _MAX_STATUS_LENGTH
    ):
        if len(issues) < _MAX_SCHEMA_ISSUES:
            issues.append(_schema_issue("status"))
        status["status"] = None

    raw_updated_at = raw_status.get("updated_at")
    if not _bounded_clean_string(raw_updated_at, _MAX_TIMESTAMP_LENGTH, nullable=False):
        if len(issues) < _MAX_SCHEMA_ISSUES:
            issues.append(_schema_issue("updated_at"))
        status["updated_at"] = None

    status["projects"] = valid_projects
    return status, issues[:_MAX_REPORT_ISSUES], {
        "processed_count": processed_count,
        "truncated_count": truncated_count,
    }


def _normalize_provider_lifecycle(value: object) -> dict[str, Any] | None:
    fields = {
        "status",
        "attempted",
        "deferred",
        "succeeded",
        "failed",
        "state_counts",
        "reason_counts",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        return None
    if value.get("status") not in _PROVIDER_STATUS_VALUES:
        return None
    counts: dict[str, int] = {}
    for field in ("attempted", "deferred", "succeeded", "failed"):
        raw = value.get(field)
        if not _nonnegative_int(raw) or raw > 1_000:
            return None
        counts[field] = raw
    if counts["attempted"] != counts["succeeded"] + counts["failed"]:
        return None

    normalized_maps: dict[str, dict[str, int]] = {}
    for field, allowed in (
        ("state_counts", _PROVIDER_STATE_CODES),
        ("reason_counts", _PROVIDER_REASON_CODES),
    ):
        raw_map = value.get(field)
        if not isinstance(raw_map, Mapping) or len(raw_map) > 32:
            return None
        normalized: dict[str, int] = {}
        for key, count in raw_map.items():
            if key not in allowed or not _nonnegative_int(count) or count > 1_000:
                return None
            normalized[key] = count
        normalized_maps[field] = dict(sorted(normalized.items()))
    if sum(normalized_maps["state_counts"].values()) > counts["attempted"]:
        return None
    if sum(normalized_maps["reason_counts"].values()) != counts["attempted"]:
        return None
    return {
        "status": value["status"],
        **counts,
        **normalized_maps,
    }


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
    project_counts: dict[str, int] | None = None,
    status_meta: dict[str, int] | None = None,
) -> dict[str, Any]:
    errors = errors[:_MAX_REPORT_ISSUES]
    warnings = warnings[:max(0, _MAX_REPORT_ISSUES - len(errors))]
    health_status = "degraded" if errors else "warning" if warnings else "ok"
    project_count = status.get("project_count", 0) if status else 0
    counts = project_counts or {
        "failing": len(project_health.get("failing", [])),
        "pending": len(project_health.get("pending", [])),
        "not_built_recently": len(project_health.get("not_built_recently", [])),
    }
    summary = {
        "project_count": project_count,
        "failing_count": counts["failing"],
        "pending_count": counts["pending"],
        "not_built_recently_count": counts["not_built_recently"],
        "error_count": len(errors),
        "warning_count": len(warnings),
    }
    if status_meta and status_meta.get("truncated_count", 0):
        summary["projects_processed_count"] = status_meta["processed_count"]
        summary["projects_truncated_count"] = status_meta["truncated_count"]
    return {
        "ok": not errors,
        "status": health_status,
        "checked_at": now.isoformat(timespec="seconds"),
        "base_path": str(base),
        "status_path": str(status_path),
        "status_age_seconds": None if status_age_seconds is None else round(status_age_seconds, 3),
        "daemon_status": status.get("status") if status else None,
        "summary": summary,
        "errors": errors,
        "warnings": warnings,
        "process": process,
        "startup": startup,
        "provider_lifecycle": status.get("provider_lifecycle") if status else None,
        "projects": project_health,
    }
