"""Tests for Windows Scheduled Task helpers."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from graphite.cli import main
from graphite.windows_task import daemon_task_command, query_daemon_task


def test_daemon_task_command_quotes_paths_and_uses_safe_defaults(tmp_path: Path) -> None:
    base = tmp_path / "Projects Root"
    base.mkdir()

    command = daemon_task_command(base)

    assert command.working_dir == base.resolve()
    assert "-P -m graphite daemon" in command.task_run
    assert f'"{base.resolve()}"' in command.task_run
    assert "--max-builds-per-cycle 1" in command.task_run
    assert "--build-timeout 240" in command.task_run


def test_generated_launcher_runs_the_interpreter_not_a_bare_console_script(
    tmp_path: Path,
) -> None:
    """A generated launcher must not be shadowable.

    `graphite ...` is hijacked exactly like `python -m graphite`: an installed
    entry point does not put its own directory on `sys.path[0]`, and this
    launcher runs with a projects root as its working directory, so a
    `graphite.py` dropped there wins. A console script also cannot express the
    fix -- there is no `-P` to add to it -- which is why the launcher has to run
    the interpreter directly rather than the shim.
    """
    base = tmp_path / "Projects Root"
    base.mkdir()

    command = daemon_task_command(base)

    assert command.executable == Path(sys.executable)
    assert command.arguments[:4] == ("-P", "-m", "graphite", "daemon")
    assert command.arguments[4] == str(base.resolve())


def test_generated_launcher_refuses_a_console_script_it_cannot_protect(
    tmp_path: Path,
) -> None:
    """Failing closed beats accepting the input that caused the defect.

    A console script is the vulnerable shape itself, and no flag can fix it --
    pip's generated `graphite.exe` cannot even be edited. Accepting one here
    would quietly regenerate a shadowable launcher.
    """
    shim = tmp_path / "bin" / "graphite.cmd"
    shim.parent.mkdir()
    shim.write_text("@echo off\n", encoding="utf-8")
    base = tmp_path / "Projects"
    base.mkdir()

    with pytest.raises(ValueError) as excinfo:
        daemon_task_command(base, graphite_executable=str(shim))

    assert "-P" in str(excinfo.value)


def test_an_explicit_interpreter_is_still_honoured(tmp_path: Path) -> None:
    """The override remains usable; it just has to name an interpreter."""
    interpreter = tmp_path / "python.exe"
    interpreter.write_text("", encoding="utf-8")
    base = tmp_path / "Projects"
    base.mkdir()

    command = daemon_task_command(base, graphite_executable=str(interpreter))

    assert command.executable == interpreter.resolve()
    assert command.arguments[:3] == ("-P", "-m", "graphite")


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
