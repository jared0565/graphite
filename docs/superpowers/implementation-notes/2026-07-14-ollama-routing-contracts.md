# Ollama Routing Contract Verification

Verified against official Ollama documentation and the local loopback inventory on 2026-07-14.

## Historical and superseded profile record

This note preserves the evidence used by the earlier routing pool. It is historical,
not the active allowlist. The [hardened four-model evidence
note](2026-07-14-router-model-pool-evidence.md) is the current profile evidence
record. Only `routing.registry.BUNDLED_PROFILES`, the bundled allowlist, is the
authorization authority.

- List models: https://docs.ollama.com/api/tags
- Chat endpoint and response fields: https://docs.ollama.com/api/chat
- Usage metrics: https://docs.ollama.com/api/usage
- Local and cloud authentication: https://docs.ollama.com/api/authentication
- Cloud model behavior and deprecations: https://docs.ollama.com/cloud
- Thinking controls: https://docs.ollama.com/capabilities/thinking
- Kimi K2.7 Code profile: https://ollama.com/library/kimi-k2.7-code
- Kimi K2.6 profile: https://ollama.com/library/kimi-k2.6
- Historical removed GLM-5 profile: https://ollama.com/library/glm-5

Implementation conclusions:

1. Graphite contacts only the local Ollama API at canonical loopback. It does not send or read an Ollama API key.
2. Inventory refresh uses `GET /api/tags`; model execution will use non-streaming `POST /api/chat`.
3. Usage accounting reads `prompt_eval_count`, `eval_count`, and nanosecond duration fields defensively.
4. Ollama documents `think` booleans and levels, but exact support varies by model. No model-specific level was live-tested during the earlier task, so those now-superseded provisional profiles exposed only `default` and omitted `think`.
5. Historically, the local inventory contained exact entries for `kimi-k2.7-code:cloud`, `kimi-k2.6:cloud`, and the now-removed `glm-5:cloud`. The absence of `glm-5.2:cloud` was recorded at that time; neither GLM identifier is in the current hardened allowlist.
6. Historical retirement evidence listed GLM-5 cloud retirement for 2026-07-15. The superseded provisional profile recorded that date so the earlier policy could stop selecting it rather than silently follow a mutable alias. That profile is removed; the hardened four-model evidence note is the current profile evidence record, while only the bundled allowlist authorizes models.
