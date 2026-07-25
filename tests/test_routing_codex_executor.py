from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from graphite.routing.codex_executor import AdapterError, execute_codex, preflight_codex
from graphite.routing.contracts import Effort, PermissionMode, ProviderId
from graphite.routing.process_runner import CliProcessError, CliProcessResult


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
    executable = tmp_path / "bin" / "codex.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"trusted codex executable")
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    return executable, workspace, credentials


def _jsonl(*events: dict[str, object]) -> bytes:
    return ("\n".join(json.dumps(event) for event in events) + "\n").encode()


@pytest.mark.parametrize("status_on_stderr", [False, True])
def test_preflight_accepts_exact_chatgpt_status_from_one_stream(
    tmp_path: Path, status_on_stderr: bool
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    status = b"Logged in using ChatGPT\n"
    warning = b"WARNING: failed to clean up stale arg0 temp dirs: redacted\n"
    transport = ScriptedTransport(
        [
            _result(b"codex-cli 0.144.1\n"),
            _result(warning if status_on_stderr else status, status if status_on_stderr else warning),
        ]
    )
    identity = preflight_codex(
        executable=executable,
        workspace=workspace,
        credential_home=credentials,
        transport=transport,
    )
    assert identity.provider is ProviderId.CODEX
    assert identity.cli_version == "0.144.1"
    assert [call["argv"] for call in transport.calls] == [
        (str(executable.resolve()), "--version"),
        (str(executable.resolve()), "login", "status"),
    ]
    assert "warning" not in repr(identity)


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        (b"Logged in using ChatGPT\n", b"Logged in using ChatGPT\n"),
        (b"Logged in using ChatGPT\n", b"unexpected diagnostic\n"),
    ],
)
def test_preflight_rejects_ambiguous_or_unallowlisted_status_output(
    tmp_path: Path, stdout: bytes, stderr: bytes
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    transport = ScriptedTransport(
        [_result(b"codex-cli 0.144.1\n"), _result(stdout, stderr)]
    )
    with pytest.raises(AdapterError, match="^auth_required$"):
        preflight_codex(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            transport=transport,
        )


@pytest.mark.parametrize("status", [b"Not logged in\n", b"Logged in using API key\n", b""])
def test_preflight_rejects_non_chatgpt_auth(tmp_path: Path, status: bytes) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    transport = ScriptedTransport([_result(b"codex-cli 0.144.1"), _result(status)])
    with pytest.raises(AdapterError, match="^auth_required$"):
        preflight_codex(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            transport=transport,
        )


def test_preflight_rejects_version_protocol_drift_before_auth(tmp_path: Path) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    transport = ScriptedTransport([_result(b"codex development build")])
    with pytest.raises(AdapterError, match="^version$"):
        preflight_codex(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            transport=transport,
        )
    assert len(transport.calls) == 1


def test_execution_uses_canonical_argv_and_allowlisted_jsonl(tmp_path: Path) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    transport = ScriptedTransport(
        [
            _result(
                _jsonl(
                    {"type": "thread.started", "thread_id": "discard-me"},
                    {"type": "turn.started"},
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "Implemented safely."},
                    },
                    {
                        "type": "turn.completed",
                        "model": "gpt-5.6-codex",
                        "usage": {"input_tokens": 150, "output_tokens": 42},
                    },
                )
            )
        ]
    )

    outcome = execute_codex(
        executable=executable,
        workspace=workspace,
        credential_home=credentials,
        prompt=b"make the approved edit",
        requested_model="gpt-5.6-codex",
        expected_effective_model="gpt-5.6-codex",
        effort=Effort.XHIGH,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        transport=transport,
    )

    assert outcome.message == "Implemented safely."
    assert outcome.input_tokens == 150
    assert outcome.output_tokens == 42
    assert transport.calls[0]["argv"] == (
        str(executable.resolve()),
        "--strict-config",
        "-a",
        "never",
        "-s",
        "workspace-write",
        "-C",
        str(workspace.resolve()),
        "-m",
        "gpt-5.6-codex",
        "-c",
        'model_reasoning_effort="xhigh"',
        "-c",
        'windows.sandbox="elevated"',
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-",
    )
    forbidden = {"--dangerously-bypass-approvals-and-sandbox", "resume", "cloud", "--oss"}
    assert forbidden.isdisjoint(transport.calls[0]["argv"])
    assert len(transport.calls) == 1


