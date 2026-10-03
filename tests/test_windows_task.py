"""Tests for Windows Scheduled Task helpers."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from graphite.cli import main
from graphite.programs import system_program
from graphite.windows_task import (
    TaskCommand,
    create_daemon_task,
    daemon_task_command,
    delete_daemon_task,
    query_daemon_task,
    start_daemon_task,
)


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

    Only `-m` puts the CWD on `sys.path[0]`, so a console script is not itself
    the hazard -- the `-m` inside a wrapper is. The resolver this replaced
    returned whatever `graphite` was on PATH, which in the field was a
    hand-written `.cmd` running `python -B -m graphite`; launched with a
    projects root as its working directory, the shadow ran (measured). The
    generator cannot see inside a wrapper, so it runs the interpreter and
    passes `-P` itself.
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
    """Failing closed beats accepting an argument vector it cannot carry.

    The command built here begins `-P -m graphite`, which only a Python
    interpreter can accept -- handing those flags to `graphite.cmd` would be
    broken quite apart from shadowing. A wrapper is also unauditable: the
    generator cannot tell whether it runs `-m` internally, and cannot add `-P`
    to one that does.
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
        assert cmd[:3] == [str(system_program("schtasks.exe")), "/Query", "/TN"]
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


# --- schtasks is launched from System32 by absolute path (channel round 304) ---


@pytest.mark.parametrize(
    ("call", "verb"),
    [
        (lambda run: create_daemon_task("T", TaskCommand(Path("python.exe"), ("-P",), Path(".")), run=run), "/Create"),
        (lambda run: start_daemon_task("T", run=run), "/Run"),
        (lambda run: delete_daemon_task("T", run=run), "/Delete"),
        (lambda run: query_daemon_task("T", run=run), "/Query"),
    ],
)
def test_every_schtasks_call_names_the_system_copy_by_absolute_path(
    monkeypatch: pytest.MonkeyPatch, call: object, verb: str
) -> None:
    # A bare `schtasks.exe` is looked up in the current directory before
    # System32, so these are anchored in the directory the OS reports.
    monkeypatch.setattr("graphite.windows_task.platform.system", lambda: "Windows")
    seen: list[list[str]] = []

    def record(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "")

    call(record)  # type: ignore[operator]

    assert seen[0][0] == str(system_program("schtasks.exe"))
    assert seen[0][1] == verb
    if sys.platform == "win32":
        assert Path(seen[0][0]).is_absolute() and Path(seen[0][0]).is_file()
