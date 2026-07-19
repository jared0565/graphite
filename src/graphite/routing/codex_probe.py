"""Bounded, non-inference Codex CLI lifecycle observation."""
from __future__ import annotations

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

_VERSION: Final = re.compile(r"^codex-cli (0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\r?\n?$")
_GLOBAL_FLAGS: Final = frozenset({"--strict-config", "-a", "-s", "-C", "-m", "-c"})
_EXEC_FLAGS: Final = frozenset(
    {"--json", "--ephemeral", "--ignore-user-config", "--ignore-rules"}
)
_WARNING_PREFIXES: Final = ("WARNING: failed to clean up stale arg0 temp dirs:", "WARNING: proceeding, even though we could not create PATH aliases:")
ProcessProbe = Callable[..., CliProcessResult]


def _streams(result: CliProcessResult, code: str) -> tuple[str, str]:
    if not isinstance(result, CliProcessResult) or result.returncode != 0:
        raise ProviderProbeError(code)
    try:
        return decode_cli_output(result.stdout), decode_cli_output(result.stderr)
    except CliProcessError:
        raise ProviderProbeError(code) from None


def observe_codex(
    *, executable: Path, workspace: Path, credential_home: Path | None,
    observed_at: int, policy_version: str, timeout_seconds: float = 15.0,
    transport: ProcessProbe = run_process_probe,
    clock: Callable[[], float] = time.monotonic,
) -> ProviderRuntimeIdentity:
    """Observe one Codex CLI through fixed local metadata commands only."""
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
            return transport(argv=(str(resolved), *args), cwd=workspace, provider=LifecycleProviderId.CODEX, credential_home=credential_home, timeout_seconds=remaining)
        except ProviderProbeError:
            raise
        except Exception:
            raise ProviderProbeError("probe_failed") from None

    try:
        version = parse_semantic_version_output(_streams(call("--version"), "probe_version_invalid")[0], _VERSION)
    except CliIdentityPrimitiveError:
        raise ProviderProbeError("probe_version_invalid") from None
    help_stdout, help_stderr = _streams(call("--help"), "probe_capability_missing")
    global_tokens = set((help_stdout + "\n" + help_stderr).split())
    exec_stdout, exec_stderr = _streams(call("exec", "--help"), "probe_capability_missing")
    exec_tokens = set((exec_stdout + "\n" + exec_stderr).split())
    if not _GLOBAL_FLAGS.issubset(global_tokens) or not _EXEC_FLAGS.issubset(exec_tokens):
        raise ProviderProbeError("probe_capability_missing")
    stdout, stderr = _streams(call("login", "status"), "probe_auth_unhealthy")
    lines = [line.strip() for stream in (stdout, stderr) for line in stream.splitlines() if line.strip()]
    if lines.count("Logged in using ChatGPT") != 1 or any(line != "Logged in using ChatGPT" and not line.startswith(_WARNING_PREFIXES) for line in lines):
        raise ProviderProbeError("probe_auth_unhealthy")
    try:
        if executable_sha256(resolved) != runtime_digest:
            raise ProviderProbeError("probe_identity_changed")
    except CliIdentityPrimitiveError:
        raise ProviderProbeError("probe_identity_changed") from None
    try:
        return ProviderRuntimeIdentity(LifecycleProviderId.CODEX, RuntimeKind.LOCAL_CLI, version, runtime_digest, None, None, ("credential_health", "structured_output", "version"), policy_version, observed_at)
    except ValueError:
        raise ProviderProbeError("probe_request_invalid") from None
