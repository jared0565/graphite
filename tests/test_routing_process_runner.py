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


def _require_symlinks(tmp_path: Path) -> None:
    """Skip loudly if this machine cannot make symlinks.

    Windows needs SeCreateSymbolicLinkPrivilege (or Developer Mode). It is
    available on the GitHub windows runners and on the dev machine, so this
    should almost never fire -- and it names the reason when it does, because a
    silent skip on the gating platform is a check that only looks like one.
    """
    probe = tmp_path / "_symlink_probe"
    probe.mkdir()
    target = probe / "target"
    target.write_text("x", encoding="utf-8")
    try:
        (probe / "link").symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - privilege-dependent
        pytest.skip(f"symlinks unavailable on this machine: {exc}")


def test_a_symlinked_interpreter_is_accepted(tmp_path: Path) -> None:
    """On POSIX essentially every interpreter is a symlink -- `/usr/bin/python3`
    -> `python3.12`, and a venv's `bin/python` -> the system binary. Measured on
    Ubuntu 24.04: `sys.executable` itself is one.

    The old guard rejected any symlink via `lstat`, so `build_cli_environment`
    raised `executable_invalid` for `sys.executable`, and graphite could not
    launch a single CLI provider on Linux or macOS. 32 tests in this file failed
    identically on both platforms for this one reason.
    """
    _require_symlinks(tmp_path)
    real_bin = tmp_path / "realbin"
    real_bin.mkdir()
    target = real_bin / "tool"
    target.write_bytes(b"binary")
    link_dir = tmp_path / "linkbin"
    link_dir.mkdir()
    link = link_dir / "tool-link"
    link.symlink_to(target)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    environment = build_cli_environment(
        provider=ProviderId.CODEX,
        executable=link,
        workspace=workspace,
        credential_home=_credential_home(tmp_path),
    )

    assert environment, "a symlinked executable outside the workspace must be usable"


