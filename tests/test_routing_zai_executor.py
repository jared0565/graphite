import hashlib
import json
import pytest
from graphite.routing.probe_runner import HttpProbeResult, ProviderProbeError
from graphite.routing.zai_probe import ZaiPricing

PRICING = ZaiPricing(prompt="0.0000014", completion="0.0000044")

def _envelope(content, model="glm-5.2", pin=420, cout=6):
    payload = {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": pin, "completion_tokens": cout},
    }
    if model is not None:
        payload["model"] = model
    body = json.dumps(payload).encode()
    return HttpProbeResult(200, body, hashlib.sha256(body).hexdigest(), 0.05)

def _raw(obj):
    body = json.dumps(obj).encode()
    return HttpProbeResult(200, body, hashlib.sha256(body).hexdigest(), 0.05)

def test_execute_zai_returns_plaintext_and_usage():
    from graphite.routing.zai_executor import execute_zai
    seen = {}
    def transport(**kw):
        seen.update(kw)
        return _envelope("GRAPHITE_PROFILE_OK")
    result = execute_zai(
        api_key="k", prompt=b"return GRAPHITE_PROFILE_OK", requested_model="glm-5.2",
        expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
        max_cost_microunits=100000, timeout_seconds=60.0, transport=transport)
    assert result.message == "GRAPHITE_PROFILE_OK"        # verbatim, NOT json-parsed
    assert result.input_tokens == 420 and result.output_tokens == 6
    body = json.loads(seen["request_body"])
    assert body == {"max_tokens": 64, "messages": [{"content": "return GRAPHITE_PROFILE_OK", "role": "user"}], "model": "glm-5.2", "stream": False, "temperature": 0, "thinking": {"type": "disabled"}}
    assert seen["authorization"] == "Bearer k"

def test_execute_zai_disables_thinking_for_deterministic_plaintext():
    # glm-5.2 is a reasoning model; with thinking on it spends the whole output
    # budget on reasoning_tokens and returns empty content (finish_reason=length),
    # so the bounded plain-text oracle never gets emitted. The executor must send
    # z.ai's `thinking:{type:disabled}` (confirmed honored -> reasoning_tokens 0)
    # so the exact answer is produced deterministically inside the 64-token budget.
    from graphite.routing.zai_executor import execute_zai
    seen = {}
    def transport(**kw):
        seen.update(kw)
        return _envelope("GRAPHITE_PROFILE_OK")
    execute_zai(
        api_key="k", prompt=b"return GRAPHITE_PROFILE_OK", requested_model="glm-5.2",
        expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
        max_cost_microunits=100000, timeout_seconds=60.0, transport=transport)
    body = json.loads(seen["request_body"])
    assert body.get("thinking") == {"type": "disabled"}

def test_execute_zai_cost_ceiling():
    from graphite.routing.zai_executor import execute_zai
    from graphite.routing.claude_executor import AdapterError
    with pytest.raises(AdapterError) as e:
        execute_zai(api_key="k", prompt=b"x", requested_model="glm-5.2",
            expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
            max_cost_microunits=1, timeout_seconds=60.0,
            transport=lambda **kw: _envelope("GRAPHITE_PROFILE_OK", cout=1000))
    assert e.value.code == "cost_ceiling_exceeded"

def test_execute_zai_maps_transport_failure():
    from graphite.routing.zai_executor import execute_zai
    from graphite.routing.claude_executor import AdapterError
    def transport(**kw): raise ProviderProbeError("probe_http_status")
    with pytest.raises(AdapterError) as e:
        execute_zai(api_key="k", prompt=b"x", requested_model="glm-5.2",
            expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
            max_cost_microunits=100000, timeout_seconds=60.0, transport=transport)
    assert e.value.code == "http_status"

