from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from graphite.routing.claude_probe import observe_claude
from graphite.routing.lifecycle import LifecycleProviderId, RuntimeKind
from graphite.routing.probe_runner import ProviderProbeError
from graphite.routing.process_runner import CliProcessResult


def _result(stdout: bytes) -> CliProcessResult:
    return CliProcessResult(0, stdout, b"", 0.01, "0" * 64, hashlib.sha256(stdout).hexdigest(), hashlib.sha256(b"").hexdigest())


class ScriptedProbe:
    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> CliProcessResult:
        self.calls.append(kwargs)
        return _result(self.outputs.pop(0))


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    executable = tmp_path / "bin" / "claude.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"claude-runtime")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    return executable, workspace, credentials


def test_claude_observation_is_bounded_non_inference_and_normalized(tmp_path: Path) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    probe = ScriptedProbe([
        b"2.1.214 (Claude Code)\n",
        b"--allowedTools --disable-slash-commands --effort --input-format --json-schema --max-turns --model --no-chrome --no-session-persistence --output-format --permission-mode --print --safe-mode --strict-mcp-config --verbose\n",
        b'{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty","email":"private@example.test"}',
    ])

    identity = observe_claude(
        executable=executable,
        workspace=workspace,
        credential_home=credentials,
        observed_at=100,
        policy_version="1.0.0",
        transport=probe,
    )

    assert identity.provider is LifecycleProviderId.CLAUDE_CODE
    assert identity.runtime_kind is RuntimeKind.LOCAL_CLI
    assert identity.version == "2.1.214"
    assert identity.runtime_digest == hashlib.sha256(b"claude-runtime").hexdigest()
    assert identity.capabilities == ("credential_health", "structured_output", "version")
    assert [call["argv"][1:] for call in probe.calls] == [
        ("--version",), ("--help",), ("auth", "status", "--json")
    ]
    assert all("stdin" not in call for call in probe.calls)
    assert "private@example.test" not in repr(identity)


def test_claude_observation_rejects_workspace_local_and_missing_flags(tmp_path: Path) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    local = workspace / "claude.exe"
    local.write_bytes(b"untrusted")
    with pytest.raises(ProviderProbeError, match="^probe_executable_invalid$"):
        observe_claude(executable=local, workspace=workspace, credential_home=credentials, observed_at=1, policy_version="1.0.0")

    probe = ScriptedProbe([b"2.1.214 (Claude Code)\n", b"--output-format\n"])
    with pytest.raises(ProviderProbeError, match="^probe_capability_missing$"):
        observe_claude(executable=executable, workspace=workspace, credential_home=credentials, observed_at=1, policy_version="1.0.0", transport=probe)
    assert len(probe.calls) == 2


def test_claude_observation_accepts_exact_flags_with_help_punctuation(
    tmp_path: Path,
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    help_output = (
        b"[--allowedTools <tools>], --disable-slash-commands --effort --input-format "
        b"--json-schema [--max-turns=<count>] --model --no-chrome "
        b"--no-session-persistence --output-format --permission-mode --print "
        b"--safe-mode --strict-mcp-config --verbose\n"
    )
    probe = ScriptedProbe([
        b"2.1.214 (Claude Code)\n",
        help_output,
        b'{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty"}',
    ])

    identity = observe_claude(
        executable=executable,
        workspace=workspace,
        credential_home=credentials,
        observed_at=100,
        policy_version="1.0.0",
        transport=probe,
    )

    assert identity.version == "2.1.214"
    assert len(probe.calls) == 3


@pytest.mark.parametrize(
    "lookalike",
    [b"prefix--allowedTools", b"--allowedToolsExtra", b"--max-turns-extra"],
)
def test_claude_observation_rejects_flag_lookalikes(
    tmp_path: Path, lookalike: bytes
) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    valid = (
        b"--allowedTools --disable-slash-commands --effort --input-format --json-schema "
        b"--max-turns --model --no-chrome --no-session-persistence --output-format "
        b"--permission-mode --print --safe-mode --strict-mcp-config --verbose"
    )
    if b"allowedTools" in lookalike:
        help_output = valid.replace(b"--allowedTools", lookalike)
    else:
        help_output = valid.replace(b"--max-turns", lookalike)
    probe = ScriptedProbe([b"2.1.214 (Claude Code)\n", help_output])

    with pytest.raises(ProviderProbeError, match="^probe_capability_missing$"):
        observe_claude(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            observed_at=100,
            policy_version="1.0.0",
            transport=probe,
        )

    assert len(probe.calls) == 2
