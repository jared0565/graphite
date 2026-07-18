from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from graphite.routing.claude_executor import (
    AdapterError,
    PROFILE_VERIFICATION_MARKER,
    execute_claude,
    execute_claude_profile_verification,
    preflight_claude,
)
from graphite.routing.contracts import Effort, PermissionMode, ProviderId
from graphite.routing.process_runner import CliProcessResult
from graphite.routing.process_runner import CliProcessError


def _result(stdout: bytes, stderr: bytes = b"") -> CliProcessResult:
    return CliProcessResult(
        0,
        stdout,
        stderr,
        0.25,
        hashlib.sha256(b"").hexdigest(),
        hashlib.sha256(stdout).hexdigest(),
        hashlib.sha256(stderr).hexdigest(),
    )


def _stream_result(payload: dict[str, object], *, model: str) -> bytes:
    return (
        json.dumps({"type": "assistant", "message": {"model": model}})
        + "\n"
        + json.dumps(payload)
        + "\n"
    ).encode()


class ScriptedTransport:
    def __init__(self, results: list[CliProcessResult]) -> None:
        self.results = results
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> CliProcessResult:
        self.calls.append(kwargs)
        return self.results.pop(0)


class FailingTransport:
    def __init__(self, code: str) -> None:
        self.code = code
        self.calls = 0

    def __call__(self, **kwargs: object) -> CliProcessResult:
        self.calls += 1
        raise CliProcessError(self.code)


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    executable = tmp_path / "bin" / "claude.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"trusted claude executable")
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    return executable, workspace, credentials


def test_preflight_uses_exact_commands_and_discards_pii(tmp_path: Path) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    transport = ScriptedTransport(
        [
            _result(b"2.1.208 (Claude Code)\n"),
            _result(
                json.dumps(
                    {
                        "loggedIn": True,
                        "authMethod": "claude.ai",
                        "apiProvider": "firstParty",
                        "email": "must-not-escape@example.test",
                        "organizationId": "secret-org",
                    }
                ).encode()
            ),
        ]
    )

    identity = preflight_claude(
        executable=executable,
        workspace=workspace,
        credential_home=credentials,
        transport=transport,
    )

    assert identity.provider is ProviderId.CLAUDE_CODE
    assert identity.cli_version == "2.1.208"
    assert identity.executable_sha256 == hashlib.sha256(executable.read_bytes()).hexdigest()
    assert "must-not-escape" not in repr(identity)
    assert [call["argv"] for call in transport.calls] == [
        (str(executable.resolve()), "--version"),
        (str(executable.resolve()), "auth", "status", "--json"),
    ]
    assert all(call["provider"] is ProviderId.CLAUDE_CODE for call in transport.calls)


@pytest.mark.parametrize(
    "auth",
    [
        {},
        {"loggedIn": False, "authMethod": "claude.ai", "apiProvider": "firstParty"},
        {"loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty"},
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"},
    ],
)
def test_preflight_rejects_non_subscription_auth(tmp_path: Path, auth: dict[str, object]) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    transport = ScriptedTransport(
        [_result(b"2.1.208 (Claude Code)"), _result(json.dumps(auth).encode())]
    )
    with pytest.raises(AdapterError, match="^auth_required$"):
        preflight_claude(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            transport=transport,
        )


def test_preflight_rejects_version_protocol_drift_before_auth(tmp_path: Path) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    transport = ScriptedTransport([_result(b"Claude Code version unknown")])
    with pytest.raises(AdapterError, match="^version$"):
        preflight_claude(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            transport=transport,
        )
    assert len(transport.calls) == 1


def test_execution_has_fixed_safe_argv_and_parses_one_terminal_result(tmp_path: Path) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Implemented and validated.",
        "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 120, "outputTokens": 33}},
        "usage": {"input_tokens": 120, "output_tokens": 33},
        "session_id": "must-be-discarded",
        "total_cost_usd": 123,
    }
    transport = ScriptedTransport(
        [_result(_stream_result(payload, model="claude-sonnet-4-6"))]
    )

    outcome = execute_claude(
        executable=executable,
        workspace=workspace,
        credential_home=credentials,
        prompt=b"make the approved edit",
        requested_model="sonnet",
        expected_effective_model="claude-sonnet-4-6",
        effort=Effort.HIGH,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        transport=transport,
    )

    assert outcome.effective_model == "claude-sonnet-4-6"
    assert outcome.input_tokens == 120
    assert outcome.output_tokens == 33
    assert outcome.message == "Implemented and validated."
    assert "session" not in repr(outcome)
    call = transport.calls[0]
    assert call["stdin"] == b"make the approved edit"
    assert call["argv"] == (
        str(executable.resolve()),
        "--safe-mode",
        "--no-chrome",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Read,Edit,Write",
        "--model",
        "sonnet",
        "--effort",
        "high",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "--input-format",
        "text",
        "--print",
    )
    forbidden = {"--fallback-model", "--resume", "--continue", "--dangerously-skip-permissions"}
    assert forbidden.isdisjoint(call["argv"])
    assert len(transport.calls) == 1


