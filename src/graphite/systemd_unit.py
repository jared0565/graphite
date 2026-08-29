"""systemd user unit for the Graphite daemon (Linux).

Mirrors `windows_task.py`: an injectable `run`, the same result payload shape,
and the launch argv from `daemon_launch` so `-P` is present by construction.
The unit is a USER unit (`systemctl --user`), so it needs no privilege and
runs while the user has a session; lingering (`loginctl enable-linger`) is
a policy decision this module deliberately does not make.
"""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import Callable, Sequence

from .daemon_launch import DaemonLaunch
from .io import atomic_write_text

DEFAULT_UNIT_NAME = "graphite-daemon"

#: What the `run` seams accept: `subprocess.run`, or a test double of its shape.
Runner = Callable[..., subprocess.CompletedProcess[str]]


def require_linux() -> None:
    if platform.system().lower() != "linux":
        raise RuntimeError("systemd user unit integration is only available on Linux")


def systemd_quote(arg: str) -> str:
    """Quote one ExecStart argument per systemd.service(5) rules."""
    if arg and not any(ch.isspace() for ch in arg) and '"' not in arg and "'" not in arg:
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit_path(name: str = DEFAULT_UNIT_NAME, *, home: Path | None = None) -> Path:
    root = home if home is not None else Path.home()
    return root / ".config" / "systemd" / "user" / f"{name}.service"


def render_unit(launch: DaemonLaunch, *, description: str = "Graphite daemon") -> str:
    exec_start = " ".join(systemd_quote(part) for part in launch.argv)
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        f"WorkingDirectory={launch.working_dir}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        # Belt and braces beside `-P`: the same guarantee expressed as the
        # environment variable, for anyone who edits ExecStart by hand later.
        "Environment=PYTHONSAFEPATH=1\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _payload(result: subprocess.CompletedProcess[str], *, command: Sequence[str]) -> dict[str, object]:
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": (result.stdout or "").strip(),
        "stderr": (result.stderr or "").strip(),
        "command": list(command),
    }


def _systemctl(args: Sequence[str], run: Runner) -> dict[str, object]:
    cmd = ["systemctl", "--user", *args]
    return _payload(run(cmd, capture_output=True, text=True, check=False), command=cmd)


def install_unit(
    launch: DaemonLaunch,
    *,
    name: str = DEFAULT_UNIT_NAME,
    home: Path | None = None,
    start_now: bool = True,
    run: Runner = subprocess.run,
) -> dict[str, object]:
    require_linux()
    path = unit_path(name, home=home)
    atomic_write_text(path, render_unit(launch))
    steps = [_systemctl(["daemon-reload"], run)]
    enable = ["enable", "--now", f"{name}.service"] if start_now else ["enable", f"{name}.service"]
    steps.append(_systemctl(enable, run))
    return {"ok": all(bool(step["ok"]) for step in steps), "unit_path": str(path), "steps": steps}


def query_unit(name: str = DEFAULT_UNIT_NAME, *, run: Runner = subprocess.run) -> dict[str, object]:
    require_linux()
    payload = _systemctl(["show", "-p", "ActiveState,SubState,MainPID,LoadState", f"{name}.service"], run)
    fields: dict[str, str] = {}
    for line in str(payload["stdout"]).splitlines():
        key, _, value = line.partition("=")
        if key:
            fields[key] = value
    load_state = fields.pop("LoadState", "")
    payload["exists"] = bool(payload["ok"]) and load_state not in ("", "not-found")
    payload["unit"] = fields
    return payload


def uninstall_unit(
    name: str = DEFAULT_UNIT_NAME,
    *,
    home: Path | None = None,
    run: Runner = subprocess.run,
) -> dict[str, object]:
    require_linux()
    steps = [_systemctl(["disable", "--now", f"{name}.service"], run)]
    path = unit_path(name, home=home)
    removed = False
    if path.exists():
        path.unlink()
        removed = True
    steps.append(_systemctl(["daemon-reload"], run))
    return {
        "ok": all(bool(step["ok"]) for step in steps),
        "unit_path": str(path),
        "removed": removed,
        "steps": steps,
    }
