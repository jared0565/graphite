from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from graphite.routing.codex_probe import observe_codex
from graphite.routing.lifecycle import LifecycleProviderId
from graphite.routing.probe_runner import ProviderProbeError
from graphite.routing.process_runner import CliProcessResult


def _result(stdout: bytes, stderr: bytes = b"") -> CliProcessResult:
    return CliProcessResult(0, stdout, stderr, 0.01, "0" * 64, hashlib.sha256(stdout).hexdigest(), hashlib.sha256(stderr).hexdigest())


class ScriptedProbe:
    def __init__(self, results: list[CliProcessResult]) -> None:
        self.results = results
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> CliProcessResult:
        self.calls.append(kwargs)
        return self.results.pop(0)


def test_codex_observation_uses_only_fixed_metadata_commands(tmp_path: Path) -> None:
    executable = tmp_path / "bin" / "codex.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"codex-runtime")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    probe = ScriptedProbe([
        _result(b"codex-cli 0.144.1\n"),
        _result(b"--strict-config -a -s -C -m -c\n"),
        _result(b"--json --ephemeral --ignore-user-config --ignore-rules\n"),
        _result(b"Logged in using ChatGPT\n"),
    ])

    identity = observe_codex(executable=executable, workspace=workspace, credential_home=credentials, observed_at=200, policy_version="1.0.0", transport=probe)

    assert identity.provider is LifecycleProviderId.CODEX
    assert identity.version == "0.144.1"
    assert identity.capabilities == ("credential_health", "structured_output", "version")
    assert [call["argv"][1:] for call in probe.calls] == [("--version",), ("--help",), ("exec", "--help"), ("login", "status")]
    assert len(probe.calls) == 4


def test_codex_observation_stops_on_unhealthy_auth_without_leaking(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"codex-runtime")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    probe = ScriptedProbe([_result(b"codex-cli 0.144.1"), _result(b"--strict-config -a -s -C -m -c"), _result(b"--json --ephemeral --ignore-user-config --ignore-rules"), _result(b"Logged in using API key PRIVATE")])
    with pytest.raises(ProviderProbeError, match="^probe_auth_unhealthy$") as caught:
        observe_codex(executable=executable, workspace=workspace, credential_home=None, observed_at=1, policy_version="1.0.0", transport=probe)
    assert "PRIVATE" not in repr(caught.value)
    assert len(probe.calls) == 4
