"""Tests for Windows Startup-folder Graphite daemon launcher."""
from __future__ import annotations

import sys
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
    base = tmp_path / "Projects"
    base.mkdir()

    result = install_startup_launcher(base, name="GraphiteDaemon-Test")

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
    # This script starts hidden at login with a projects root as its working
    # directory -- precisely where a `graphite.py` would beat the installed
    # package. So it must launch the interpreter with `-P` and never a bare
    # console script, which cannot carry the flag at all.
    launch = next(line for line in script.splitlines() if line.startswith("Start-Process"))
    assert f"-FilePath '{Path(sys.executable)}'" in launch
    assert "@('-P', '-m', 'graphite', 'daemon'" in launch
    # Scoped to the launch line on purpose. `graphite.exe` still appears in the
    # script's `$hosts` list, which is the already-running detector and must
    # keep recognising a legacy console-script-hosted daemon so this does not
    # start a second one. Only what gets LAUNCHED has to avoid the shim.
    assert "graphite.cmd" not in launch
    assert "graphite.exe" not in launch

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
