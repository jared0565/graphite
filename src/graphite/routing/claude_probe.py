"""Bounded, non-inference Claude Code lifecycle observation."""
from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from .cli_identity import CliIdentityPrimitiveError, canonical_executable, executable_sha256, parse_semantic_version_output
from .lifecycle import LifecycleProviderId, ProviderRuntimeIdentity, RuntimeKind
from .probe_runner import ProviderProbeError, run_process_probe
from .process_runner import CliProcessError, CliProcessResult, decode_cli_output

_VERSION: Final = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*) \(Claude Code\)\r?\n?$")
_FLAGS: Final = frozenset(
    {
        "--allowedTools",
        "--disable-slash-commands",
        "--effort",
        "--input-format",
        "--json-schema",
        "--max-turns",
        "--model",
        "--no-chrome",
        "--no-session-persistence",
        "--output-format",
        "--permission-mode",
        "--print",
        "--safe-mode",
        "--strict-mcp-config",
        "--verbose",
    }
)
_FLAG_PATTERNS: Final = tuple(
    re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(flag)}(?![A-Za-z0-9_-])")
    for flag in sorted(_FLAGS)
)
ProcessProbe = Callable[..., CliProcessResult]


def _text(result: CliProcessResult, code: str) -> str:
    if not isinstance(result, CliProcessResult) or result.returncode != 0:
        raise ProviderProbeError(code)
    try:
        return decode_cli_output(result.stdout)
    except CliProcessError:
        raise ProviderProbeError(code) from None


def observe_claude(
    *, executable: Path, workspace: Path, credential_home: Path | None,
    observed_at: int, policy_version: str, timeout_seconds: float = 15.0,
    transport: ProcessProbe = run_process_probe,
    clock: Callable[[], float] = time.monotonic,
) -> ProviderRuntimeIdentity:
    """Observe one Claude CLI through fixed local metadata commands only."""
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= 30:
        raise ProviderProbeError("probe_request_invalid")
    try:
        resolved = canonical_executable(executable, workspace=workspace)
        runtime_digest = executable_sha256(resolved)
    except CliIdentityPrimitiveError:
        raise ProviderProbeError("probe_executable_invalid") from None
    deadline = clock() + float(timeout_seconds)

    def call(*args: str) -> CliProcessResult:
        remaining = deadline - clock()
        if remaining <= 0:
            raise ProviderProbeError("probe_timeout")
        try:
            return transport(argv=(str(resolved), *args), cwd=workspace, provider=LifecycleProviderId.CLAUDE_CODE, credential_home=credential_home, timeout_seconds=remaining)
        except ProviderProbeError:
            raise
        except Exception:
            raise ProviderProbeError("probe_failed") from None

    try:
        version = parse_semantic_version_output(_text(call("--version"), "probe_version_invalid"), _VERSION)
    except CliIdentityPrimitiveError:
        raise ProviderProbeError("probe_version_invalid") from None
    help_text = _text(call("--help"), "probe_capability_missing")
    if not all(pattern.search(help_text) is not None for pattern in _FLAG_PATTERNS):
        raise ProviderProbeError("probe_capability_missing")
    try:
        auth = json.loads(_text(call("auth", "status", "--json"), "probe_auth_unhealthy"))
    except (json.JSONDecodeError, RecursionError):
        raise ProviderProbeError("probe_auth_unhealthy") from None
    if not isinstance(auth, dict) or auth.get("loggedIn") is not True or auth.get("authMethod") != "claude.ai" or auth.get("apiProvider") != "firstParty":
        raise ProviderProbeError("probe_auth_unhealthy")
    try:
        if executable_sha256(resolved) != runtime_digest:
            raise ProviderProbeError("probe_identity_changed")
    except CliIdentityPrimitiveError:
        raise ProviderProbeError("probe_identity_changed") from None
    try:
        return ProviderRuntimeIdentity(LifecycleProviderId.CLAUDE_CODE, RuntimeKind.LOCAL_CLI, version, runtime_digest, None, None, ("credential_health", "structured_output", "version"), policy_version, observed_at)
    except ValueError:
        raise ProviderProbeError("probe_request_invalid") from None
