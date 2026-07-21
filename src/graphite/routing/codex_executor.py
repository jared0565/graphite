"""Hardened adapter for an authenticated Codex ChatGPT subscription CLI."""
from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .claude_executor import AdapterError, _canonical_file, _file_sha256, _identifier
from .cli_identity import CliIdentityPrimitiveError, parse_semantic_version_output
from .contracts import CliIdentity, Effort, PermissionMode, ProviderId
from .process_runner import (
    CliProcessError,
    CliProcessResult,
    decode_cli_output,
    run_cli_process,
)

ADAPTER_PROTOCOL_VERSION: Final = "1.0.0"
# Windows write authority depends on Codex's [windows] sandbox setting, which
# normally lives in user config. `--ignore-user-config` strips it, so a
# workspace-write execution must bind the sandbox mode explicitly in argv or
# every write tool call is denied under `-a never` and the edit becomes a
# silent no-op. The binding is passed on every platform for one deterministic
# argv contract; non-Windows Codex ignores the [windows] section.
WINDOWS_SANDBOX_MODE: Final = "elevated"
PREFLIGHT_TIMEOUT_SECONDS: Final = 15.0
EXECUTION_TIMEOUT_SECONDS: Final = 1_800.0
MAX_EVENT_COUNT: Final = 10_000
MAX_MESSAGE_LENGTH: Final = 1_048_576
MAX_TOKEN_COUNT: Final = 10_000_000
MAX_OUTPUT_SCHEMA_BYTES: Final = 65_536
_VERSION = re.compile(r"^codex-cli (0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\r?\n?$")
_ALLOWED_EVENTS: Final = frozenset(
    {
        "thread.started",
        "turn.started",
        "item.started",
        "item.updated",
        "item.completed",
        "turn.completed",
        "turn.failed",
        "error",
    }
)
_TRANSPORT_ERRORS: Final = {
    "timeout": "timeout",
    "cancelled": "cancelled",
    "response_limit": "response_limit",
    "process_containment_unavailable": "containment",
    "process_containment_failed": "containment",
}
_PREFLIGHT_WARNING_PREFIXES: Final = (
    "WARNING: failed to clean up stale arg0 temp dirs:",
    "WARNING: proceeding, even though we could not create PATH aliases:",
)
_CAPACITY_MESSAGE: Final = "selected model is at capacity. please try a different model."
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True, slots=True)
class CodexExecutionResult:
    effective_model: str
    message: str = field(repr=False)
    input_tokens: int | None
    output_tokens: int | None
    duration_seconds: float
    input_sha256: str
    stdout_sha256: str
    stderr_sha256: str


Transport = Callable[..., CliProcessResult]


def _is_reparse(metadata: object) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _output_schema(
    path: Path | None,
    expected_sha256: str | None,
    *,
    workspace: Path,
) -> tuple[Path, str] | None:
    if path is None and expected_sha256 is None:
        return None
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or not isinstance(expected_sha256, str)
        or _SHA256.fullmatch(expected_sha256) is None
    ):
        raise AdapterError("request_invalid")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        resolved_metadata = resolved.stat()
        workspace_root = workspace.resolve(strict=True)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse(metadata)
            or not stat.S_ISREG(resolved_metadata.st_mode)
            or _is_reparse(resolved_metadata)
            or not 0 < metadata.st_size <= MAX_OUTPUT_SCHEMA_BYTES
            or resolved.is_relative_to(workspace_root)
        ):
            raise OSError
        body = resolved.read_bytes()
        parsed = json.loads(body.decode("utf-8"))
    except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
        raise AdapterError("response_contract_invalid") from None
    if not isinstance(parsed, dict):
        raise AdapterError("response_contract_invalid")
    digest = hashlib.sha256(body).hexdigest()
    if digest != expected_sha256:
        raise AdapterError("response_contract_mismatch")
    return resolved, digest