def test_a_symlink_pointing_into_the_workspace_is_still_rejected(tmp_path: Path) -> None:
    """THE test for the change above. The property the symlink ban was standing
    in for is "never execute anything inside the agent-controlled workspace",
    and that is enforced by resolving first and containment-checking the
    RESOLVED path.

    If this passes, the containment check is not following the link and dropping
    the ban really did remove a protection -- so this failing is the only thing
    that makes the relaxation safe.
    """
    _require_symlinks(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    planted = workspace / "evil"
    planted.write_bytes(b"binary")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = outside / "innocent-looking"
    link.symlink_to(planted)

    with pytest.raises(CliProcessError, match="^executable_invalid$"):
        build_cli_environment(
            provider=ProviderId.CODEX,
            executable=link,
            workspace=workspace,
            credential_home=_credential_home(tmp_path),
        )


def test_a_symlink_to_a_directory_is_rejected(tmp_path: Path) -> None:
    """Dropping the `lstat` symlink ban must not cost the "is it a regular file"
    property. Validating the RESOLVED target keeps it; validating nothing would
    let a symlink to a directory or a fifo through."""
    _require_symlinks(tmp_path)
    real_dir = tmp_path / "somedir"
    real_dir.mkdir()
    link_dir = tmp_path / "linkbin"
    link_dir.mkdir()
    link = link_dir / "tool-link"
    link.symlink_to(real_dir, target_is_directory=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(CliProcessError, match="^executable_invalid$"):
        build_cli_environment(
            provider=ProviderId.CODEX,
            executable=link,
            workspace=workspace,
            credential_home=_credential_home(tmp_path),
        )


def test_a_symlinked_workspace_directory_is_accepted(tmp_path: Path) -> None:
    """`_canonical_directory` carried the identical defect. It matters most on
    macOS, where `/tmp` -> `/private/tmp` and `/var` -> `/private/var`, so a
    workspace under the system temp root can have a symlinked component and be
    refused `workspace_invalid` before the executable is even looked at."""
    _require_symlinks(tmp_path)
    real_workspace = tmp_path / "real-workspace"
    real_workspace.mkdir()
    workspace = tmp_path / "workspace-link"
    workspace.symlink_to(real_workspace, target_is_directory=True)
    executable = tmp_path / "bin" / "tool"
    executable.parent.mkdir()
    executable.write_bytes(b"binary")

    environment = build_cli_environment(
        provider=ProviderId.CODEX,
        executable=executable,
        workspace=workspace,
        credential_home=_credential_home(tmp_path),
    )

    assert environment, "a symlinked workspace path must be usable"


def test_cli_transport_rejects_non_cli_provider(tmp_path: Path) -> None:
    executable = tmp_path / "bin" / "tool.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"binary")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(CliProcessError, match="^provider_invalid$"):
        build_cli_environment(
            provider=ProviderId.OPENROUTER,
            executable=executable,
            workspace=workspace,
            credential_home=None,
        )
    with pytest.raises(CliProcessError, match="^provider_invalid$"):
        run_cli_process(
            argv=(str(executable),),
            cwd=workspace,
            stdin=b"",
            provider=ProviderId.OPENROUTER,
            credential_home=None,
            timeout_seconds=5.0,
        )


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
    assert observed["check"] is False
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


def test_nonzero_result_preserves_only_allowlisted_hashed_diagnostics(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = _credential_home(tmp_path)
    stdout = b'PRIVATE structured provider output {"type":"result"}'
    stderr = b"PRIVATE provider diagnostic with a path and credential metadata"

    def runner(argv: list[str], **kwargs: object) -> ProbeProcessResult:
        assert kwargs["check"] is False
        return ProbeProcessResult(23, stdout, stderr, 45.8)

    with pytest.raises(CliProcessError, match="^process_nonzero$") as caught:
        run_cli_process(
            argv=(sys.executable, str(FAKE_CLI), "echo"),
            cwd=workspace,
            stdin=b"PRIVATE approved prompt",
            provider=ProviderId.CLAUDE_CODE,
            credential_home=credentials,
            timeout_seconds=60,
            runner=runner,
            source_environment={},
        )

    diagnostics = caught.value.diagnostics
    assert diagnostics is not None
    assert diagnostics.to_dict() == {
        "exit_classification": "nonzero_exit",
        "exit_code": 23,
        "duration_seconds": 45.8,
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "failure_category": "provider_process_failure",
    }
    serialized = repr(diagnostics.to_dict()) + repr(caught.value) + str(caught.value)
    assert "PRIVATE" not in serialized
    assert set(diagnostics.to_dict()) == {
        "exit_classification",
        "exit_code",
        "duration_seconds",
        "stdout_sha256",
        "stderr_sha256",
        "failure_category",
    }


@pytest.mark.parametrize(
    ("provider", "diagnostic"),
    [
        (
            ProviderId.CLAUDE_CODE,
            b"Selected model is at capacity. Please try a different model.",
        ),
        (
            ProviderId.CODEX,
            b"Selected model is at capacity. Please try a different model.",
        ),
    ],
)
def test_exact_allowlisted_capacity_diagnostic_is_sanitized(
    tmp_path: Path,
    provider: ProviderId,
    diagnostic: bytes,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = _credential_home(tmp_path)

    def runner(argv: list[str], **kwargs: object) -> ProbeProcessResult:
        return ProbeProcessResult(1, b"", diagnostic, 0.25)

    with pytest.raises(CliProcessError, match="^process_nonzero$") as caught:
        run_cli_process(
            argv=(sys.executable, str(FAKE_CLI), "echo"),
            cwd=workspace,
            stdin=b"private prompt",
            provider=provider,
            credential_home=credentials,
            timeout_seconds=5,
            runner=runner,
            source_environment={},
        )

    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics.failure_category == "capacity_unavailable"
    assert diagnostic.decode("ascii") not in repr(caught.value.diagnostics)


def test_ambiguous_capacity_text_does_not_authorize_fallback(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = _credential_home(tmp_path)

    def runner(argv: list[str], **kwargs: object) -> ProbeProcessResult:
        return ProbeProcessResult(
            1,
            b"",
            b"Capacity warning: perhaps select another model later.",
            0.25,
        )

    with pytest.raises(CliProcessError) as caught:
        run_cli_process(
            argv=(sys.executable, str(FAKE_CLI), "echo"),
            cwd=workspace,
            stdin=b"",
            provider=ProviderId.CLAUDE_CODE,
            credential_home=credentials,
            timeout_seconds=5,
            runner=runner,
            source_environment={},
        )

    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics.failure_category == "provider_process_failure"


def test_capacity_phrase_embedded_in_other_diagnostic_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = _credential_home(tmp_path)

    def runner(argv: list[str], **kwargs: object) -> ProbeProcessResult:
        return ProbeProcessResult(
            1,
            b"",
            b"Not verified: selected model is at capacity. please try a different model.",
            0.25,
        )

    with pytest.raises(CliProcessError) as caught:
        run_cli_process(
            argv=(sys.executable, str(FAKE_CLI), "echo"),
            cwd=workspace,
            stdin=b"",
            provider=ProviderId.CLAUDE_CODE,
            credential_home=credentials,
            timeout_seconds=5,
            runner=runner,
            source_environment={},
        )

    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics.failure_category == "provider_process_failure"


def _classified_category(
    tmp_path: Path,
    provider: ProviderId,
    stdout: bytes,
    stderr: bytes = b"",
) -> str:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    credentials = _credential_home(tmp_path)

    def runner(argv: list[str], **kwargs: object) -> ProbeProcessResult:
        return ProbeProcessResult(1, stdout, stderr, 0.25)

    with pytest.raises(CliProcessError, match="^process_nonzero$") as caught:
        run_cli_process(
            argv=(sys.executable, str(FAKE_CLI), "echo"),
            cwd=workspace,
            stdin=b"",
            provider=provider,
            credential_home=credentials,
            timeout_seconds=5,
            runner=runner,
            source_environment={},
        )
    diagnostics = caught.value.diagnostics
    assert diagnostics is not None
    return diagnostics.failure_category


_CODEX_USAGE_LIMIT_EVENT = (
    b'{"type":"error","message":"You\'ve hit your usage limit. Visit '
    b"https://chatgpt.com/codex/settings/usage to purchase more credits or "
    b'try again at Jul 29th, 2026 9:40 AM."}'
)


def test_codex_usage_limit_error_event_classifies_as_capacity(tmp_path: Path) -> None:
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, _CODEX_USAGE_LIMIT_EVENT)
        == "capacity_unavailable"
    )


def test_codex_turn_failed_rate_limit_classifies_as_capacity(tmp_path: Path) -> None:
    stdout = b'{"type":"turn.failed","error":{"code":"rate_limit"}}'
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "capacity_unavailable"
    )


def test_codex_quota_event_amid_noise_still_classifies(tmp_path: Path) -> None:
    stdout = (
        b"WARNING: skills context budget exceeded\n"
        + _CODEX_USAGE_LIMIT_EVENT
        + b"\nnot json trailing line"
    )
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "capacity_unavailable"
    )


def test_codex_error_event_without_quota_markers_fails_closed(tmp_path: Path) -> None:
    stdout = b'{"type":"error","message":"internal server error"}'
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "provider_process_failure"
    )


