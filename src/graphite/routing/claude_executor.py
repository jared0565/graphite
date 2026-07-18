"""Hardened adapter for an authenticated Claude Code subscription CLI."""
from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .contracts import CliIdentity, Effort, PermissionMode, ProviderId
from .process_runner import (
    CliProcessError,
    CliProcessResult,
    decode_cli_output,
    run_cli_process,
)

ADAPTER_PROTOCOL_VERSION: Final = "1.0.0"
PREFLIGHT_TIMEOUT_SECONDS: Final = 15.0
EXECUTION_TIMEOUT_SECONDS: Final = 1_800.0
MAX_EXECUTABLE_BYTES: Final = 512 * 1024 * 1024
MAX_MESSAGE_LENGTH: Final = 1_048_576
MAX_TOKEN_COUNT: Final = 10_000_000
PROFILE_VERIFICATION_MARKER: Final = "GRAPHITE_PROFILE_OK"
MAX_EVENT_COUNT: Final = 10_000
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*) \(Claude Code\)\r?\n?$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_TRANSPORT_ERRORS: Final = {
    "timeout": "timeout",
    "cancelled": "cancelled",
    "response_limit": "response_limit",
    "process_containment_unavailable": "containment",
    "process_containment_failed": "containment",
}


class AdapterError(RuntimeError):
    """Stable adapter failure that contains no provider diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ClaudeExecutionResult:
    effective_model: str
    message: str = field(repr=False)
    input_tokens: int | None
    output_tokens: int | None
    duration_seconds: float
    input_sha256: str
    stdout_sha256: str
    stderr_sha256: str


Transport = Callable[..., CliProcessResult]


def _canonical_file(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise AdapterError("executable_invalid")
    try:
        link_metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise AdapterError("executable_invalid") from None
    if (
        not stat.S_ISREG(link_metadata.st_mode)
        or stat.S_ISLNK(link_metadata.st_mode)
        or bool(getattr(link_metadata, "st_file_attributes", 0) & _REPARSE_POINT)
        or link_metadata.st_size <= 0
        or link_metadata.st_size > MAX_EXECUTABLE_BYTES
    ):
        raise AdapterError("executable_invalid")
    return resolved


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        raise AdapterError("executable_invalid") from None
    return digest.hexdigest()


def _invoke(transport: Transport, **kwargs: object) -> CliProcessResult:
    try:
        result = transport(**kwargs)
    except CliProcessError as exc:
        raise AdapterError(_TRANSPORT_ERRORS.get(exc.code, "unavailable")) from None
    except AdapterError:
        raise
    except Exception:
        raise AdapterError("unavailable") from None
    if not isinstance(result, CliProcessResult) or result.returncode != 0:
        raise AdapterError("unavailable")
    return result


def _common_call(
    *, executable: Path, workspace: Path, credential_home: Path | None
) -> dict[str, object]:
    return {
        "cwd": workspace,
        "provider": ProviderId.CLAUDE_CODE,
        "credential_home": credential_home,
        "timeout_seconds": PREFLIGHT_TIMEOUT_SECONDS,
    }


def preflight_claude(
    *,
    executable: Path,
    workspace: Path,
    credential_home: Path | None,
    transport: Transport = run_cli_process,
) -> CliIdentity:
    """Verify executable identity and Claude subscription authentication."""
    resolved = _canonical_file(executable)
    executable_digest = _file_sha256(resolved)
    common = _common_call(
        executable=resolved, workspace=workspace, credential_home=credential_home
    )
    version_result = _invoke(
        transport, argv=(str(resolved), "--version"), stdin=b"", **common
    )
    try:
        version_text = decode_cli_output(version_result.stdout)
    except CliProcessError:
        raise AdapterError("version") from None
    match = _VERSION.fullmatch(version_text)
    if match is None:
        raise AdapterError("version")
    version = ".".join(match.groups())
    auth_result = _invoke(
        transport,
        argv=(str(resolved), "auth", "status", "--json"),
        stdin=b"",
        **common,
    )
    try:
        auth = json.loads(decode_cli_output(auth_result.stdout))
    except (json.JSONDecodeError, CliProcessError):
        raise AdapterError("auth_required") from None
    if (
        not isinstance(auth, dict)
        or auth.get("loggedIn") is not True
        or auth.get("authMethod") != "claude.ai"
        or auth.get("apiProvider") != "firstParty"
    ):
        raise AdapterError("auth_required")
    if _file_sha256(resolved) != executable_digest:
        raise AdapterError("version")
    return CliIdentity(
        ProviderId.CLAUDE_CODE,
        executable_digest,
        version,
        ADAPTER_PROTOCOL_VERSION,
    )


def _identifier(value: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise AdapterError("request_invalid")
    return value


def _token(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKEN_COUNT:
        raise AdapterError("protocol")
    return value


def _parse_result(
    stdout: bytes,
    expected_model: str,
    expected_structured_output: dict[str, object] | None = None,
) -> tuple[str, str, int, int]:
    try:
        lines = decode_cli_output(stdout).splitlines()
    except CliProcessError:
        raise AdapterError("protocol") from None
    if not lines or len(lines) > MAX_EVENT_COUNT or any(not line.strip() for line in lines):
        raise AdapterError("protocol")
    events: list[dict[str, object]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise AdapterError("protocol") from None
        if not isinstance(event, dict) or event.get("type") not in {
            "system", "assistant", "user", "result", "rate_limit_event"
        }:
            raise AdapterError("protocol")
        events.append(event)
    payload = events[-1]
    if payload.get("type") != "result" or any(
        event.get("type") == "result" for event in events[:-1]
    ):
        raise AdapterError("protocol")
    if payload.get("subtype") != "success" or payload.get("is_error") is not False:
        subtype = str(payload.get("subtype", "")).lower()
        if "auth" in subtype or "login" in subtype:
            raise AdapterError("auth_required")
        if "quota" in subtype or "rate" in subtype or "limit" in subtype:
            raise AdapterError("quota")
        raise AdapterError("unavailable")
    if expected_structured_output is None:
        message = payload.get("result")
        if not isinstance(message, str) or not message or len(message) > MAX_MESSAGE_LENGTH:
            raise AdapterError("protocol")
    else:
        if payload.get("structured_output") != expected_structured_output:
            raise AdapterError("protocol")
        message = PROFILE_VERIFICATION_MARKER
    assistant_models: list[str] = []
    for event in events[:-1]:
        if event.get("type") != "assistant":
            continue
        assistant = event.get("message")
        model = assistant.get("model") if isinstance(assistant, dict) else None
        if not isinstance(model, str) or _IDENTIFIER.fullmatch(model) is None:
            raise AdapterError("model_identity_unverified")
        assistant_models.append(model)
    if not assistant_models:
        raise AdapterError("model_identity_unverified")
    if any(model != expected_model for model in assistant_models):
        raise AdapterError("model_mismatch")
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        raise AdapterError("protocol")
    return (
        message,
        expected_model,
        _token(usage.get("input_tokens")),
        _token(usage.get("output_tokens")),
    )


def execute_claude(
    *,
    executable: Path,
    workspace: Path,
    credential_home: Path | None,
    prompt: bytes,
    requested_model: str,
    expected_effective_model: str,
    effort: Effort,
    permission_mode: PermissionMode,
    transport: Transport = run_cli_process,
    timeout_seconds: float = EXECUTION_TIMEOUT_SECONDS,
    _verification_marker: str | None = None,
) -> ClaudeExecutionResult:
    """Execute exactly one noninteractive, non-resumable Claude task."""
    resolved = _canonical_file(executable)
    requested = _identifier(requested_model)
    expected = _identifier(expected_effective_model)
    try:
        normalized_effort = Effort(effort)
        permission = PermissionMode(permission_mode)
    except (TypeError, ValueError):
        raise AdapterError("request_invalid") from None
    if normalized_effort is Effort.DEFAULT:
        raise AdapterError("request_invalid")
    permission_value = "acceptEdits" if permission is PermissionMode.WORKSPACE_WRITE else "plan"
    allowed_tools = "Read,Edit,Write" if permission is PermissionMode.WORKSPACE_WRITE else "Read"
    structured_output: dict[str, object] | None = None
    structured_args: tuple[str, ...] = ()
    if _verification_marker is not None:
        if (
            _verification_marker != PROFILE_VERIFICATION_MARKER
            or permission is not PermissionMode.READ_ONLY
        ):
            raise AdapterError("request_invalid")
        structured_output = {"verification": PROFILE_VERIFICATION_MARKER}
        schema = {
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
        structured_args = (
            "--max-turns",
            "1",
            "--json-schema",
            json.dumps(schema, sort_keys=True, separators=(",", ":")),
        )
    argv = (
        str(resolved),
        "--safe-mode",
        "--no-chrome",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--permission-mode",
        permission_value,
        "--allowedTools",
        allowed_tools,
        "--model",
        requested,
        "--effort",
        normalized_effort.value,
        *structured_args,
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "--input-format",
        "text",
        "--print",
    )
    result = _invoke(
        transport,
        argv=argv,
        cwd=workspace,
        stdin=prompt,
        provider=ProviderId.CLAUDE_CODE,
        credential_home=credential_home,
        timeout_seconds=timeout_seconds,
    )
    message, effective_model, input_tokens, output_tokens = _parse_result(
        result.stdout, expected, structured_output
    )
    return ClaudeExecutionResult(
        effective_model,
        message,
        input_tokens,
        output_tokens,
        result.duration_seconds,
        result.input_sha256,
        result.stdout_sha256,
        result.stderr_sha256,
    )


def execute_claude_profile_verification(
    *,
    executable: Path,
    workspace: Path,
    credential_home: Path | None,
    prompt: bytes,
    requested_model: str,
    expected_effective_model: str,
    effort: Effort,
    permission_mode: PermissionMode,
    transport: Transport = run_cli_process,
    timeout_seconds: float = EXECUTION_TIMEOUT_SECONDS,
) -> ClaudeExecutionResult:
    """Run one read-only, schema-constrained capability verification turn."""
    return execute_claude(
        executable=executable,
        workspace=workspace,
        credential_home=credential_home,
        prompt=prompt,
        requested_model=requested_model,
        expected_effective_model=expected_effective_model,
        effort=effort,
        permission_mode=permission_mode,
        transport=transport,
        timeout_seconds=timeout_seconds,
        _verification_marker=PROFILE_VERIFICATION_MARKER,
    )