def _invoke(transport: Transport, **kwargs: object) -> CliProcessResult:
    try:
        result = transport(**kwargs)
    except CliProcessError as exc:
        raise AdapterError(
            _TRANSPORT_ERRORS.get(exc.code, "unavailable"),
            process_diagnostics=exc.diagnostics,
        ) from None
    except AdapterError:
        raise
    except Exception:
        raise AdapterError("unavailable") from None
    if not isinstance(result, CliProcessResult) or result.returncode != 0:
        raise AdapterError("unavailable")
    return result


def preflight_codex(
    *,
    executable: Path,
    workspace: Path,
    credential_home: Path | None,
    transport: Transport = run_cli_process,
) -> CliIdentity:
    """Verify executable identity and ChatGPT subscription authentication."""
    resolved = _canonical_file(executable, workspace=workspace)
    executable_digest = _file_sha256(resolved)
    common = {
        "cwd": workspace,
        "provider": ProviderId.CODEX,
        "credential_home": credential_home,
        "timeout_seconds": PREFLIGHT_TIMEOUT_SECONDS,
    }
    version_result = _invoke(
        transport, argv=(str(resolved), "--version"), stdin=b"", **common
    )
    try:
        version_text = decode_cli_output(version_result.stdout)
    except CliProcessError:
        raise AdapterError("version") from None
    try:
        version = parse_semantic_version_output(version_text, _VERSION)
    except CliIdentityPrimitiveError:
        raise AdapterError("version")
    status_result = _invoke(
        transport, argv=(str(resolved), "login", "status"), stdin=b"", **common
    )
    try:
        streams = (
            decode_cli_output(status_result.stdout),
            decode_cli_output(status_result.stderr),
        )
    except CliProcessError:
        raise AdapterError("auth_required") from None
    lines = [line.strip() for stream in streams for line in stream.splitlines() if line.strip()]
    status_count = lines.count("Logged in using ChatGPT")
    noise = [line for line in lines if line != "Logged in using ChatGPT"]
    if status_count != 1 or any(
        not line.startswith(_PREFLIGHT_WARNING_PREFIXES) for line in noise
    ):
        raise AdapterError("auth_required")
    if _file_sha256(resolved) != executable_digest:
        raise AdapterError("version")
    return CliIdentity(
        ProviderId.CODEX,
        executable_digest,
        version,
        ADAPTER_PROTOCOL_VERSION,
    )