def test_execution_read_only_argv_omits_windows_sandbox_binding(
    tmp_path: Path,
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    transport = ScriptedTransport(
        [
            _result(
                _jsonl(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "GRAPHITE_PROFILE_OK"},
                    },
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 5, "output_tokens": 2},
                    },
                )
            )
        ]
    )

    execute_codex(
        executable=executable,
        workspace=workspace,
        credential_home=credentials,
        prompt=b"verify",
        requested_model="gpt-5.6-sol",
        expected_effective_model="gpt-5.6-sol",
        effort=Effort.HIGH,
        permission_mode=PermissionMode.READ_ONLY,
        transport=transport,
    )

    assert transport.calls[0]["argv"] == (
        str(executable.resolve()),
        "--strict-config",
        "-a",
        "never",
        "-s",
        "read-only",
        "-C",
        str(workspace.resolve()),
        "-m",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="high"',
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-",
    )
    assert all("windows.sandbox" not in value for value in transport.calls[0]["argv"])


def test_execution_schema_edit_argv_binds_windows_sandbox_and_schema(
    tmp_path: Path,
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    schema = tmp_path / "edit-schema.json"
    schema_body = (
        b'{"additionalProperties":false,"properties":{"result":'
        b'{"const":"GRAPHITE_EDIT_OK","type":"string"}},'
        b'"required":["result"],"type":"object"}'
    )
    schema.write_bytes(schema_body)
    transport = ScriptedTransport(
        [
            _result(
                _jsonl(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": '{"result":"GRAPHITE_EDIT_OK"}',
                        },
                    },
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 9, "output_tokens": 4},
                    },
                )
            )
        ]
    )

    outcome = execute_codex(
        executable=executable,
        workspace=workspace,
        credential_home=credentials,
        prompt=b"make the approved bounded edit",
        requested_model="gpt-5.6-sol",
        expected_effective_model="gpt-5.6-sol",
        effort=Effort.HIGH,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        output_schema_path=schema,
        output_schema_sha256=hashlib.sha256(schema_body).hexdigest(),
        transport=transport,
    )

    assert outcome.message == '{"result":"GRAPHITE_EDIT_OK"}'
    assert transport.calls[0]["argv"] == (
        str(executable.resolve()),
        "--strict-config",
        "-a",
        "never",
        "-s",
        "workspace-write",
        "-C",
        str(workspace.resolve()),
        "-m",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="high"',
        "-c",
        'windows.sandbox="elevated"',
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--output-schema",
        str(schema.resolve()),
        "-",
    )


def test_execution_binds_full_requested_slug_when_terminal_omits_model(
    tmp_path: Path,
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    transport = ScriptedTransport(
        [
            _result(
                _jsonl(
                    {"type": "thread.started", "thread_id": "discard-me"},
                    {"type": "turn.started"},
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "verified"},
                    },
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 3, "output_tokens": 1},
                    },
                )
            )
        ]
    )
    outcome = execute_codex(
        executable=executable,
        workspace=workspace,
        credential_home=credentials,
        prompt=b"verify",
        requested_model="gpt-5.6-sol",
        expected_effective_model="gpt-5.6-sol",
        effort=Effort.HIGH,
        permission_mode=PermissionMode.READ_ONLY,
        transport=transport,
    )
    assert outcome.effective_model == "gpt-5.6-sol"
    assert (outcome.input_tokens, outcome.output_tokens) == (3, 1)


