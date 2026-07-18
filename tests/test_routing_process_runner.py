"""Hardened common CLI subprocess policy tests."""
from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

import pytest

from graphite.probe_process import ProbeProcessError, ProbeProcessResult
from graphite.routing.contracts import ProviderId
from graphite.routing.process_runner import (
    CliProcessError,
    build_cli_environment,
    decode_cli_output,
    require_process_containment,
    run_cli_process,
)

FAKE_CLI = Path(__file__).parent / "fake_clis" / "fake_cli.py"


def _credential_home(tmp_path: Path) -> Path:
    path = tmp_path / "credentials"
    path.mkdir()
    return path


def test_cli_environment_is_allowlisted_and_strips_ambient_secrets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = _credential_home(tmp_path)
    source = {
        "SYSTEMROOT": r"C:\Windows",
        "WINDIR": r"C:\Windows",
        "COMSPEC": r"C:\Windows\System32\cmd.exe",
        "PATHEXT": ".COM;.EXE",
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "appdata"),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
        "OPENAI_API_KEY": "secret",
        "ANTHROPIC_API_KEY": "secret",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "NPM_TOKEN": "secret",
        "HTTPS_PROXY": "http://private.invalid",
        "PATH": str(workspace / "hostile-bin"),
    }

    environment = build_cli_environment(
        provider=ProviderId.CODEX,
        executable=Path(sys.executable),
        workspace=workspace,
        credential_home=credentials,
        source=source,
    )

    assert environment["CODEX_HOME"] == str(credentials.resolve())
    assert environment["NO_COLOR"] == "1"
    assert environment["TERM"] == "dumb"
    assert str(workspace) not in environment["PATH"]
    for forbidden in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "NPM_TOKEN",
        "HTTPS_PROXY",
    ):
        assert forbidden not in environment


def test_credential_override_inside_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    credentials = workspace / "credentials"
    credentials.mkdir(parents=True)

    with pytest.raises(CliProcessError, match="credential_home_invalid"):
        build_cli_environment(
            provider=ProviderId.CLAUDE_CODE,
            executable=Path(sys.executable),
            workspace=workspace,
            credential_home=credentials,
            source={},
        )


def test_run_cli_process_uses_fixed_argv_exact_environment_and_bounded_stdin(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = _credential_home(tmp_path)
    observed: dict[str, object] = {}

    def runner(argv: list[str], **kwargs: object) -> ProbeProcessResult:
        observed["argv"] = argv
        observed.update(kwargs)
        return ProbeProcessResult(0, b'{"ok":true}', b"", 0.01)

    result = run_cli_process(
        argv=(sys.executable, str(FAKE_CLI), "echo"),
        cwd=workspace,
        stdin=b"approved request",
        provider=ProviderId.CODEX,
        credential_home=credentials,
        timeout_seconds=5,
        max_input_bytes=1_024,
        max_output_bytes=2_048,
        runner=runner,
        source_environment={},
    )

    assert observed["argv"] == [sys.executable, str(FAKE_CLI), "echo"]
    assert observed["stdin"] == b"approved request"
    assert observed["check"] is True
    assert observed["max_input_bytes"] == 1_024
    assert observed["max_output_bytes"] == 2_048
    assert isinstance(observed["environment"], dict)
    assert result.stdout == b'{"ok":true}'
    assert result.input_sha256 == hashlib.sha256(b"approved request").hexdigest()
    assert result.stdout_sha256 == hashlib.sha256(b'{"ok":true}').hexdigest()


def test_current_platform_has_native_descendant_containment() -> None:
    require_process_containment()


def test_real_cli_fixture_receives_only_sanitized_environment_and_closed_input(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = _credential_home(tmp_path)
    result = run_cli_process(
        argv=(sys.executable, str(FAKE_CLI), "environment"),
        cwd=workspace,
        stdin=b"",
        provider=ProviderId.CODEX,
        credential_home=credentials,
        timeout_seconds=5,
        source_environment={"OPENAI_API_KEY": "secret", "TEMP": str(tmp_path)},
    )

    environment = json.loads(decode_cli_output(result.stdout))
    assert environment["CODEX_HOME"] == str(credentials.resolve())
    assert "OPENAI_API_KEY" not in environment


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("sleep", "timeout"),
        ("overflow", "response_limit"),
        ("nonzero", "process_nonzero"),
    ],
)
def test_process_failures_are_stable_and_never_reflect_diagnostics(
    tmp_path: Path, mode: str, code: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = _credential_home(tmp_path)
    with pytest.raises(CliProcessError, match=f"^{code}$") as caught:
        run_cli_process(
            argv=(sys.executable, str(FAKE_CLI), mode),
            cwd=workspace,
            stdin=b"",
            provider=ProviderId.CLAUDE_CODE,
            credential_home=credentials,
            timeout_seconds=0.3 if mode == "sleep" else 5,
            max_output_bytes=1_024,
            source_environment={},
        )
    assert "PRIVATE" not in str(caught.value)


def test_cancellation_and_invalid_utf8_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = _credential_home(tmp_path)
    with pytest.raises(CliProcessError, match="^cancelled$"):
        run_cli_process(
            argv=(sys.executable, str(FAKE_CLI), "sleep"),
            cwd=workspace,
            stdin=b"",
            provider=ProviderId.CODEX,
            credential_home=credentials,
            timeout_seconds=5,
            cancelled=lambda: True,
            source_environment={},
        )
    with pytest.raises(CliProcessError, match="^provider_protocol$"):
        decode_cli_output(b"\xff")


@pytest.mark.parametrize(
    ("transport_code", "cli_code"),
    [
        ("cleanup_failed", "process_containment_failed"),
        ("launch_failed", "process_launch_failed"),
        ("input_limit", "request_limit"),
        ("io_failed", "process_io_failed"),
    ],
)
def test_transport_errors_are_normalized_without_raw_details(
    tmp_path: Path, transport_code: str, cli_code: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = _credential_home(tmp_path)

    def runner(*args: object, **kwargs: object) -> ProbeProcessResult:
        raise ProbeProcessError(transport_code)

    with pytest.raises(CliProcessError, match=f"^{cli_code}$"):
        run_cli_process(
            argv=(sys.executable, str(FAKE_CLI), "echo"),
            cwd=workspace,
            stdin=b"",
            provider=ProviderId.CODEX,
            credential_home=credentials,
            timeout_seconds=5,
            runner=runner,
            source_environment={},
        )