def test_codex_agent_message_with_marker_text_fails_closed(tmp_path: Path) -> None:
    stdout = (
        b'{"type":"item.completed","item":{"type":"agent_message",'
        b'"text":"the api rate limit is 10 requests per minute"}}'
    )
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "provider_process_failure"
    )


def test_codex_quota_event_on_stderr_only_fails_closed(tmp_path: Path) -> None:
    assert (
        _classified_category(
            tmp_path, ProviderId.CODEX, b"", stderr=_CODEX_USAGE_LIMIT_EVENT
        )
        == "provider_process_failure"
    )


def test_codex_non_json_stdout_fails_closed(tmp_path: Path) -> None:
    stdout = b"plain text mentioning usage limit\nsecond line"
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "provider_process_failure"
    )


def test_codex_undecodable_stdout_fails_closed(tmp_path: Path) -> None:
    stdout = b"\xff\xfe" + _CODEX_USAGE_LIMIT_EVENT
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "provider_process_failure"
    )


def test_claude_quota_subtype_result_classifies_as_capacity(tmp_path: Path) -> None:
    stdout = b'{"type":"result","subtype":"error_rate_limit","is_error":true}'
    assert (
        _classified_category(tmp_path, ProviderId.CLAUDE_CODE, stdout)
        == "capacity_unavailable"
    )


def test_claude_marker_only_in_result_text_fails_closed(tmp_path: Path) -> None:
    stdout = (
        b'{"type":"result","subtype":"error_during_execution","is_error":true,'
        b'"result":"I hit a rate limit while calling the api"}'
    )
    assert (
        _classified_category(tmp_path, ProviderId.CLAUDE_CODE, stdout)
        == "provider_process_failure"
    )


def test_claude_informational_rate_limit_event_fails_closed(tmp_path: Path) -> None:
    stdout = b'{"type":"rate_limit_event","rate_limit":{"status":"warning"}}'
    assert (
        _classified_category(tmp_path, ProviderId.CLAUDE_CODE, stdout)
        == "provider_process_failure"
    )


def test_claude_success_result_never_classifies_as_capacity(tmp_path: Path) -> None:
    stdout = b'{"type":"result","subtype":"success","is_error":false}'
    assert (
        _classified_category(tmp_path, ProviderId.CLAUDE_CODE, stdout)
        == "provider_process_failure"
    )


def test_codex_hostile_json_line_does_not_abort_scan(tmp_path: Path) -> None:
    huge_integer_line = b"9" * 5000
    stdout = huge_integer_line + b"\n" + _CODEX_USAGE_LIMIT_EVENT
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "capacity_unavailable"
    )


def test_codex_deeply_nested_json_line_does_not_abort_scan(tmp_path: Path) -> None:
    nested_line = b"[" * 100_000
    stdout = nested_line + b"\n" + _CODEX_USAGE_LIMIT_EVENT
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "capacity_unavailable"
    )


def test_codex_usage_limit_underscore_marker_classifies_as_capacity(
    tmp_path: Path,
) -> None:
    stdout = b'{"type":"turn.failed","error":{"code":"usage_limit_reached"}}'
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "capacity_unavailable"
    )


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
