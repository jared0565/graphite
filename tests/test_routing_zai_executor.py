import hashlib, json, pytest
from graphite.routing.probe_runner import HttpProbeResult, ProviderProbeError
from graphite.routing.zai_probe import ZaiPricing

PRICING = ZaiPricing(prompt="0.0000014", completion="0.0000044")

def _envelope(content, model="glm-5.2", pin=420, cout=6):
    body = json.dumps({
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": pin, "completion_tokens": cout},
    }).encode()
    return HttpProbeResult(200, body, hashlib.sha256(body).hexdigest(), 0.05)

def test_execute_zai_returns_plaintext_and_usage():
    from graphite.routing.zai_executor import execute_zai
    seen = {}
    def transport(**kw):
        seen.update(kw); return _envelope("GRAPHITE_PROFILE_OK")
    result = execute_zai(
        api_key="k", prompt=b"return GRAPHITE_PROFILE_OK", requested_model="glm-5.2",
        expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
        max_cost_microunits=100000, timeout_seconds=60.0, transport=transport)
    assert result.message == "GRAPHITE_PROFILE_OK"        # verbatim, NOT json-parsed
    assert result.input_tokens == 420 and result.output_tokens == 6
    body = json.loads(seen["request_body"])
    assert body == {"max_tokens": 64, "messages": [{"content": "return GRAPHITE_PROFILE_OK", "role": "user"}], "model": "glm-5.2", "stream": False, "temperature": 0}
    assert seen["authorization"] == "Bearer k"

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