def _token(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKEN_COUNT:
        raise AdapterError("protocol")
    return value


def _failure_code(event: dict[str, object]) -> str:
    text = json.dumps(event, ensure_ascii=True, separators=(",", ":")).lower()
    if "auth" in text or "login" in text or "unauthorized" in text:
        return "auth_required"
    if "quota" in text or "rate_limit" in text or "rate limit" in text:
        return "quota"
    return "unavailable"


def _parse_jsonl(
    stdout: bytes,
    expected_model: str,
    *,
    final_message_only: bool = False,
) -> tuple[str, str, int, int]:
    try:
        text = decode_cli_output(stdout)
    except CliProcessError:
        raise AdapterError("protocol") from None
    lines = text.splitlines()
    if not lines or len(lines) > MAX_EVENT_COUNT or any(not line.strip() for line in lines):
        raise AdapterError("protocol")
    messages: list[str] = []
    completion: dict[str, object] | None = None
    terminal_index: int | None = None
    for index, line in enumerate(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise AdapterError("protocol") from None
        if not isinstance(event, dict) or event.get("type") not in _ALLOWED_EVENTS:
            raise AdapterError("protocol")
        event_type = event["type"]
        if event_type in {"turn.failed", "error"}:
            if index != len(lines) - 1:
                raise AdapterError("protocol")
            raise AdapterError(_failure_code(event))
        if event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                message = item.get("text")
                if not isinstance(message, str) or not message or len(message) > MAX_MESSAGE_LENGTH:
                    raise AdapterError("protocol")
                messages.append(message)
        if event_type == "turn.completed":
            if completion is not None:
                raise AdapterError("protocol")
            completion = event
            terminal_index = index
    if completion is None or terminal_index != len(lines) - 1:
        raise AdapterError("protocol")
    effective_model = completion.get("model")
    if effective_model is None:
        # Codex's documented exec JSONL terminal event does not echo the model.
        # Identity remains bound by the full requested slug, strict config, and
        # the immutable capability snapshot. A future conflicting echo fails.
        effective_model = expected_model
    else:
        if not isinstance(effective_model, str):
            raise AdapterError("model_identity_unverified")
        try:
            _identifier(effective_model)
        except AdapterError:
            raise AdapterError("model_identity_unverified") from None
        if effective_model != expected_model:
            raise AdapterError("model_mismatch")
    usage = completion.get("usage")
    if not isinstance(usage, dict):
        raise AdapterError("protocol")
    message = messages[-1] if final_message_only else "\n".join(messages)
    if not message:
        raise AdapterError("protocol")
    if message.strip().lower() == _CAPACITY_MESSAGE:
        raise AdapterError("capacity_unavailable")
    return (
        message,
        effective_model,
        _token(usage.get("input_tokens")),
        _token(usage.get("output_tokens")),
    )


def execute_codex(
    *,
    executable: Path,
    workspace: Path,
    credential_home: Path | None,
    prompt: bytes,
    requested_model: str,
    expected_effective_model: str,
    effort: Effort,
    permission_mode: PermissionMode,
    output_schema_path: Path | None = None,
    output_schema_sha256: str | None = None,
    transport: Transport = run_cli_process,
    timeout_seconds: float = EXECUTION_TIMEOUT_SECONDS,
) -> CodexExecutionResult:
    """Execute exactly one ephemeral, sandboxed Codex task."""
    resolved = _canonical_file(executable, workspace=workspace)
    requested = _identifier(requested_model)
    expected = _identifier(expected_effective_model)
    try:
        normalized_effort = Effort(effort)
        permission = PermissionMode(permission_mode)
    except (TypeError, ValueError):
        raise AdapterError("request_invalid") from None
    if normalized_effort in {Effort.DEFAULT, Effort.MAX}:
        raise AdapterError("request_invalid")
    schema = _output_schema(
        output_schema_path,
        output_schema_sha256,
        workspace=workspace,
    )
    sandbox = "workspace-write" if permission is PermissionMode.WORKSPACE_WRITE else "read-only"
    argv_prefix = (
        str(resolved),
        "--strict-config",
        "-a",
        "never",
        "-s",
        sandbox,
        "-C",
        str(workspace.resolve()),
        "-m",
        requested,
        "-c",
        f'model_reasoning_effort="{normalized_effort.value}"',
        *(
            ("-c", f'windows.sandbox="{WINDOWS_SANDBOX_MODE}"')
            if permission is PermissionMode.WORKSPACE_WRITE
            else ()
        ),
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
    )
    argv = (
        *argv_prefix,
        *(("--output-schema", str(schema[0])) if schema is not None else ()),
        "-",
    )
    result = _invoke(
        transport,
        argv=argv,
        cwd=workspace,
        stdin=prompt,
        provider=ProviderId.CODEX,
        credential_home=credential_home,
        timeout_seconds=timeout_seconds,
    )
    if schema is not None:
        try:
            current = _output_schema(
                schema[0],
                schema[1],
                workspace=workspace,
            )
        except AdapterError:
            raise AdapterError("response_contract_changed") from None
        if current is None or current[1] != schema[1]:
            raise AdapterError("response_contract_changed")
    message, effective_model, input_tokens, output_tokens = _parse_jsonl(
        result.stdout,
        expected,
        final_message_only=schema is not None,
    )
    return CodexExecutionResult(
        effective_model,
        message,
        input_tokens,
        output_tokens,
        result.duration_seconds,
        result.input_sha256,
        result.stdout_sha256,
        result.stderr_sha256,
    )
