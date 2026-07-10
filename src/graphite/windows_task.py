"""Windows Scheduled Task helpers for the Graphite daemon."""
from __future__ import annotations

import csv
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

DEFAULT_TASK_NAME = "GraphiteDaemon-FProjects"


@dataclass(frozen=True)
class TaskCommand:
    executable: Path
    arguments: tuple[str, ...]
    working_dir: Path

    @property
    def task_run(self) -> str:
        quoted_exe = f'"{self.executable}"'
        rendered_args = " ".join(_quote_arg(arg) for arg in self.arguments)
        return f"{quoted_exe} {rendered_args}".strip()


def _quote_arg(arg: str) -> str:
    if not arg:
        return '""'
    if any(ch.isspace() for ch in arg) or any(ch in arg for ch in '"&()[]{}^=;!,`'):
        return '"' + arg.replace('"', '\\"') + '"'
    return arg


def require_windows() -> None:
    if platform.system().lower() != "windows":
        raise RuntimeError("Windows Scheduled Task integration is only available on Windows")


def resolve_graphite_executable(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        return path
    found = shutil.which("graphite")
    if found:
        return Path(found).resolve()
    local = Path.home() / ".local" / "bin" / "graphite.cmd"
    if local.exists():
        return local.resolve()
    raise FileNotFoundError("graphite executable not found on PATH or at ~/.local/bin/graphite.cmd")


def daemon_task_command(
    base_path: Path,
    *,
    graphite_executable: str | None = None,
    scan_interval: float = 15.0,
    discover_interval: float = 90.0,
    max_projects: int = 128,
    max_depth: int = 6,
    max_builds_per_cycle: int = 1,
    build_timeout: float = 240.0,
    debounce: float = 1.0,
) -> TaskCommand:
    base = base_path.resolve()
    args = (
        "daemon",
        str(base),
        "--scan-interval",
        _fmt_number(scan_interval),
        "--discover-interval",
        _fmt_number(discover_interval),
        "--max-projects",
        str(max_projects),
        "--max-depth",
        str(max_depth),
        "--max-builds-per-cycle",
        str(max_builds_per_cycle),
        "--build-timeout",
        _fmt_number(build_timeout),
        "--debounce",
        _fmt_number(debounce),
    )
    return TaskCommand(
        executable=resolve_graphite_executable(graphite_executable),
        arguments=args,
        working_dir=base,
    )


def _fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def create_daemon_task(
    task_name: str,
    command: TaskCommand,
    *,
    force: bool = True,
    start_now: bool = False,
    run: callable = subprocess.run,
) -> dict[str, object]:
    require_windows()
    cmd = [
        "schtasks.exe",
        "/Create",
        "/TN",
        task_name,
        "/TR",
        command.task_run,
        "/SC",
        "ONLOGON",
        "/RL",
        "LIMITED",
    ]
    if force:
        cmd.append("/F")
    result = run(cmd, capture_output=True, text=True, check=False)
    payload = _result_payload(result, command=cmd)
    if result.returncode == 0 and start_now:
        started = start_daemon_task(task_name, run=run)
        payload["started"] = started
    return payload


def start_daemon_task(task_name: str, *, run: callable = subprocess.run) -> dict[str, object]:
    require_windows()
    cmd = ["schtasks.exe", "/Run", "/TN", task_name]
    result = run(cmd, capture_output=True, text=True, check=False)
    return _result_payload(result, command=cmd)


def delete_daemon_task(task_name: str, *, run: callable = subprocess.run) -> dict[str, object]:
    require_windows()
    cmd = ["schtasks.exe", "/Delete", "/TN", task_name, "/F"]
    result = run(cmd, capture_output=True, text=True, check=False)
    return _result_payload(result, command=cmd)


def query_daemon_task(task_name: str, *, run: callable = subprocess.run) -> dict[str, object]:
    require_windows()
    cmd = ["schtasks.exe", "/Query", "/TN", task_name, "/FO", "CSV", "/V"]
    result = run(cmd, capture_output=True, text=True, check=False)
    payload = _result_payload(result, command=cmd)
    if result.returncode != 0:
        payload["exists"] = False
        return payload
    payload["exists"] = True
    payload["task"] = _parse_schtasks_csv(result.stdout)
    return payload


def _parse_schtasks_csv(stdout: str) -> dict[str, str]:
    rows = list(csv.DictReader(stdout.splitlines()))
    if not rows:
        return {}
    return {str(k): str(v) for k, v in rows[0].items()}


def _result_payload(result: subprocess.CompletedProcess[str], *, command: Sequence[str]) -> dict[str, object]:
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": (result.stdout or "").strip(),
        "stderr": (result.stderr or "").strip(),
        "command": list(command),
    }
