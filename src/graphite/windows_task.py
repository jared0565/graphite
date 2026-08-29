"""Windows Scheduled Task helpers for the Graphite daemon."""
from __future__ import annotations

import csv
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

DEFAULT_TASK_NAME = "GraphiteDaemon-FProjects"
#: What the `run` seams accept: `subprocess.run`, or a test double of its shape.
Runner = Callable[..., subprocess.CompletedProcess[str]]


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


#: Console scripts a generated launcher must never invoke. Matched on the stem,
#: so `graphite`, `graphite.cmd` and `graphite.exe` are all refused.
_CONSOLE_SCRIPT_STEMS = frozenset({"graphite", "graphite-mcp"})


def resolve_launcher_interpreter(explicit: str | None = None) -> Path:
    """The Python interpreter a generated launcher runs. Never a console script.

    The hazard is not the console script itself. Running a SCRIPT puts the
    script's own directory on `sys.path[0]`; only `-m` puts the CWD there, and
    that is the entire mechanism -- so a pip-generated entry point is not
    cwd-shadowable. The hazard is the `-m` a wrapper may run internally, which
    the generator cannot see and cannot add `-P` to.

    That is not hypothetical. The resolver this replaced returned
    `shutil.which("graphite")` or `~/.local/bin/graphite.cmd`, and on the
    machine this was found on there is no pip console script at all -- what
    `graphite` names there is a hand-written `.cmd` running
    `python -B -m graphite`. This launcher starts hidden at every login with a
    projects root as its working directory, so a `graphite.py` -- or a
    `graphite/` directory -- dropped at that root beat the installed package,
    and a module-shaped shadow RUNS before it errors. Measured, cwd holding a
    hostile `graphite.py`:

        python -m graphite                 -> shadow ran
        python -P -m graphite              -> real graphite
        .cmd -> python -B -m graphite      -> shadow ran
        .cmd -> python -B -P -m graphite   -> real graphite

    So the launcher runs the interpreter directly and passes `-P` itself: the
    only shape it can guarantee without auditing someone else's wrapper.

    An explicit override stays supported and is honoured, but one naming a
    console script is refused -- the argument vector built below begins
    `-P -m graphite`, which only an interpreter can accept.
    """
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        if path.stem.lower() in _CONSOLE_SCRIPT_STEMS:
            raise ValueError(
                f"{path.name} is a console script and cannot carry `-P`, so a launcher "
                "built from it would be shadowable by a `graphite.py` in its working "
                "directory. Pass a Python interpreter instead."
            )
        return path
    return Path(sys.executable)


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
    # The argv itself -- `-P -m graphite daemon ...` -- is built by
    # `daemon_launch`, shared with the systemd and launchd installers so the
    # three supervisors cannot drift apart on the flags that matter. Imported
    # here rather than at module level because daemon_launch imports
    # `resolve_launcher_interpreter` from this module.
    from .daemon_launch import daemon_launch

    launch = daemon_launch(
        base_path,
        interpreter=graphite_executable,
        scan_interval=scan_interval,
        discover_interval=discover_interval,
        max_projects=max_projects,
        max_depth=max_depth,
        max_builds_per_cycle=max_builds_per_cycle,
        build_timeout=build_timeout,
        debounce=debounce,
    )
    return TaskCommand(
        executable=launch.interpreter,
        arguments=launch.arguments,
        working_dir=launch.working_dir,
    )


def create_daemon_task(
    task_name: str,
    command: TaskCommand,
    *,
    force: bool = True,
    start_now: bool = False,
    run: Runner = subprocess.run,
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


def start_daemon_task(task_name: str, *, run: Runner = subprocess.run) -> dict[str, object]:
    require_windows()
    cmd = ["schtasks.exe", "/Run", "/TN", task_name]
    result = run(cmd, capture_output=True, text=True, check=False)
    return _result_payload(result, command=cmd)


def delete_daemon_task(task_name: str, *, run: Runner = subprocess.run) -> dict[str, object]:
    require_windows()
    cmd = ["schtasks.exe", "/Delete", "/TN", task_name, "/F"]
    result = run(cmd, capture_output=True, text=True, check=False)
    return _result_payload(result, command=cmd)


def query_daemon_task(task_name: str, *, run: Runner = subprocess.run) -> dict[str, object]:
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
