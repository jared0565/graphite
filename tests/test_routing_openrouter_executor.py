"""Governed OpenRouter development executor tests."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from graphite.routing.claude_executor import AdapterError
from graphite.routing.contracts import Effort, ProviderId
from graphite.routing.openrouter_executor import (
    ADAPTER_PROTOCOL_VERSION,
    API_CONTRACT_VERSION,
    CANONICAL_ENDPOINT,
    MAX_EDIT_FILE_BYTES,
    apply_whole_file_edit,
    execute_openrouter,
    preflight_openrouter,
)
from graphite.routing.openrouter_probe import OpenRouterPricing
from graphite.routing.probe_runner import (
    HttpProbeResult,
    ProbeEndpointPurpose,
    ProviderProbeError,
)


class ScriptedHttp:
    def __init__(self, payloads: list[object]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> HttpProbeResult:
        self.calls.append(kwargs)
        body = json.dumps(self.payloads.pop(0), separators=(",", ":")).encode()
        return HttpProbeResult(200, body, hashlib.sha256(body).hexdigest(), 0.01)


def _fake_transport_with_models(data: list[object]) -> ScriptedHttp:
    return ScriptedHttp([{"ok": True}, {"data": data}])


def test_preflight_binds_endpoint_model_routing_and_pricing() -> None:
    preflight = preflight_openrouter(
        api_key="k", model_id="moonshotai/kimi-k3",
        routing_policy={"order": ["moonshotai"]}, observed_at=7, policy_version="1.0.0",
        transport=_fake_transport_with_models(
            [{"id": "moonshotai/kimi-k3", "pricing": {"prompt": "0.0000006", "completion": "0.0000025"}}]
        ),
    )
    assert preflight.identity.provider is ProviderId.OPENROUTER
    assert len(preflight.identity.executable_sha256) == 64
    assert preflight.identity.cli_version == API_CONTRACT_VERSION == "1.0.0"
    assert preflight.identity.adapter_protocol_version == ADAPTER_PROTOCOL_VERSION == "1.0.0"
    assert CANONICAL_ENDPOINT == "https://openrouter.ai/api/v1"
    expected = hashlib.sha256(json.dumps({
        "endpoint": preflight.runtime.runtime_digest,
        "model": preflight.runtime.model_identity_digest,
        "pricing": preflight.pricing.digest,
        "routing": preflight.runtime.routing_policy_digest,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert preflight.identity.executable_sha256 == expected


@pytest.mark.parametrize(
    ("probe_code", "adapter_code"),
    [
        ("probe_model_unavailable", "model_unavailable"),
        ("probe_auth_unhealthy", "auth_required"),
        ("probe_timeout", "unavailable"),
        ("probe_protocol_invalid", "unavailable"),
    ],
)
def test_preflight_maps_probe_failures_to_stable_adapter_codes(
    probe_code: str, adapter_code: str
) -> None:
    def failing(**_kwargs: object) -> HttpProbeResult:
        raise ProviderProbeError(probe_code)

    with pytest.raises(AdapterError, match=f"^{adapter_code}$"):
        preflight_openrouter(
            api_key="k", model_id="moonshotai/kimi-k3",
            routing_policy={}, observed_at=7, policy_version="1.0.0",
            transport=failing,
        )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


_SCHEMA = {
    "additionalProperties": False,
    "properties": {"result": {"const": "GRAPHITE_EDIT_OK", "type": "string"}},
    "required": ["result"],
    "type": "object",
}


def _completion(
    content: str,
    *,
    model: str = "moonshotai/kimi-k3",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    usage: bool = True,
) -> bytes:
    payload: dict[str, object] = {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    if usage:
        payload["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
    return json.dumps(payload).encode()


class _RecordingTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> HttpProbeResult:
        self.calls.append(kwargs)
        return HttpProbeResult(200, self.body, hashlib.sha256(self.body).hexdigest(), 0.05)

    @property
    def endpoint(self) -> object:
        return self.calls[-1]["endpoint"]

    @property
    def request_body(self) -> bytes:
        return self.calls[-1]["request_body"]


def _execute(transport: _RecordingTransport, **overrides: object) -> object:
    kwargs: dict[str, object] = dict(
        api_key="k",
        prompt=b"do the approved task",
        requested_model="moonshotai/kimi-k3",
        expected_effective_model="moonshotai/kimi-k3",
        effort=Effort.HIGH,
        output_schema=_SCHEMA,
        output_schema_sha256=_canonical_sha256(_SCHEMA),
        pricing=OpenRouterPricing(prompt="0.000001", completion="0.000002"),
        max_output_tokens=4096,
        max_cost_microunits=10_000,
        timeout_seconds=120.0,
        transport=transport,
    )
    kwargs.update(overrides)
    return execute_openrouter(**kwargs)


def test_execute_builds_canonical_schema_bound_request_and_returns_canonical_json() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    outcome = _execute(transport)
    assert outcome.message == '{"result":"GRAPHITE_EDIT_OK"}'
    assert outcome.effective_model == "moonshotai/kimi-k3"
    assert outcome.input_tokens == 100
    assert outcome.output_tokens == 50
    assert outcome.cost_microunits == 200  # 100*1 + 50*2 microunits
    assert outcome.duration_seconds == 0.05
    assert len(transport.calls) == 1
    body = json.loads(transport.request_body)
    assert body["temperature"] == 0
    assert body["stream"] is False
    assert body["model"] == "moonshotai/kimi-k3"
    assert body["max_tokens"] == 4096
    assert body["reasoning"] == {"effort": "high"}
    assert body["messages"] == [{"content": "do the approved task", "role": "user"}]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["name"] == "graphite_response"
    assert body["response_format"]["json_schema"]["schema"] == _SCHEMA
    assert body["usage"] == {"include": True}
    assert transport.request_body == json.dumps(
        body, sort_keys=True, separators=(",", ":")
    ).encode()
    assert transport.endpoint.purpose is ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS
    assert transport.calls[-1]["authorization"] == "Bearer k"
    assert outcome.request_sha256 == hashlib.sha256(transport.request_body).hexdigest()
    assert outcome.response_sha256 == hashlib.sha256(transport.body).hexdigest()
    assert "GRAPHITE_EDIT_OK" not in repr(outcome)


def test_execute_json_object_mode_relaxes_response_format_only() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    outcome = _execute(transport, response_format_type="json_object")
    # Parsing and the returned canonical JSON are unchanged from strict mode.
    assert outcome.message == '{"result":"GRAPHITE_EDIT_OK"}'
    assert outcome.input_tokens == 100
    assert outcome.output_tokens == 50
    assert len(transport.calls) == 1
    body = json.loads(transport.request_body)
    # The ONLY change: the provider receives json_object mode, no schema block.
    assert body["response_format"] == {"type": "json_object"}
    # Everything else is held constant (single-variable experiment).
    assert body["temperature"] == 0
    assert body["stream"] is False
    assert body["model"] == "moonshotai/kimi-k3"
    assert body["max_tokens"] == 4096
    assert body["reasoning"] == {"effort": "high"}
    assert body["messages"] == [{"content": "do the approved task", "role": "user"}]
    assert body["usage"] == {"include": True}
    assert transport.request_body == json.dumps(
        body, sort_keys=True, separators=(",", ":")
    ).encode()


def test_execute_still_pins_schema_digest_in_json_object_mode() -> None:
    # Governance is preserved: the schema is still canonicalized and pinned even
    # though it is no longer sent to the provider as an enforcement mechanism.
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    with pytest.raises(AdapterError, match="^request_invalid$"):
        _execute(
            transport,
            response_format_type="json_object",
            output_schema_sha256="0" * 64,
        )
    assert transport.calls == []


def test_execute_rejects_unknown_response_format_type() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    with pytest.raises(AdapterError, match="^request_invalid$"):
        _execute(transport, response_format_type="fenced_text")
    assert transport.calls == []


def test_execute_fails_closed_over_cost_ceiling() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    with pytest.raises(AdapterError, match="^cost_ceiling_exceeded$"):
        _execute(transport, max_cost_microunits=199)  # cost is exactly 200


@pytest.mark.parametrize("content", ["prose, not JSON", "[1,2]", "42"])
def test_execute_rejects_non_object_content(content: str) -> None:
    transport = _RecordingTransport(_completion(content))
    with pytest.raises(AdapterError, match="^response_contract_invalid$"):
        _execute(transport)


def test_execute_rejects_wrong_model_echo() -> None:
    transport = _RecordingTransport(
        _completion('{"result":"GRAPHITE_EDIT_OK"}', model="other/model")
    )
    with pytest.raises(AdapterError, match="^model_mismatch$"):
        _execute(transport)


def test_execute_schema_digest_mismatch_never_reaches_transport() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    with pytest.raises(AdapterError, match="^request_invalid$"):
        _execute(transport, output_schema_sha256="0" * 64)
    assert transport.calls == []


def test_execute_requires_usage() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}', usage=False))
    with pytest.raises(AdapterError, match="^protocol$"):
        _execute(transport)


@pytest.mark.parametrize("effort", [Effort.XHIGH, Effort.MAX, Effort.DEFAULT])
def test_execute_rejects_unsupported_effort(effort: Effort) -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    with pytest.raises(AdapterError, match="^request_invalid$"):
        _execute(transport, effort=effort)
    assert transport.calls == []


@pytest.mark.parametrize(
    ("probe_code", "adapter_code"),
    [
        ("probe_response_limit", "response_limit"),
        ("probe_timeout", "timeout"),
        ("probe_http_status", "unavailable"),
        ("probe_dns_busy", "unavailable"),
    ],
)
def test_execute_maps_transport_failures_to_diagnosable_codes(
    probe_code: str, adapter_code: str
) -> None:
    from graphite.routing.probe_runner import ProviderProbeError

    def failing(**_kwargs: object) -> None:
        raise ProviderProbeError(probe_code)

    with pytest.raises(AdapterError, match=f"^{adapter_code}$"):
        _execute(failing)


def test_execute_requires_api_key_before_any_request() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    with pytest.raises(AdapterError, match="^auth_required$"):
        _execute(transport, api_key="")
    assert transport.calls == []


def test_execute_rejects_undecodable_prompt() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    with pytest.raises(AdapterError, match="^request_invalid$"):
        _execute(transport, prompt=b"\xff\xfe")
    assert transport.calls == []


_EDIT_SCOPE = ("src/alpha.py", "src/beta.py")


def _edit_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "alpha.py").write_bytes(b"alpha = 1\r\n")
    (workspace / "src" / "beta.py").write_bytes(b"beta = 2\n")
    return workspace


def _tree(workspace: Path) -> dict[str, bytes]:
    return {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in sorted(workspace.rglob("*"))
        if path.is_file()
    }


def _edit_payload(files: object, result: object = "GRAPHITE_EDIT_OK") -> dict[str, object]:
    return {"files": files, "result": result}


def _both_files(alpha: str = "alpha = 2\r\n", beta: str = "beta = 3\n") -> list[dict[str, str]]:
    return [
        {"content": beta, "path": "src/beta.py"},
        {"content": alpha, "path": "src/alpha.py"},
    ]


@pytest.mark.parametrize(
    "hostile",
    [
        "../escape.py",
        "C:/x.py",
        "/x.py",
        "src\\alpha.py",
        "./x.py",
        "src//x.py",
        "src/../alpha.py",
        "",
        "x" * 513,
    ],
)
def test_edit_rejects_hostile_paths_without_writing(tmp_path: Path, hostile: str) -> None:
    workspace = _edit_workspace(tmp_path)
    before = _tree(workspace)
    with pytest.raises(AdapterError, match="^edit_scope_violation$"):
        apply_whole_file_edit(
            workspace=workspace,
            payload=_edit_payload([{"content": "x", "path": hostile}]),
            edit_scope=(hostile,),
            max_total_bytes=2048,
        )
    assert _tree(workspace) == before
    assert not (tmp_path / "escape.py").exists()


@pytest.mark.parametrize(
    "files",
    [
        [{"content": "x", "path": "src/alpha.py"}],
        [
            {"content": "x", "path": "src/alpha.py"},
            {"content": "y", "path": "src/beta.py"},
            {"content": "z", "path": "src/gamma.py"},
        ],
        [
            {"content": "x", "path": "src/alpha.py"},
            {"content": "y", "path": "src/alpha.py"},
        ],
        "not-a-list",
        [],
        [{"content": 42, "path": "src/alpha.py"}, {"content": "y", "path": "src/beta.py"}],
        [{"path": "src/alpha.py"}, {"content": "y", "path": "src/beta.py"}],
    ],
)
def test_edit_rejects_scope_mismatch_and_malformed_files(tmp_path: Path, files: object) -> None:
    workspace = _edit_workspace(tmp_path)
    before = _tree(workspace)
    with pytest.raises(AdapterError, match="^edit_scope_violation$"):
        apply_whole_file_edit(
            workspace=workspace,
            payload=_edit_payload(files),
            edit_scope=_EDIT_SCOPE,
            max_total_bytes=2048,
        )
    assert _tree(workspace) == before


@pytest.mark.parametrize(
    "payload",
    [
        {"files": []},
        {"files": [], "result": "DONE"},
        {"files": [], "result": "GRAPHITE_EDIT_OK", "extra": 1},
        "prose",
        {},
    ],
)
def test_edit_rejects_missing_or_wrong_marker(tmp_path: Path, payload: object) -> None:
    workspace = _edit_workspace(tmp_path)
    before = _tree(workspace)
    with pytest.raises(AdapterError, match="^edit_scope_violation$"):
        apply_whole_file_edit(
            workspace=workspace,
            payload=payload,
            edit_scope=_EDIT_SCOPE,
            max_total_bytes=2048,
        )
    assert _tree(workspace) == before


def test_edit_rejects_file_over_byte_cap(tmp_path: Path) -> None:
    workspace = _edit_workspace(tmp_path)
    before = _tree(workspace)
    files = _both_files(alpha="x" * (MAX_EDIT_FILE_BYTES + 1))
    with pytest.raises(AdapterError, match="^edit_scope_violation$"):
        apply_whole_file_edit(
            workspace=workspace,
            payload=_edit_payload(files),
            edit_scope=_EDIT_SCOPE,
            max_total_bytes=10 * MAX_EDIT_FILE_BYTES,
        )
    assert _tree(workspace) == before


def test_edit_rejects_aggregate_over_total_budget(tmp_path: Path) -> None:
    workspace = _edit_workspace(tmp_path)
    before = _tree(workspace)
    files = _both_files(alpha="a" * 8, beta="b" * 8)
    with pytest.raises(AdapterError, match="^edit_scope_violation$"):
        apply_whole_file_edit(
            workspace=workspace,
            payload=_edit_payload(files),
            edit_scope=_EDIT_SCOPE,
            max_total_bytes=15,
        )
    assert _tree(workspace) == before


def test_edit_rejects_symlinked_parent_directory(tmp_path: Path) -> None:
    workspace = _edit_workspace(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    (real / "alpha.py").write_bytes(b"alpha = 1\n")
    link = workspace / "linked"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(AdapterError, match="^edit_scope_violation$"):
        apply_whole_file_edit(
            workspace=workspace,
            payload=_edit_payload([{"content": "x", "path": "linked/alpha.py"}]),
            edit_scope=("linked/alpha.py",),
            max_total_bytes=2048,
        )
    assert (real / "alpha.py").read_bytes() == b"alpha = 1\n"


def test_edit_rejects_missing_scope_target(tmp_path: Path) -> None:
    workspace = _edit_workspace(tmp_path)
    before = _tree(workspace)
    with pytest.raises(AdapterError, match="^edit_scope_violation$"):
        apply_whole_file_edit(
            workspace=workspace,
            payload=_edit_payload([{"content": "x", "path": "src/missing.py"}]),
            edit_scope=("src/missing.py",),
            max_total_bytes=2048,
        )
    assert _tree(workspace) == before
    assert not (workspace / "src" / "missing.py").exists()


def test_edit_rejects_preexisting_temp_collision(tmp_path: Path) -> None:
    workspace = _edit_workspace(tmp_path)
    (workspace / "src" / "alpha.py.graphite-tmp").write_bytes(b"stale")
    before = _tree(workspace)
    with pytest.raises(AdapterError, match="^edit_scope_violation$"):
        apply_whole_file_edit(
            workspace=workspace,
            payload=_edit_payload(_both_files()),
            edit_scope=_EDIT_SCOPE,
            max_total_bytes=2048,
        )
    assert _tree(workspace) == before


def test_edit_replaces_files_atomically_and_byte_exactly(tmp_path: Path) -> None:
    workspace = _edit_workspace(tmp_path)
    applied = apply_whole_file_edit(
        workspace=workspace,
        payload=_edit_payload(_both_files()),
        edit_scope=_EDIT_SCOPE,
        max_total_bytes=2048,
    )
    assert applied == ("src/alpha.py", "src/beta.py")
    assert (workspace / "src" / "alpha.py").read_bytes() == b"alpha = 2\r\n"
    assert (workspace / "src" / "beta.py").read_bytes() == b"beta = 3\n"
    assert not list(workspace.rglob("*.graphite-tmp"))


def test_edit_mid_set_replace_failure_restores_originals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _edit_workspace(tmp_path)
    before = _tree(workspace)
    original_replace = os.replace
    calls = {"count": 0}

    def flaky(source: object, destination: object) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated")
        original_replace(source, destination)

    monkeypatch.setattr("os.replace", flaky)
    with pytest.raises(AdapterError, match="^edit_apply_failed$"):
        apply_whole_file_edit(
            workspace=workspace,
            payload=_edit_payload(_both_files(alpha="alpha = 9\n", beta="beta = 9\n")),
            edit_scope=_EDIT_SCOPE,
            max_total_bytes=2048,
        )
    assert calls["count"] == 2
    assert _tree(workspace) == before
