"""launchd agent for the Graphite daemon (macOS).

Mirrors `windows_task.py` and `systemd_unit.py`: an injectable `run`, the same
result payload shape, and the launch argv from `daemon_launch` so `-P` is
present by construction. A per-user LaunchAgent under `~/Library/LaunchAgents`,
bootstrapped into the user's GUI domain, so it needs no privilege.
"""
from __future__ import annotations

import os
import platform
import plistlib
import subprocess
from pathlib import Path
from typing import Callable, Sequence

from .daemon_launch import DaemonLaunch
from .io import atomic_write_text

DEFAULT_LABEL = "com.graphite.daemon"

#: What the `run` seams accept: `subprocess.run`, or a test double of its shape.
Runner = Callable[..., subprocess.CompletedProcess[str]]


def require_macos() -> None:
    if platform.system().lower() != "darwin":
        raise RuntimeError("launchd agent integration is only available on macOS")


def _home(home: Path | None) -> Path:
    return home if home is not None else Path.home()


def _uid(uid: int | None) -> int:
    if uid is not None:
        return uid
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise RuntimeError("launchd agent integration needs a POSIX uid")
    return int(getuid())


def agent_path(label: str = DEFAULT_LABEL, *, home: Path | None = None) -> Path:
    return _home(home) / "Library" / "LaunchAgents" / f"{label}.plist"


def log_dir(*, home: Path | None = None) -> Path:
    return _home(home) / "Library" / "Logs" / "graphite"


def render_plist(launch: DaemonLaunch, *, label: str = DEFAULT_LABEL, home: Path | None = None) -> bytes:
    logs = log_dir(home=home)
    payload = {
        "Label": label,
        "ProgramArguments": list(launch.argv),
        "WorkingDirectory": str(launch.working_dir),
        "RunAtLoad": True,
        # Restart on a crash, stay stopped after a clean exit -- the same
        # policy as the systemd unit's Restart=on-failure.
        "KeepAlive": {"SuccessfulExit": False},
        "EnvironmentVariables": {"PYTHONSAFEPATH": "1"},
        "StandardOutPath": str(logs / "daemon.log"),
        "StandardErrorPath": str(logs / "daemon.err"),
    }
    return plistlib.dumps(payload, sort_keys=True)


def _payload(result: subprocess.CompletedProcess[str], *, command: Sequence[str]) -> dict[str, object]:
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": (result.stdout or "").strip(),
        "stderr": (result.stderr or "").strip(),
        "command": list(command),
    }


def _launchctl(args: Sequence[str], run: Runner) -> dict[str, object]:
    cmd = ["launchctl", *args]
    return _payload(run(cmd, capture_output=True, text=True, check=False), command=cmd)


def install_agent(
    launch: DaemonLaunch,
    *,
    label: str = DEFAULT_LABEL,
    home: Path | None = None,
    uid: int | None = None,
    run: Runner = subprocess.run,
) -> dict[str, object]:
    require_macos()
    path = agent_path(label, home=home)
    log_dir(home=home).mkdir(parents=True, exist_ok=True)
    # plistlib emits UTF-8 XML; the atomic text writer keeps the same bytes.
    atomic_write_text(path, render_plist(launch, label=label, home=home).decode("utf-8"))
    step = _launchctl(["bootstrap", f"gui/{_uid(uid)}", str(path)], run)
    return {"ok": bool(step["ok"]), "agent_path": str(path), "steps": [step]}


def query_agent(
    label: str = DEFAULT_LABEL,
    *,
    uid: int | None = None,
    run: Runner = subprocess.run,
) -> dict[str, object]:
    require_macos()
    payload = _launchctl(["print", f"gui/{_uid(uid)}/{label}"], run)
    fields: dict[str, str] = {}
    if payload["ok"]:
        for line in str(payload["stdout"]).splitlines():
            key, sep, value = line.strip().partition(" = ")
            if sep and key in ("pid", "state"):
                fields[key] = value.strip()
    payload["exists"] = bool(payload["ok"])
    payload["agent"] = fields
    return payload


def uninstall_agent(
    label: str = DEFAULT_LABEL,
    *,
    home: Path | None = None,
    uid: int | None = None,
    run: Runner = subprocess.run,
) -> dict[str, object]:
    require_macos()
    step = _launchctl(["bootout", f"gui/{_uid(uid)}/{label}"], run)
    path = agent_path(label, home=home)
    removed = False
    if path.exists():
        path.unlink()
        removed = True
    return {
        "ok": bool(step["ok"]) or removed,
        "agent_path": str(path),
        "removed": removed,
        "steps": [step],
    }
