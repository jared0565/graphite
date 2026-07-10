"""Tests for Windows Startup-folder Graphite daemon launcher."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.cli import main
from graphite.windows_startup import install_startup_launcher, startup_status


def test_install_startup_launcher_writes_hidden_vbs_and_idempotent_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("graphite.windows_startup.platform.system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    exe = tmp_path / "bin" / "graphite.cmd"
    exe.parent.mkdir()
    exe.write_text("@echo off\n", encoding="utf-8")
    base = tmp_path / "Projects"
    base.mkdir()

    result = install_startup_launcher(base, graphite_executable=str(exe), name="GraphiteDaemon-Test")

    assert result.script_path.exists()
    assert result.launcher_path.exists()
    script = result.script_path.read_text(encoding="utf-8")
    launcher = result.launcher_path.read_text(encoding="utf-8")
    assert "Get-CimInstance Win32_Process" in script
    assert "Start-Process" in script
    assert "WindowStyle Hidden" in script
    assert str(base.resolve()) in script
    # The already-running guard must only match daemon host processes, not any
    # shell whose command line merely mentions graphite/daemon (self-match bug).
    assert "$hosts -contains $_.Name" in script
    assert "'python.exe'" in script
    assert "WScript.Shell" in launcher
    assert ", 0, False" in launcher

    status = startup_status(base, name="GraphiteDaemon-Test")
    assert status["installed"] is True


def test_daemon_startup_status_cli_reports_missing(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr("graphite.cli.startup_status", lambda base_path, name: {
        "name": name,
        "installed": False,
        "launcher_path": "missing.vbs",
        "script_path": "missing.ps1",
    })

    result = main(["daemon-startup-status", "F:/Projects", "--name", "GraphiteDaemon-Test"])
    output = capsys.readouterr().out

    assert result == 1
    assert "startup launcher not installed" in output
