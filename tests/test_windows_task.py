"""Tests for Windows Scheduled Task helpers."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from graphite.cli import main
from graphite.windows_task import daemon_task_command, query_daemon_task


def test_daemon_task_command_quotes_paths_and_uses_safe_defaults(tmp_path: Path) -> None:
    exe = tmp_path / "bin" / "graphite.cmd"
    exe.parent.mkdir()
    exe.write_text("@echo off\n", encoding="utf-8")
    base = tmp_path / "Projects Root"
    base.mkdir()

    command = daemon_task_command(base, graphite_executable=str(exe))

    assert command.executable == exe.resolve()
    assert command.working_dir == base.resolve()
    assert "daemon" in command.task_run
    assert f'"{base.resolve()}"' in command.task_run
    assert "--max-builds-per-cycle 1" in command.task_run
    assert "--build-timeout 240" in command.task_run


def test_query_daemon_task_parses_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.windows_task.platform.system", lambda: "Windows")

    def fake_run(cmd, capture_output, text, check):
        assert cmd[:3] == ["schtasks.exe", "/Query", "/TN"]
        stdout = '"TaskName","Status","Task To Run"\n"\\GraphiteDaemon-FProjects","Ready","graphite daemon F:\\Projects"\n'
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    result = query_daemon_task("GraphiteDaemon-FProjects", run=fake_run)

    assert result["exists"] is True
    assert result["task"]["Status"] == "Ready"
    assert "graphite daemon" in result["task"]["Task To Run"]


def test_daemon_task_status_cli_reports_missing_task(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr("graphite.cli.query_daemon_task", lambda task_name: {"exists": False, "ok": False, "returncode": 1})

    result = main(["daemon-task-status", "--task-name", "GraphiteDaemon-Test"])
    output = capsys.readouterr().out

    assert result == 1
    assert "scheduled task not found" in output