def test_execution_classifies_exact_terminal_capacity_message(
    tmp_path: Path,
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    transport = ScriptedTransport(
        [
            _result(
                _jsonl(
                    {"type": "thread.started", "thread_id": "discard-me"},
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": (
                                "Selected model is at capacity. "
                                "Please try a different model."
                            ),
                        },
                    },
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 3, "output_tokens": 8},
                    },
                )
            )
        ]
    )

    with pytest.raises(AdapterError, match="^capacity_unavailable$"):
        execute_codex(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            prompt=b"edit",
            requested_model="gpt-5.6-sol",
            expected_effective_model="gpt-5.6-sol",
            effort=Effort.HIGH,
            permission_mode=PermissionMode.WORKSPACE_WRITE,
            transport=transport,
        )

    assert len(transport.calls) == 1


def test_execution_does_not_classify_embedded_capacity_text(
    tmp_path: Path,
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    message = (
        "Not verified: selected model is at capacity. "
        "Please try a different model."
    )
    transport = ScriptedTransport(
        [
            _result(
                _jsonl(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": message},
                    },
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 3, "output_tokens": 9},
                    },
                )
            )
        ]
    )

    outcome = execute_codex(
        executable=executable,
        workspace=workspace,
        credential_home=credentials,
        prompt=b"edit",
        requested_model="gpt-5.6-sol",
        expected_effective_model="gpt-5.6-sol",
        effort=Effort.HIGH,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        transport=transport,
    )

    assert outcome.message == message


def test_execution_binds_external_output_schema_by_digest(tmp_path: Path) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    schema = tmp_path / "output-schema.json"
    schema_body = b'{"properties":{"result":{"type":"string"}},"type":"object"}'
    schema.write_bytes(schema_body)
    transport = ScriptedTransport(
        [
            _result(
                _jsonl(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "Intermediate progress must not be terminal.",
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": '{"result":"GRAPHITE_EDIT_OK"}',
                        },
                    },
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 4, "output_tokens": 7},
                    },
                )
            )
        ]
    )

    outcome = execute_codex(
        executable=executable,
        workspace=workspace,
        credential_home=credentials,
        prompt=b"edit",
        requested_model="gpt-5.6-sol",
        expected_effective_model="gpt-5.6-sol",
        effort=Effort.HIGH,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        output_schema_path=schema,
        output_schema_sha256=hashlib.sha256(schema_body).hexdigest(),
        transport=transport,
    )

    assert outcome.message == '{"result":"GRAPHITE_EDIT_OK"}'
    argv = transport.calls[0]["argv"]
    assert argv[-3:] == ("--output-schema", str(schema.resolve()), "-")