@pytest.mark.parametrize("obj", [
    [1, 2],                                                                   # envelope not a dict
    {"model": "glm-5.2", "usage": {"prompt_tokens": 1, "completion_tokens": 1}},  # no choices
    {"model": "glm-5.2", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},  # 0 choices
    {"model": "glm-5.2", "choices": [{"message": {}}, {"message": {}}],        # 2 choices
     "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    {"model": "glm-5.2", "choices": [{"message": "nope"}],                     # message not a dict
     "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    {"model": "glm-5.2", "choices": [{"message": {"content": 42}}],            # content not a str
     "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    {"model": "glm-5.2", "choices": [{"message": {"content": "x"}}]},          # usage missing
    {"model": "glm-5.2", "choices": [{"message": {"content": "x"}}],           # token not an int
     "usage": {"prompt_tokens": "1", "completion_tokens": 1}},
])
def test_execute_zai_rejects_malformed_envelope(obj):
    from graphite.routing.zai_executor import execute_zai
    from graphite.routing.claude_executor import AdapterError
    with pytest.raises(AdapterError) as e:
        execute_zai(api_key="k", prompt=b"x", requested_model="glm-5.2",
            expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
            max_cost_microunits=100000, timeout_seconds=60.0, transport=lambda **kw: _raw(obj))
    assert e.value.code == "protocol"

def test_execute_zai_rejects_missing_model():
    from graphite.routing.zai_executor import execute_zai
    from graphite.routing.claude_executor import AdapterError
    with pytest.raises(AdapterError) as e:
        execute_zai(api_key="k", prompt=b"x", requested_model="glm-5.2",
            expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
            max_cost_microunits=100000, timeout_seconds=60.0,
            transport=lambda **kw: _envelope("GRAPHITE_PROFILE_OK", model=None))
    assert e.value.code == "model_identity_unverified"

def test_execute_zai_rejects_wrong_model():
    from graphite.routing.zai_executor import execute_zai
    from graphite.routing.claude_executor import AdapterError
    with pytest.raises(AdapterError) as e:
        execute_zai(api_key="k", prompt=b"x", requested_model="glm-5.2",
            expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
            max_cost_microunits=100000, timeout_seconds=60.0,
            transport=lambda **kw: _envelope("GRAPHITE_PROFILE_OK", model="other-model"))
    assert e.value.code == "model_mismatch"

def test_execute_zai_rejects_empty_model():
    from graphite.routing.zai_executor import execute_zai
    from graphite.routing.claude_executor import AdapterError
    with pytest.raises(AdapterError) as e:
        execute_zai(api_key="k", prompt=b"x", requested_model="glm-5.2",
            expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
            max_cost_microunits=100000, timeout_seconds=60.0,
            transport=lambda **kw: _envelope("GRAPHITE_PROFILE_OK", model=""))
    assert e.value.code == "model_identity_unverified"

def test_execute_zai_rejects_non_string_model():
    from graphite.routing.zai_executor import execute_zai
    from graphite.routing.claude_executor import AdapterError
    with pytest.raises(AdapterError) as e:
        execute_zai(api_key="k", prompt=b"x", requested_model="glm-5.2",
            expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
            max_cost_microunits=100000, timeout_seconds=60.0,
            transport=lambda **kw: _envelope("GRAPHITE_PROFILE_OK", model=123))
    assert e.value.code == "model_identity_unverified"

def test_execute_zai_returns_empty_content_verbatim():
    # When glm-5.2 exhausts its budget on reasoning (finish_reason=length) it returns empty
    # content. execute_zai returns it verbatim (message==""); the harness-side plaintext oracle
    # (message.strip() == exact_text) is what rejects it. This pins that the executor stays a
    # general adapter and does not itself apply the verification string.
    from graphite.routing.zai_executor import execute_zai
    result = execute_zai(
        api_key="k", prompt=b"x", requested_model="glm-5.2",
        expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
        max_cost_microunits=100000, timeout_seconds=60.0,
        transport=lambda **kw: _envelope(""))
    assert result.message == ""
