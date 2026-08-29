"""launchd agent for the daemon: plist rendering and the launchctl sequence."""
from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from graphite.daemon_launch import daemon_launch
from graphite.launchd_agent import (
    DEFAULT_LABEL,
    agent_path,
    install_agent,
    query_agent,
    render_plist,
    uninstall_agent,
)


class FakeRun:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, cmd, capture_output, text, check):  # noqa: ANN001 - subprocess.run's shape
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def test_plist_round_trips_with_the_launch_argv(tmp_path: Path) -> None:
    base = tmp_path / "Projects Root"
    base.mkdir()
    launch = daemon_launch(base)

    data = plistlib.loads(render_plist(launch, home=tmp_path))

    assert data["Label"] == DEFAULT_LABEL
    assert data["ProgramArguments"] == list(launch.argv)
    assert data["ProgramArguments"][1:5] == ["-P", "-m", "graphite", "daemon"]
    assert data["WorkingDirectory"] == str(base.resolve())
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["EnvironmentVariables"] == {"PYTHONSAFEPATH": "1"}
    assert data["StandardOutPath"] == str(tmp_path / "Library" / "Logs" / "graphite" / "daemon.log")
    assert data["StandardErrorPath"] == str(tmp_path / "Library" / "Logs" / "graphite" / "daemon.err")


def test_agent_path_lives_under_launch_agents(tmp_path: Path) -> None:
    assert agent_path(home=tmp_path) == tmp_path / "Library" / "LaunchAgents" / f"{DEFAULT_LABEL}.plist"


def test_install_writes_plist_and_bootstraps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.launchd_agent.platform.system", lambda: "Darwin")
    run = FakeRun()

    payload = install_agent(daemon_launch(tmp_path), home=tmp_path, uid=501, run=run)

    assert payload["ok"] is True
    path = agent_path(home=tmp_path)
    assert payload["agent_path"] == str(path)
    assert plistlib.loads(path.read_bytes())["Label"] == DEFAULT_LABEL
    assert (tmp_path / "Library" / "Logs" / "graphite").is_dir()
    assert run.calls == [["launchctl", "bootstrap", "gui/501", str(path)]]


def test_query_parses_state_and_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.launchd_agent.platform.system", lambda: "Darwin")
    run = FakeRun(stdout="com.graphite.daemon = {\n\tactive count = 1\n\tpid = 777\n\tstate = running\n}\n")

    payload = query_agent(uid=501, run=run)

    assert payload["exists"] is True
    assert payload["agent"] == {"pid": "777", "state": "running"}
    assert run.calls == [["launchctl", "print", f"gui/501/{DEFAULT_LABEL}"]]


def test_query_absent_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.launchd_agent.platform.system", lambda: "Darwin")

    payload = query_agent(uid=501, run=FakeRun(returncode=113))

    assert payload["exists"] is False
    assert payload["agent"] == {}


def test_uninstall_boots_out_then_removes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.launchd_agent.platform.system", lambda: "Darwin")
    run = FakeRun()
    install_agent(daemon_launch(tmp_path), home=tmp_path, uid=501, run=run)

    payload = uninstall_agent(home=tmp_path, uid=501, run=run)

    assert payload["ok"] is True
    assert payload["removed"] is True
    assert not agent_path(home=tmp_path).exists()
    assert run.calls[-1] == ["launchctl", "bootout", f"gui/501/{DEFAULT_LABEL}"]


def test_refuses_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.launchd_agent.platform.system", lambda: "Linux")

    with pytest.raises(RuntimeError, match="only available on macOS"):
        query_agent(uid=501, run=FakeRun())


@pytest.mark.skipif(sys.platform != "darwin", reason="plutil runs on the macOS leg only")
def test_rendered_plist_passes_plutil_lint(tmp_path: Path) -> None:
    """The artifact, checked by macOS itself. plutil ships with every macOS,
    so on the macOS leg this must RUN -- an absent plutil is a failure there,
    not a skip."""
    plutil = shutil.which("plutil")
    assert plutil is not None, "plutil must exist on macOS"
    path = tmp_path / "agent.plist"
    path.write_bytes(render_plist(daemon_launch(tmp_path), home=tmp_path))

    result = subprocess.run([plutil, "-lint", str(path)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stdout + result.stderr
