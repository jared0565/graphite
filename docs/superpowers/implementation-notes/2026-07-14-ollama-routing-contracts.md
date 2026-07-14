# Ollama Routing Contract Verification

Verified against official Ollama documentation and the local loopback inventory on 2026-07-14.

- List models: https://docs.ollama.com/api/tags
- Chat endpoint and response fields: https://docs.ollama.com/api/chat
- Usage metrics: https://docs.ollama.com/api/usage
- Local and cloud authentication: https://docs.ollama.com/api/authentication
- Cloud model behavior and deprecations: https://docs.ollama.com/cloud
- Thinking controls: https://docs.ollama.com/capabilities/thinking
- Kimi K2.7 Code profile: https://ollama.com/library/kimi-k2.7-code
- Kimi K2.6 profile: https://ollama.com/library/kimi-k2.6
- GLM-5 profile: https://ollama.com/library/glm-5

Implementation conclusions:

1. Graphite contacts only the local Ollama API at canonical loopback. It does not send or read an Ollama API key.
2. Inventory refresh uses `GET /api/tags`; model execution will use non-streaming `POST /api/chat`.
3. Usage accounting reads `prompt_eval_count`, `eval_count`, and nanosecond duration fields defensively.
4. Ollama documents `think` booleans and levels, but exact support varies by model. No model-specific level was live-tested during this task, so the three provisional profiles expose only `default` and omit `think`.
5. The local inventory contained exact entries for `kimi-k2.7-code:cloud`, `kimi-k2.6:cloud`, and `glm-5:cloud`. It did not contain `glm-5.2:cloud`, so Graphite does not allowlist GLM-5.2 yet.
6. Ollama lists GLM-5 cloud retirement for 2026-07-15. Its provisional profile records that date so policy can stop selecting it rather than silently following a mutable alias.
