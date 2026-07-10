"""Windows Startup-folder fallback for launching the Graphite daemon at user logon."""
from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path

from .windows_task import DEFAULT_TASK_NAME, daemon_task_command


@dataclass(frozen=True)
class StartupInstall:
    name: str
    base_path: Path
    state_dir: Path
    script_path: Path
    launcher_path: Path
    command_line: str


def require_windows() -> None:
    if platform.system().lower() != "windows":
        raise RuntimeError("Windows startup integration is only available on Windows")


def startup_folder() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set; cannot locate Windows Startup folder")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_paths(name: str, base_path: Path) -> tuple[Path, Path, Path]:
    base = base_path.resolve()
    state_dir = base / ".graphite-daemon"
    script_path = state_dir / f"{name}.ps1"
    launcher_path = startup_folder() / f"{name}.vbs"
    return state_dir, script_path, launcher_path


def install_startup_launcher(
    base_path: Path,
    *,
    name: str = DEFAULT_TASK_NAME,
    graphite_executable: str | None = None,
    scan_interval: float = 15.0,
    discover_interval: float = 90.0,
    max_projects: int = 128,
    max_depth: int = 6,
    max_builds_per_cycle: int = 1,
    build_timeout: float = 240.0,
    debounce: float = 1.0,
) -> StartupInstall:
    require_windows()
    base = base_path.resolve()
    command = daemon_task_command(
        base,
        graphite_executable=graphite_executable,
        scan_interval=scan_interval,
        discover_interval=discover_interval,
        max_projects=max_projects,
        max_depth=max_depth,
        max_builds_per_cycle=max_builds_per_cycle,
        build_timeout=build_timeout,
        debounce=debounce,
    )
    state_dir, script_path, launcher_path = startup_paths(name, base)
    state_dir.mkdir(parents=True, exist_ok=True)
    launcher_path.parent.mkdir(parents=True, exist_ok=True)

    script = _powershell_script(command.executable, command.arguments, command.working_dir)
    script_path.write_text(script, encoding="utf-8")

    vbs = _vbs_launcher(script_path)
    launcher_path.write_text(vbs, encoding="utf-8")

    return StartupInstall(
        name=name,
        base_path=base,
        state_dir=state_dir,
        script_path=script_path,
        launcher_path=launcher_path,
        command_line=command.task_run,
    )


def startup_status(base_path: Path, *, name: str = DEFAULT_TASK_NAME) -> dict[str, object]:
    require_windows()
    state_dir, script_path, launcher_path = startup_paths(name, base_path)
    return {
        "name": name,
        "installed": script_path.exists() and launcher_path.exists(),
        "script_path": str(script_path),
        "launcher_path": str(launcher_path),
        "state_dir": str(state_dir),
        "script_exists": script_path.exists(),
        "launcher_exists": launcher_path.exists(),
    }


def uninstall_startup_launcher(base_path: Path, *, name: str = DEFAULT_TASK_NAME) -> dict[str, object]:
    require_windows()
    _, script_path, launcher_path = startup_paths(name, base_path)
    removed: list[str] = []
    for path in (launcher_path, script_path):
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return {
        "name": name,
        "ok": True,
        "removed": removed,
        "script_path": str(script_path),
        "launcher_path": str(launcher_path),
    }


def _powershell_script(executable: Path, arguments: tuple[str, ...], working_dir: Path) -> str:
    args_literal = ", ".join(_ps_quote(arg) for arg in arguments)
    escaped_marker = str(working_dir)
    # The already-running guard only inspects processes that can actually host
    # the daemon (python/cmd). Matching every process command line caused false
    # positives: a PowerShell command that merely mentioned
    # "graphite ... daemon ... <base>" (e.g. a restart command) matched itself
    # and the launcher exited without starting anything.
    return f"""$ErrorActionPreference = 'Stop'
$hosts = @('python.exe', 'pythonw.exe', 'cmd.exe', 'graphite.exe')
$existing = Get-CimInstance Win32_Process | Where-Object {{ $hosts -contains $_.Name -and $_.CommandLine -like '*graphite*daemon*{escaped_marker}*' }}
if ($existing) {{ exit 0 }}
Start-Process -FilePath {_ps_quote(str(executable))} -ArgumentList @({args_literal}) -WorkingDirectory {_ps_quote(str(working_dir))} -WindowStyle Hidden
"""


def _vbs_launcher(script_path: Path) -> str:
    ps = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script_path}"'
    escaped = ps.replace('"', '""')
    return f'Set WshShell = CreateObject("WScript.Shell")\nWshShell.Run "{escaped}", 0, False\n'


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