def test_execution_rejects_effective_model_mismatch_without_retry(tmp_path: Path) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    transport = ScriptedTransport(
        [
            _result(
                _stream_result(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "done",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                    model="different-model",
                )
            )
        ]
    )
    with pytest.raises(AdapterError, match="^model_mismatch$"):
        execute_claude(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            prompt=b"task",
            requested_model="sonnet",
            expected_effective_model="claude-sonnet-4-6",
            effort=Effort.HIGH,
            permission_mode=PermissionMode.READ_ONLY,
            transport=transport,
        )
    assert len(transport.calls) == 1


def test_execution_accepts_repeated_consistent_assistant_identity(tmp_path: Path) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    terminal = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "done",
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }
    assistant = json.dumps(
        {"type": "assistant", "message": {"model": "claude-sonnet-4-6"}}
    )
    payload = f"{assistant}\n{assistant}\n{json.dumps(terminal)}\n".encode()
    outcome = execute_claude(
        executable=executable,
        workspace=workspace,
        credential_home=credentials,
        prompt=b"task",
        requested_model="sonnet",
        expected_effective_model="claude-sonnet-4-6",
        effort=Effort.HIGH,
        permission_mode=PermissionMode.READ_ONLY,
        transport=ScriptedTransport([_result(payload)]),
    )
    assert outcome.effective_model == "claude-sonnet-4-6"


def test_profile_verification_uses_one_turn_and_exact_structured_output(
    tmp_path: Path,
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "free text is not verification authority",
        "structured_output": {"verification": PROFILE_VERIFICATION_MARKER},
        "usage": {"input_tokens": 2, "output_tokens": 8},
    }
    transport = ScriptedTransport(
        [_result(_stream_result(payload, model="claude-sonnet-5"))]
    )

    outcome = execute_claude_profile_verification(
        executable=executable,
        workspace=workspace,
        credential_home=credentials,
        prompt=b"verify the approved profile",
        requested_model="sonnet",
        expected_effective_model="claude-sonnet-5",
        effort=Effort.HIGH,
        permission_mode=PermissionMode.READ_ONLY,
        transport=transport,
    )

    assert outcome.message == PROFILE_VERIFICATION_MARKER
    argv = transport.calls[0]["argv"]
    schema_index = argv.index("--json-schema")
    assert argv[schema_index - 2 : schema_index] == ("--max-turns", "1")
    assert json.loads(argv[schema_index + 1]) == {
        "additionalProperties": False,
        "properties": {
            "verification": {
                "const": PROFILE_VERIFICATION_MARKER,
                "type": "string",
            }
        },
        "required": ["verification"],
        "type": "object",
    }
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "structured_output",
    (None, {}, {"verification": "wrong"}, {"verification": PROFILE_VERIFICATION_MARKER, "extra": True}),
)
def test_profile_verification_rejects_nonexact_structured_output(
    tmp_path: Path,
    structured_output: object,
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": PROFILE_VERIFICATION_MARKER,
        "structured_output": structured_output,
        "usage": {"input_tokens": 2, "output_tokens": 8},
    }
    transport = ScriptedTransport(
        [_result(_stream_result(payload, model="claude-sonnet-5"))]
    )

    with pytest.raises(AdapterError, match="^protocol$"):
        execute_claude_profile_verification(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            prompt=b"verify the approved profile",
            requested_model="sonnet",
            expected_effective_model="claude-sonnet-5",
            effort=Effort.HIGH,
            permission_mode=PermissionMode.READ_ONLY,
            transport=transport,
        )
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"not-json", "protocol"),
        (b'[{"type":"result"}]', "protocol"),
        (
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "done",
                    "modelUsage": {},
                }
            ).encode(),
            "model_identity_unverified",
        ),
    ],
)
def test_execution_fails_closed_on_malformed_results(
    tmp_path: Path, payload: bytes, code: str
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    with pytest.raises(AdapterError, match=f"^{code}$"):
        execute_claude(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            prompt=b"task",
            requested_model="sonnet",
            expected_effective_model="claude-sonnet-4-6",
            effort=Effort.HIGH,
            permission_mode=PermissionMode.READ_ONLY,
            transport=ScriptedTransport([_result(payload)]),
        )


@pytest.mark.parametrize(
    ("transport_code", "adapter_code"),
    [
        ("timeout", "timeout"),
        ("cancelled", "cancelled"),
        ("response_limit", "response_limit"),
        ("process_containment_failed", "containment"),
    ],
)
def test_execution_normalizes_transport_failure_without_retry(
    tmp_path: Path, transport_code: str, adapter_code: str
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    transport = FailingTransport(transport_code)
    with pytest.raises(AdapterError, match=f"^{adapter_code}$"):
        execute_claude(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            prompt=b"task",
            requested_model="sonnet",
            expected_effective_model="claude-sonnet-4-6",
            effort=Effort.HIGH,
            permission_mode=PermissionMode.READ_ONLY,
            transport=transport,
        )
    assert transport.calls == 1


def test_execution_normalizes_structured_quota_failure(tmp_path: Path) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    payload = {"type": "result", "subtype": "rate_limit", "is_error": True}
    with pytest.raises(AdapterError, match="^quota$"):
        execute_claude(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            prompt=b"task",
            requested_model="sonnet",
            expected_effective_model="claude-sonnet-4-6",
            effort=Effort.HIGH,
            permission_mode=PermissionMode.READ_ONLY,
            transport=ScriptedTransport([_result(json.dumps(payload).encode())]),
        )
