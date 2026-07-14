# Router model pool evidence — 2026-07-14

## Evidence boundary

This note records a bounded, read-only local observation from
`GET http://127.0.0.1:11434/api/tags` on 2026-07-14. It was a loopback inventory
read, not an inference request. These values establish what the local inventory
reported at access time; they are not a provider guarantee. Execution still
requires the bundled allowlist, a fresh inventory, and exact digest revalidation.
Inventory presence does not authorize a model.

The provider-reported usage class below is coarse routing metadata, not a USD price
or measured cost saving. Each profile is provisional, uses only `default` effort,
and is subject to capability, context, risk, quota, and the 30-day minimum
retirement runway hard gates.

## Approved profile evidence

### `kimi-k2.7-code:cloud`

- Locally observed digest: `eda07a6592375dcbde7cf167b6d6b368cdd28e244f9d71559fb59919aca882fa`
- Locally observed context length: 262144 tokens
- Locally observed capabilities: vision, thinking, completion, tools
- Official Ollama URL: <https://ollama.com/library/kimi-k2.7-code>
- Official evidence access date: 2026-07-14
- Roles: primary coding, coding
- Provider-reported usage class: high
- Profile status and effort: provisional; `default` only

### `minimax-m2.7:cloud`

- Locally observed digest: `06daa293c105f0bd71fd19420e4d15cae66cc5f71cb8f55b4f998e96ec8ab67a`
- Locally observed context length: 204800 tokens
- Locally observed capabilities: completion, tools, thinking
- Official Ollama URL: <https://ollama.com/library/minimax-m2.7:cloud>
- Official evidence access date: 2026-07-14
- Roles: coding, agentic
- Provider-reported usage class: medium
- Profile status and effort: provisional; `default` only

### `nemotron-3-super:cloud`

- Locally observed digest: `be3943c5a818be61a08f3563b971e392bfc12e506e296fb186c870f5c63377a4`
- Locally observed context length: 262144 tokens
- Locally observed capabilities: completion, tools, thinking
- Official Ollama URL: <https://ollama.com/library/nemotron-3-super:cloud>
- Official evidence access date: 2026-07-14
- Roles: reasoning, review
- Provider-reported usage class: medium
- Profile status and effort: provisional; `default` only

### `minimax-m3:cloud`

- Locally observed digest: `d03a959f45c04ab183e245922ecb46ebccfb9d5e55bdee5e9055271ee70195e3`
- Locally observed context length: 524288 tokens
- Locally observed capabilities: completion, tools, thinking, vision
- Official Ollama URL: <https://ollama.com/library/minimax-m3:cloud>
- Official evidence access date: 2026-07-14
- Roles: long-context, agentic
- Provider-reported usage class: high
- Profile status and effort: provisional; `default` only

## Removed and migration history

`glm-5:cloud` was removed from the router allowlist during the 2026-07-14 pool
migration. Its appearance here is historical only; it is not an active profile or
execution candidate.
