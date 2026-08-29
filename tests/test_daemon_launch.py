"""The one argv builder every daemon launcher shares."""
from __future__ import annotations

import sys
from pathlib import Path

from graphite.daemon_launch import daemon_launch


def test_launch_argv_begins_with_the_interpreter_and_safe_path(tmp_path: Path) -> None:
    base = tmp_path / "Projects Root"
    base.mkdir()

    launch = daemon_launch(base)

    assert launch.interpreter == Path(sys.executable)
    assert launch.arguments[:5] == ("-P", "-m", "graphite", "daemon", str(base.resolve()))
    assert launch.working_dir == base.resolve()
    assert launch.argv[0] == sys.executable
    assert "-B" not in launch.arguments


def test_launch_argv_renders_numbers_without_trailing_zero(tmp_path: Path) -> None:
    launch = daemon_launch(tmp_path, build_timeout=240.0, debounce=1.5)

    args = launch.arguments
    assert args[args.index("--build-timeout") + 1] == "240"
    assert args[args.index("--debounce") + 1] == "1.5"
    assert args[args.index("--max-projects") + 1] == "128"


def test_windows_task_command_uses_the_shared_builder(tmp_path: Path) -> None:
    """The Windows generator delegates, so the three supervisors can never
    drift apart on the flags that matter."""
    from graphite.windows_task import daemon_task_command

    command = daemon_task_command(tmp_path, scan_interval=7.5, max_depth=3)

    expected = daemon_launch(tmp_path, scan_interval=7.5, max_depth=3)
    assert command.arguments == expected.arguments
    assert command.executable == expected.interpreter
    assert command.working_dir == expected.working_dir
