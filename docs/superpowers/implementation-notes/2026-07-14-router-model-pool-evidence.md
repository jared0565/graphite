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

Provenance is intentionally split: model ID, digest, context, and provider
capabilities were observed in the local loopback inventory; usage class and official
URL/access date are provider-page metadata; roles, provisional status, and
default-only effort are Graphite policy metadata. To reproduce a sanitized local
observation, read `GET http://127.0.0.1:11434/api/tags`, retain only the four approved
IDs, and emit only `name`, `digest`, `details.context_length`, and `capabilities`.
Do not retain unrelated inventory entries or local aliases.

Canonical snapshot representation: sort rows by `model_id`; encode a JSON array
whose objects contain only `model_id`, `digest`, integer `context`, and ordered
`provider_capabilities`; use UTF-8, lexicographically sorted object keys, and JSON
separators `,` and `:` with no extra whitespace. Python standard-library equivalent:
`json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")`.
The SHA-256 of the four rows below is
`4bb9957626958885b90d9d6cf22b92e77a7cf09eac7c7bec830a70123684890c`.

## Approved profile evidence

| Model ID | Digest | Context | Roles | Usage class | Provider capabilities | Official Ollama URL | Accessed | Status | Effort |
|---|---|---|---|---|---|---|---|---|---|
| `kimi-k2.7-code:cloud` | `eda07a6592375dcbde7cf167b6d6b368cdd28e244f9d71559fb59919aca882fa` | 262144 | primary coding, coding | high | vision, thinking, completion, tools | <https://ollama.com/library/kimi-k2.7-code> | 2026-07-14 | provisional | `default` only |
| `minimax-m2.7:cloud` | `06daa293c105f0bd71fd19420e4d15cae66cc5f71cb8f55b4f998e96ec8ab67a` | 204800 | coding, agentic | medium | completion, tools, thinking | <https://ollama.com/library/minimax-m2.7:cloud> | 2026-07-14 | provisional | `default` only |
| `nemotron-3-super:cloud` | `be3943c5a818be61a08f3563b971e392bfc12e506e296fb186c870f5c63377a4` | 262144 | reasoning, review | medium | completion, tools, thinking | <https://ollama.com/library/nemotron-3-super:cloud> | 2026-07-14 | provisional | `default` only |
| `minimax-m3:cloud` | `d03a959f45c04ab183e245922ecb46ebccfb9d5e55bdee5e9055271ee70195e3` | 524288 | long-context, agentic | high | completion, tools, thinking, vision | <https://ollama.com/library/minimax-m3:cloud> | 2026-07-14 | provisional | `default` only |

## Removed and migration history

`glm-5:cloud` was removed from the router allowlist during the 2026-07-14 pool
migration. Its appearance here is historical only; it is not an active profile or
execution candidate.
