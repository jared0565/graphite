"""systemd user unit for the daemon: rendering, quoting, and the command sequence."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from graphite.daemon_launch import daemon_launch
from graphite.systemd_unit import (
    DEFAULT_UNIT_NAME,
    install_unit,
    query_unit,
    render_unit,
    systemd_quote,
    uninstall_unit,
    unit_path,
)


class FakeRun:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, cmd, capture_output, text, check):  # noqa: ANN001 - subprocess.run's shape
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def test_render_unit_carries_safe_path_and_restart_policy(tmp_path: Path) -> None:
    base = tmp_path / "Projects Root"
    base.mkdir()

    text = render_unit(daemon_launch(base))

    assert "[Unit]" in text and "[Service]" in text and "[Install]" in text
    exec_line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    assert exec_line == (
        f"ExecStart={systemd_quote(sys.executable)} -P -m graphite daemon {systemd_quote(str(base.resolve()))}"
        " --scan-interval 15 --discover-interval 90 --max-projects 128 --max-depth 6"
        " --max-builds-per-cycle 1 --build-timeout 240 --debounce 1"
    )
    assert f"WorkingDirectory={base.resolve()}" in text
    assert "Restart=on-failure" in text
    assert "Environment=PYTHONSAFEPATH=1" in text
    assert "WantedBy=default.target" in text


def test_systemd_quote_wraps_spaces_and_escapes_quotes() -> None:
    assert systemd_quote("plain") == "plain"
    assert systemd_quote("has space") == '"has space"'
    assert systemd_quote('say "hi"') == '"say \\"hi\\""'
    assert systemd_quote("") == '""'


def test_unit_path_lives_under_the_user_config_dir(tmp_path: Path) -> None:
    assert unit_path(home=tmp_path) == tmp_path / ".config" / "systemd" / "user" / f"{DEFAULT_UNIT_NAME}.service"


def test_install_writes_the_unit_then_reloads_and_enables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Linux")
    run = FakeRun()

    payload = install_unit(daemon_launch(tmp_path), home=tmp_path, run=run)

    assert payload["ok"] is True
    assert payload["unit_path"] == str(unit_path(home=tmp_path))
    assert unit_path(home=tmp_path).read_text(encoding="utf-8").startswith("[Unit]")
    assert run.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", f"{DEFAULT_UNIT_NAME}.service"],
    ]


def test_install_without_start_only_enables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Linux")
    run = FakeRun()

    install_unit(daemon_launch(tmp_path), home=tmp_path, start_now=False, run=run)

    assert run.calls[-1] == ["systemctl", "--user", "enable", f"{DEFAULT_UNIT_NAME}.service"]


def test_install_reports_a_failing_systemctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Linux")
    run = FakeRun(returncode=1)

    payload = install_unit(daemon_launch(tmp_path), home=tmp_path, run=run)

    assert payload["ok"] is False
    assert all(step["returncode"] == 1 for step in payload["steps"])


def test_query_reports_active_state_and_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Linux")
    run = FakeRun(stdout="ActiveState=active\nSubState=running\nMainPID=4242\nLoadState=loaded\n")

    payload = query_unit(run=run)

    assert payload["exists"] is True
    assert payload["unit"] == {"ActiveState": "active", "SubState": "running", "MainPID": "4242"}
    assert run.calls == [
        ["systemctl", "--user", "show", "-p", "ActiveState,SubState,MainPID,LoadState", f"{DEFAULT_UNIT_NAME}.service"]
    ]


def test_query_absent_unit_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Linux")
    run = FakeRun(stdout="ActiveState=inactive\nSubState=dead\nMainPID=0\nLoadState=not-found\n")

    payload = query_unit(run=run)

    assert payload["ok"] is True
    assert payload["exists"] is False


def test_uninstall_disables_then_removes_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Linux")
    run = FakeRun()
    install_unit(daemon_launch(tmp_path), home=tmp_path, run=run)

    payload = uninstall_unit(home=tmp_path, run=run)

    assert payload["ok"] is True
    assert payload["removed"] is True
    assert not unit_path(home=tmp_path).exists()
    assert run.calls[-2:] == [
        ["systemctl", "--user", "disable", "--now", f"{DEFAULT_UNIT_NAME}.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]


def test_refuses_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Darwin")

    with pytest.raises(RuntimeError, match="only available on Linux"):
        query_unit(run=FakeRun())


@pytest.mark.skipif(sys.platform != "linux", reason="systemd-analyze verify runs on the Linux leg only")
def test_rendered_unit_passes_systemd_analyze(tmp_path: Path) -> None:
    """The artifact, checked by systemd itself. `ubuntu-latest` ships
    systemd-analyze; a Linux host without it skips with a named reason."""
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze not installed on this Linux host")
    unit = tmp_path / f"{DEFAULT_UNIT_NAME}.service"
    unit.write_text(render_unit(daemon_launch(tmp_path)), encoding="utf-8")

    result = subprocess.run([analyze, "--user", "verify", str(unit)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
