import pytest

def test_zai_pricing_and_cost():
    from graphite.routing.zai_probe import ZaiPricing, zai_cost_microunits
    p = ZaiPricing(prompt="0.0000014", completion="0.0000044")
    # 1000 in, 100 out -> (1000*1.4e-6 + 100*4.4e-6)*1e6 = 1400 + 440 = 1840
    assert zai_cost_microunits(p, input_tokens=1000, output_tokens=100) == 1840

def test_preflight_zai_builds_identities_no_routing_digest():
    from graphite.routing.zai_probe import preflight_zai, ZAI_MODEL
    from graphite.routing.contracts import ProviderId
    from graphite.routing.lifecycle import LifecycleProviderId
    pf = preflight_zai(model_id=ZAI_MODEL, observed_at=1_700_000_000, policy_version="1.0.0")
    assert pf.runtime.provider is LifecycleProviderId.ZAI
    assert pf.runtime.routing_policy_digest is None
    assert pf.runtime.model_identity_digest is not None      # required for zai
    assert pf.identity.provider is ProviderId.ZAI
    assert len(pf.identity.executable_sha256) == 64
    assert pf.pricing.prompt == "0.0000014"

def test_preflight_zai_rejects_bad_model_id():
    from graphite.routing.zai_probe import preflight_zai
    with pytest.raises(Exception):
        preflight_zai(model_id="bad model", observed_at=1, policy_version="1.0.0")

def test_zai_policy_supports_zai_identity():
    from graphite.routing.zai_probe import _ZAI_POLICY, preflight_zai, ZAI_MODEL
    pf = preflight_zai(model_id=ZAI_MODEL, observed_at=1_700_000_000, policy_version="1.0.0")
    assert _ZAI_POLICY.supports(pf.runtime) is True