@pytest.mark.parametrize("case", ["inside_workspace", "digest_mismatch", "non_object"])
def test_execution_rejects_unbound_output_schema_before_transport(
    tmp_path: Path,
    case: str,
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    schema = (
        workspace / "schema.json"
        if case == "inside_workspace"
        else tmp_path / "schema.json"
    )
    body = b"[]" if case == "non_object" else b'{"type":"object"}'
    schema.write_bytes(body)
    expected = (
        "0" * 64
        if case == "digest_mismatch"
        else hashlib.sha256(body).hexdigest()
    )
    transport = ScriptedTransport([])

    with pytest.raises(
        AdapterError,
        match=(
            "^response_contract_mismatch$"
            if case == "digest_mismatch"
            else "^response_contract_invalid$"
        ),
    ):
        execute_codex(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            prompt=b"edit",
            requested_model="gpt-5.6-sol",
            expected_effective_model="gpt-5.6-sol",
            effort=Effort.HIGH,
            permission_mode=PermissionMode.WORKSPACE_WRITE,
            output_schema_path=schema,
            output_schema_sha256=expected,
            transport=transport,
        )

    assert not transport.calls


def test_execution_rejects_output_schema_drift_after_transport(
    tmp_path: Path,
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    schema = tmp_path / "schema.json"
    body = b'{"type":"object"}'
    schema.write_bytes(body)
    result = _result(
        _jsonl(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"result":"ok"}'},
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        )
    )

    def transport(**_kwargs: object) -> CliProcessResult:
        schema.write_bytes(b'{"type":"string"}')
        return result

    with pytest.raises(AdapterError, match="^response_contract_changed$"):
        execute_codex(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            prompt=b"edit",
            requested_model="gpt-5.6-sol",
            expected_effective_model="gpt-5.6-sol",
            effort=Effort.HIGH,
            permission_mode=PermissionMode.WORKSPACE_WRITE,
            output_schema_path=schema,
            output_schema_sha256=hashlib.sha256(body).hexdigest(),
            transport=transport,
        )


@pytest.mark.parametrize(
    ("events", "code"),
    [
        (({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},), "protocol"),
        (
            (
                {"type": "turn.completed", "model": "other", "usage": {"input_tokens": 1, "output_tokens": 1}},
            ),
            "model_mismatch",
        ),
        (
            (
                {"type": "turn.completed", "model": "gpt-5.6-codex", "usage": {"input_tokens": 1, "output_tokens": 1}},
                {"type": "turn.completed", "model": "gpt-5.6-codex", "usage": {"input_tokens": 1, "output_tokens": 1}},
            ),
            "protocol",
        ),
        (
            (
                {"type": "turn.completed", "model": "gpt-5.6-codex", "usage": {"input_tokens": 1, "output_tokens": 1}},
                {"type": "turn.started"},
            ),
            "protocol",
        ),
    ],
)
def test_execution_rejects_unproved_or_nonterminal_identity_without_retry(
    tmp_path: Path, events: tuple[dict[str, object], ...], code: str
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    transport = ScriptedTransport([_result(_jsonl(*events))])
    with pytest.raises(AdapterError, match=f"^{code}$"):
        execute_codex(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            prompt=b"task",
            requested_model="gpt-5.6-codex",
            expected_effective_model="gpt-5.6-codex",
            effort=Effort.XHIGH,
            permission_mode=PermissionMode.READ_ONLY,
            transport=transport,
        )
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("transport_code", "adapter_code"),
    [
        ("timeout", "timeout"),
        ("cancelled", "cancelled"),
        ("response_limit", "response_limit"),
        ("process_containment_unavailable", "containment"),
    ],
)
def test_execution_normalizes_transport_failure_without_retry(
    tmp_path: Path, transport_code: str, adapter_code: str
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    transport = FailingTransport(transport_code)
    with pytest.raises(AdapterError, match=f"^{adapter_code}$"):
        execute_codex(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            prompt=b"task",
            requested_model="gpt-5.6-codex",
            expected_effective_model="gpt-5.6-codex",
            effort=Effort.XHIGH,
            permission_mode=PermissionMode.READ_ONLY,
            transport=transport,
        )
    assert transport.calls == 1


def test_execution_normalizes_structured_rate_limit_failure(tmp_path: Path) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    events = ({"type": "turn.failed", "error": {"code": "rate_limit"}},)
    with pytest.raises(AdapterError, match="^quota$"):
        execute_codex(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            prompt=b"task",
            requested_model="gpt-5.6-codex",
            expected_effective_model="gpt-5.6-codex",
            effort=Effort.XHIGH,
            permission_mode=PermissionMode.READ_ONLY,
            transport=ScriptedTransport([_result(_jsonl(*events))]),
        )


def test_execution_normalizes_usage_limit_failure_as_quota(tmp_path: Path) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    events = (
        {
            "type": "error",
            "message": (
                "You've hit your usage limit. Visit "
                "https://chatgpt.com/codex/settings/usage to purchase more "
                "credits or try again at Jul 29th, 2026 9:40 AM."
            ),
        },
    )
    with pytest.raises(AdapterError, match="^quota$"):
        execute_codex(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            prompt=b"task",
            requested_model="gpt-5.6-codex",
            expected_effective_model="gpt-5.6-codex",
            effort=Effort.XHIGH,
            permission_mode=PermissionMode.READ_ONLY,
            transport=ScriptedTransport([_result(_jsonl(*events))]),
        )
