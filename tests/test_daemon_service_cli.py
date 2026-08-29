"""The POSIX daemon commands: refuse on the wrong platform, install via the modules."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite.cli import main


@pytest.mark.parametrize(
    "command",
    ["daemon-install-linux", "daemon-uninstall-linux", "daemon-install-macos", "daemon-uninstall-macos", "daemon-service-status"],
)
def test_the_commands_parse(command: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main([command, "--help"])
    assert exc.value.code == 0


def test_install_linux_refuses_elsewhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Windows")

    rc = main(["daemon-install-linux", str(tmp_path), "--json"])

    assert rc == 1
    assert "only available on Linux" in json.loads(capsys.readouterr().out)["error"]


def test_install_linux_calls_the_installer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def fake_install(launch, *, name, home, start_now, run):  # noqa: ANN001 - installer's shape
        seen.update(base=launch.working_dir, name=name, start_now=start_now, argv=launch.argv[1:4])
        return {"ok": True, "unit_path": "unit", "steps": []}

    monkeypatch.setattr("graphite.cli.install_unit", fake_install)

    rc = main(["daemon-install-linux", str(tmp_path), "--no-start", "--scan-interval", "7", "--json"])

    assert rc == 0
    assert seen == {"base": tmp_path.resolve(), "name": "graphite-daemon", "start_now": False, "argv": ("-P", "-m", "graphite")}
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["name"] == "graphite-daemon"


def test_install_macos_calls_the_installer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def fake_install(launch, *, label, home, uid, run):  # noqa: ANN001 - installer's shape
        seen.update(base=launch.working_dir, label=label)
        return {"ok": True, "agent_path": "agent", "steps": []}

    monkeypatch.setattr("graphite.cli.install_agent", fake_install)

    rc = main(["daemon-install-macos", str(tmp_path), "--json"])

    assert rc == 0
    assert seen == {"base": tmp_path.resolve(), "label": "com.graphite.daemon"}


def test_uninstall_reports_failure_as_exit_one(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        "graphite.cli.uninstall_unit",
        lambda name, *, home, run: {"ok": False, "unit_path": "unit", "removed": False, "steps": [{"command": ["systemctl"], "returncode": 1, "ok": False, "stdout": "", "stderr": "no"}]},
    )

    rc = main(["daemon-uninstall-linux", "--json"])

    assert rc == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_service_status_dispatches_by_platform(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("graphite.cli.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "graphite.cli.query_unit",
        lambda name, *, run: {"ok": True, "exists": True, "unit": {"ActiveState": "active", "MainPID": "9"}},
    )

    rc = main(["daemon-service-status", "--json"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["platform"] == "linux" and out["exists"] is True and out["supervisor"] == "systemd"


def test_service_status_absent_is_exit_one(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("graphite.cli.platform.system", lambda: "Darwin")
    monkeypatch.setattr("graphite.cli.query_agent", lambda label, *, uid, run: {"ok": False, "exists": False, "agent": {}})

    rc = main(["daemon-service-status", "--json"])

    assert rc == 1
    assert json.loads(capsys.readouterr().out)["supervisor"] == "launchd"
