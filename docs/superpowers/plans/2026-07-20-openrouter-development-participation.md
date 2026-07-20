# OpenRouter Development Participation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OpenRouter a third governed development provider with read-only review roles and bounded whole-file edit authority, behind the same fail-closed snapshot/lifecycle/manifest governance as the Claude Code and Codex CLIs.

**Architecture:** A new `openrouter_executor` adapter performs exactly one pinned, schema-bound `chat/completions` call per approved action, with pricing captured at probe time feeding a hard per-call cost ceiling. Edits are delivered as schema-bound complete file contents that Graphite validates fully and applies atomically inside an isolated worktree; the existing diff-policy, deterministic-validation, and promotion pipeline is reused unchanged.

**Tech Stack:** Python 3.14, stdlib only (http.client via existing `probe_runner`, `decimal`, `hashlib`, `json`), pytest with deterministic fake transports, Ruff.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-20-openrouter-development-participation-design.md`.
- Worktree `F:\tmp\graphite-claude-codex-router`, branch `feat/claude-codex-router`. Never touch `F:\Projects\graphite` (main). No merge, push, or deploy.
- Tests need `PYTHONPATH=F:\tmp\graphite-claude-codex-router\src` and `-p no:cacheprovider`.
- No live provider, network, or credentialed call anywhere in this plan — deterministic fakes only. Live acceptance is a separate manifest-gated phase after the plan completes.
- Claude and Codex adapter argv and behavior must remain byte-identical; existing tests prove it and must keep passing.
- Canonical endpoint literal everywhere: `https://openrouter.ai/api/v1`. Adapter protocol and API contract versions: `"1.0.0"`.
- All new failure codes are stable snake_case strings raised via the existing `AdapterError` / `ProviderProbeError` / harness patterns; no raw provider output, prompts, credentials, or file contents in any exception, log, or persisted record.
- Every task: run Ruff on touched files and `git diff --check` before commit; commit messages end with the Claude co-author trailer.

---

### Task 1: `ProviderId.OPENROUTER`, CLI-transport guard, evidence hosts, operator profile

**Files:**
- Modify: `src/graphite/routing/contracts.py:64-67` (ProviderId enum)
- Modify: `src/graphite/routing/process_runner.py` (CLI-provider guard in `build_cli_environment` and `run_cli_process`)
- Modify: `src/graphite/routing/profiles.py:66-73` (evidence-host mapping) and after `operator_codex_profile` (new factory)
- Test: `tests/test_routing_profiles.py`, `tests/test_routing_process_runner.py`

**Interfaces:**
- Consumes: existing `ProviderId`, `RequestedProfile`, `CliProcessError`.
- Produces: `ProviderId.OPENROUTER` (`"openrouter"`); `operator_openrouter_profile(*, model_id, supported_efforts, evidence_url, evidence_accessed) -> RequestedProfile`; CLI transport rejects non-CLI providers with `CliProcessError("provider_invalid")`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_routing_profiles.py` add:

```python
def test_operator_openrouter_profile_allows_openrouter_evidence_host() -> None:
    profile = operator_openrouter_profile(
        model_id="moonshotai/kimi-k3",
        supported_efforts=(Effort.HIGH,),
        evidence_url="https://openrouter.ai/moonshotai/kimi-k3",
        evidence_accessed="2026-07-20",
    )
    assert profile.provider is ProviderId.OPENROUTER
    assert profile.requested_model == "moonshotai/kimi-k3"


@pytest.mark.parametrize(
    "url",
    [
        "https://developers.openai.com/models/kimi",
        "https://platform.claude.com/docs",
        "http://openrouter.ai/moonshotai/kimi-k3",
    ],
)
def test_operator_openrouter_profile_rejects_foreign_or_insecure_evidence(url: str) -> None:
    with pytest.raises(ProfileError, match="^profile_evidence_invalid$"):
        operator_openrouter_profile(
            model_id="moonshotai/kimi-k3",
            supported_efforts=(Effort.HIGH,),
            evidence_url=url,
            evidence_accessed="2026-07-20",
        )
```

(Import `operator_openrouter_profile` alongside the existing profile imports.)

In `tests/test_routing_process_runner.py` add:

```python
def test_cli_transport_rejects_non_cli_provider(tmp_path: Path) -> None:
    executable = tmp_path / "bin" / "tool.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"binary")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(CliProcessError, match="^provider_invalid$"):
        build_cli_environment(
            provider=ProviderId.OPENROUTER,
            executable=executable,
            workspace=workspace,
            credential_home=None,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=F:\tmp\graphite-claude-codex-router\src python -B -m pytest tests/test_routing_profiles.py tests/test_routing_process_runner.py -q -p no:cacheprovider -k "openrouter_profile or non_cli_provider"`
Expected: FAIL (`AttributeError: OPENROUTER` / `ImportError: operator_openrouter_profile`).

- [ ] **Step 3: Implement**

`contracts.py`:

```python
class ProviderId(StrEnum):
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    OPENROUTER = "openrouter"
```

`process_runner.py` — add below `_CAPACITY_DIAGNOSTICS`:

```python
_CLI_PROVIDERS: Final = frozenset({ProviderId.CLAUDE_CODE, ProviderId.CODEX})
```

In `build_cli_environment`, immediately after `normalized_provider = ProviderId(provider)` (and identically in `run_cli_process` after its normalization):

```python
    if normalized_provider not in _CLI_PROVIDERS:
        raise CliProcessError("provider_invalid")
```

`profiles.py` — replace the inline host selection (lines 66-73) with a mapping:

```python
_EVIDENCE_HOSTS: Final = {
    ProviderId.CLAUDE_CODE: "platform.claude.com",
    ProviderId.CODEX: "developers.openai.com",
    ProviderId.OPENROUTER: "openrouter.ai",
}
```

```python
        parsed = urlparse(self.evidence_url)
        allowed_host = _EVIDENCE_HOSTS[provider]
        if parsed.scheme != "https" or parsed.hostname != allowed_host or parsed.username:
            raise ProfileError("profile_evidence_invalid")
```

After `operator_codex_profile`:

```python
def operator_openrouter_profile(
    *,
    model_id: str,
    supported_efforts: tuple[Effort, ...],
    evidence_url: str,
    evidence_accessed: str,
) -> RequestedProfile:
    """Create an operator-selected OpenRouter request without claiming key validity."""
    return RequestedProfile(
        ProviderId.OPENROUTER,
        model_id,
        supported_efforts,
        evidence_url,
        evidence_accessed,
    )
```

- [ ] **Step 4: Run the focused tests, then the two full modules**

Run: `PYTHONPATH=... python -B -m pytest tests/test_routing_profiles.py tests/test_routing_process_runner.py tests/test_routing_contracts.py -q -p no:cacheprovider`
Expected: PASS (existing CLI-provider tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/graphite/routing/contracts.py src/graphite/routing/process_runner.py src/graphite/routing/profiles.py tests/test_routing_profiles.py tests/test_routing_process_runner.py
git commit -m "feat(routing): OpenRouter provider id, evidence host, CLI-transport guard"
```

---

### Task 2: Inference purpose in the pinned HTTP transport

**Files:**
- Modify: `src/graphite/routing/probe_runner.py` (purpose enum, policy table, purpose-aware caps in `run_http_probe`)
- Test: `tests/test_provider_probe_runner.py`

**Interfaces:**
- Consumes: existing `run_http_probe`, `HttpProbeEndpoint`, `_PURPOSE_POLICY`.
- Produces: `ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS` (`"openrouter_chat_completions"`, POST `/api/v1/chat/completions`); constants `MAX_INFERENCE_REQUEST_BYTES = 1_048_576`, `MAX_INFERENCE_RESPONSE_BYTES = 4_194_304`, `MAX_INFERENCE_TIMEOUT_SECONDS = 600.0`. Non-inference purposes keep the exact existing 30 s / 64 KiB caps.

- [ ] **Step 1: Write the failing tests**

In `tests/test_provider_probe_runner.py` (reuse that module's existing fake resolver/connection fixtures for a 200-JSON response):

```python
def test_inference_purpose_allows_post_body_and_long_deadline() -> None:
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER, "https", "openrouter.ai", 443,
        ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS,
    )
    result = run_http_probe(
        endpoint=endpoint,
        timeout_seconds=480.0,
        request_body=b'{"model":"moonshotai/kimi-k3"}',
        authorization="Bearer test-key",
        max_response_bytes=MAX_INFERENCE_RESPONSE_BYTES,
        resolver=_fake_resolver,
        connection_factory=_fake_connection_factory(b'{"ok":true}'),
    )
    assert result.status_code == 200


def test_inference_purpose_requires_request_body() -> None:
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER, "https", "openrouter.ai", 443,
        ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS,
    )
    with pytest.raises(ProviderProbeError, match="^probe_request_invalid$"):
        run_http_probe(
            endpoint=endpoint, timeout_seconds=10.0, request_body=None,
            authorization="Bearer test-key",
            resolver=_fake_resolver,
            connection_factory=_fake_connection_factory(b"{}"),
        )


def test_non_inference_purposes_keep_thirty_second_ceiling() -> None:
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER, "https", "openrouter.ai", 443,
        ProbeEndpointPurpose.OPENROUTER_MODELS,
    )
    with pytest.raises(ProviderProbeError, match="^probe_request_invalid$"):
        run_http_probe(
            endpoint=endpoint, timeout_seconds=31.0,
            resolver=_fake_resolver,
            connection_factory=_fake_connection_factory(b"{}"),
        )
```

- [ ] **Step 2: Run to verify failure** — `-k inference_purpose or thirty_second` → FAIL (`AttributeError: OPENROUTER_CHAT_COMPLETIONS`).

- [ ] **Step 3: Implement**

```python
class ProbeEndpointPurpose(StrEnum):
    OLLAMA_VERSION = "ollama_version"
    OLLAMA_TAGS = "ollama_tags"
    OLLAMA_SHOW = "ollama_show"
    OPENROUTER_MODELS = "openrouter_models"
    OPENROUTER_AUTH_KEY = "openrouter_auth_key"
    OPENROUTER_CHAT_COMPLETIONS = "openrouter_chat_completions"
```

Policy entry:

```python
    ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS: (
        LifecycleProviderId.OPENROUTER,
        "POST",
        "/api/v1/chat/completions",
    ),
```

Constants beside the existing caps:

```python
MAX_INFERENCE_REQUEST_BYTES: Final = 1_048_576
MAX_INFERENCE_RESPONSE_BYTES: Final = 4_194_304
MAX_INFERENCE_TIMEOUT_SECONDS: Final = 600.0
_BODY_PURPOSES: Final = frozenset(
    {ProbeEndpointPurpose.OLLAMA_SHOW, ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS}
)
_INFERENCE_PURPOSES: Final = frozenset({ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS})
```

In `run_http_probe`, compute purpose-aware ceilings before the big validation `if` and use them in it:

```python
    inference = endpoint.purpose in _INFERENCE_PURPOSES if isinstance(endpoint, HttpProbeEndpoint) else False
    timeout_ceiling = MAX_INFERENCE_TIMEOUT_SECONDS if inference else 30.0
    request_ceiling = MAX_INFERENCE_REQUEST_BYTES if inference else MAX_PROBE_REQUEST_BYTES
    response_ceiling = MAX_INFERENCE_RESPONSE_BYTES if inference else MAX_PROBE_RESPONSE_BYTES
```

Replace `not 0.1 <= timeout_seconds <= 30` with `not 0.1 <= timeout_seconds <= timeout_ceiling`, `<= MAX_PROBE_RESPONSE_BYTES` with `<= response_ceiling` (both the `max_response_bytes` bound and default stay as-is for probes), and `len(request_body) > MAX_PROBE_REQUEST_BYTES` with `len(request_body) > request_ceiling`. Replace the body-required rule:

```python
    if (request_body is not None) is (endpoint.purpose not in _BODY_PURPOSES):
        raise ProviderProbeError("probe_request_invalid")
```

- [ ] **Step 4: Run the whole module** — `tests/test_provider_probe_runner.py` → PASS (all pre-existing probe tests unchanged).

- [ ] **Step 5: Commit** — `feat(routing): pinned inference purpose for OpenRouter chat completions`

---

### Task 3: Pricing capture and cost arithmetic in `openrouter_probe`

**Files:**
- Modify: `src/graphite/routing/openrouter_probe.py`
- Test: `tests/test_provider_openrouter_probe.py`

**Interfaces:**
- Consumes: existing `observe_openrouter` internals, `ProviderProbeError`.
- Produces:
  - `OpenRouterPricing` frozen dataclass: fields `prompt: str`, `completion: str` (non-negative decimal strings ≤ "1", ≤ 64 chars); property `digest -> str` = sha256 of canonical `{"completion": ..., "prompt": ...}` JSON.
  - `OpenRouterObservation` frozen dataclass: `identity: ProviderRuntimeIdentity`, `pricing: OpenRouterPricing`.
  - `observe_openrouter_with_pricing(...)` — same keyword signature as `observe_openrouter`, returns `OpenRouterObservation`.
  - `completion_cost_microunits(pricing: OpenRouterPricing, *, input_tokens: int, output_tokens: int) -> int` — ceiling of `(input*prompt + output*completion) * 1_000_000` in exact `Decimal` arithmetic.
  - `observe_openrouter` keeps its exact current signature and return.

- [ ] **Step 1: Write the failing tests**

```python
def test_observe_with_pricing_binds_catalog_pricing() -> None:
    observation = observe_openrouter_with_pricing(
        endpoint=CANONICAL_ENDPOINT, api_key="k", model_id="moonshotai/kimi-k3",
        routing_policy={"order": ["moonshotai"]}, observed_at=1, policy_version="1.0.0",
        transport=_fake_transport_with_models(
            [{"id": "moonshotai/kimi-k3", "pricing": {"prompt": "0.0000006", "completion": "0.0000025"}}]
        ),
    )
    assert observation.identity.model_identity_digest is not None
    assert observation.pricing.prompt == "0.0000006"
    assert len(observation.pricing.digest) == 64


def test_observe_with_pricing_fails_closed_on_missing_pricing() -> None:
    with pytest.raises(ProviderProbeError, match="^probe_protocol_invalid$"):
        observe_openrouter_with_pricing(
            endpoint=CANONICAL_ENDPOINT, api_key="k", model_id="moonshotai/kimi-k3",
            routing_policy={}, observed_at=1, policy_version="1.0.0",
            transport=_fake_transport_with_models([{"id": "moonshotai/kimi-k3"}]),
        )


def test_completion_cost_rounds_up_in_exact_decimal() -> None:
    pricing = OpenRouterPricing(prompt="0.0000006", completion="0.0000025")
    # 10_000*0.0000006 + 1_000*0.0000025 = 0.0085 USD -> 8_500 microunits
    assert completion_cost_microunits(pricing, input_tokens=10_000, output_tokens=1_000) == 8_500
    # 1*0.0000006 = 0.6 microunits -> ceil -> 1
    assert completion_cost_microunits(pricing, input_tokens=1, output_tokens=0) == 1
```

(`_fake_transport_with_models(data)` follows the module's existing fake-transport pattern: auth call returns `{"data": {}}`-style dict, models call returns `{"data": data}`.)

- [ ] **Step 2: Run to verify failure** — ImportError on the new names.

- [ ] **Step 3: Implement**

```python
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_PRICE = re.compile(r"^(0|[0-9]{1,10}(\.[0-9]{1,18})?|\.[0-9]{1,18})$")


@dataclass(frozen=True, slots=True)
class OpenRouterPricing:
    prompt: str
    completion: str

    def __post_init__(self) -> None:
        for value in (self.prompt, self.completion):
            if (
                not isinstance(value, str)
                or len(value) > 64
                or _PRICE.fullmatch(value) is None
            ):
                raise ProviderProbeError("probe_protocol_invalid")
            try:
                parsed = Decimal(value)
            except InvalidOperation:
                raise ProviderProbeError("probe_protocol_invalid") from None
            if not Decimal(0) <= parsed <= Decimal(1):
                raise ProviderProbeError("probe_protocol_invalid")

    @property
    def digest(self) -> str:
        return _digest({"completion": self.completion, "prompt": self.prompt})


@dataclass(frozen=True, slots=True)
class OpenRouterObservation:
    identity: ProviderRuntimeIdentity
    pricing: OpenRouterPricing


def completion_cost_microunits(
    pricing: OpenRouterPricing, *, input_tokens: int, output_tokens: int
) -> int:
    for value in (input_tokens, output_tokens):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100_000_000:
            raise ProviderProbeError("probe_request_invalid")
    cost = (
        Decimal(input_tokens) * Decimal(pricing.prompt)
        + Decimal(output_tokens) * Decimal(pricing.completion)
    ) * Decimal(1_000_000)
    whole = int(cost)
    return whole if cost == whole else whole + 1
```

Refactor: move the body of `observe_openrouter` into `_observe(...)` that, at the point where `model_id in identifiers` is confirmed, also selects the matched entry and returns `(identity, entry)`. Then:

```python
def observe_openrouter(...existing signature...) -> ProviderRuntimeIdentity:
    return _observe(...)[0]


def observe_openrouter_with_pricing(...same keywords...) -> OpenRouterObservation:
    identity, entry = _observe(...)
    raw = entry.get("pricing")
    if not isinstance(raw, dict):
        raise ProviderProbeError("probe_protocol_invalid")
    pricing = OpenRouterPricing(prompt=raw.get("prompt"), completion=raw.get("completion"))
    return OpenRouterObservation(identity, pricing)
```

- [ ] **Step 4: Run the whole module** — all existing `observe_openrouter` tests must pass unchanged.

- [ ] **Step 5: Commit** — `feat(routing): OpenRouter pricing capture and exact cost arithmetic`

---

### Task 4: `openrouter_executor` — identity and preflight

**Files:**
- Create: `src/graphite/routing/openrouter_executor.py`
- Test: `tests/test_routing_openrouter_executor.py` (new)

**Interfaces:**
- Consumes: `observe_openrouter_with_pricing`, `OpenRouterPricing`, `completion_cost_microunits`, `CliIdentity`, `AdapterError` (import from `claude_executor` exactly as `codex_executor` does), `run_http_probe`.
- Produces:
  - `ADAPTER_PROTOCOL_VERSION = "1.0.0"`, `API_CONTRACT_VERSION = "1.0.0"`, `CANONICAL_ENDPOINT = "https://openrouter.ai/api/v1"`.
  - `OpenRouterPreflight` frozen dataclass: `identity: CliIdentity`, `runtime: ProviderRuntimeIdentity`, `pricing: OpenRouterPricing`.
  - `preflight_openrouter(*, api_key: str, model_id: str, routing_policy: Mapping[str, object], observed_at: int, policy_version: str, transport=run_http_probe) -> OpenRouterPreflight`.
  - Composite identity digest: sha256 of canonical JSON `{"endpoint": runtime.runtime_digest, "model": runtime.model_identity_digest, "pricing": pricing.digest, "routing": runtime.routing_policy_digest}`; stored as `CliIdentity(ProviderId.OPENROUTER, <that digest>, API_CONTRACT_VERSION, ADAPTER_PROTOCOL_VERSION)`.

- [ ] **Step 1: Write the failing test**

```python
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
    assert preflight.identity.cli_version == "1.0.0"
    expected = hashlib.sha256(json.dumps({
        "endpoint": preflight.runtime.runtime_digest,
        "model": preflight.runtime.model_identity_digest,
        "pricing": preflight.pricing.digest,
        "routing": preflight.runtime.routing_policy_digest,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert preflight.identity.executable_sha256 == expected
```

- [ ] **Step 2: Run to verify failure** — module does not exist.

- [ ] **Step 3: Implement** the module skeleton with the constants, dataclass, digest helper, and `preflight_openrouter` delegating to `observe_openrouter_with_pricing(endpoint=CANONICAL_ENDPOINT, ...)`; any `ProviderProbeError` is re-raised as `AdapterError(<mapped code>)` with mapping `probe_model_unavailable -> "model_unavailable"`, `probe_auth_unhealthy -> "auth_required"`, everything else `"unavailable"`.

- [ ] **Step 4: Run** the new test file → PASS.

- [ ] **Step 5: Commit** — `feat(routing): OpenRouter executor identity and preflight`

---

### Task 5: `execute_openrouter` — one bounded schema-bound inference call

**Files:**
- Modify: `src/graphite/routing/openrouter_executor.py`
- Test: `tests/test_routing_openrouter_executor.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True, slots=True)
class OpenRouterExecutionResult:
    effective_model: str
    message: str  # canonical JSON of the schema-validated payload
    input_tokens: int
    output_tokens: int
    cost_microunits: int
    duration_seconds: float
    request_sha256: str
    response_sha256: str


def execute_openrouter(
    *,
    api_key: str,
    prompt: bytes,
    requested_model: str,
    expected_effective_model: str,
    effort: Effort,                      # LOW/MEDIUM/HIGH only; others request_invalid
    output_schema: dict[str, object],    # required, non-empty
    output_schema_sha256: str,           # sha256 of canonical serialization, must match
    pricing: OpenRouterPricing,
    max_output_tokens: int,
    max_cost_microunits: int,
    timeout_seconds: float,
    transport: HttpProbe = run_http_probe,
) -> OpenRouterExecutionResult: ...
```

- Request body (canonical, deterministic key order via `json.dumps(..., sort_keys=True)`):

```python
{
  "max_tokens": max_output_tokens,
  "messages": [{"content": prompt.decode("utf-8"), "role": "user"}],
  "model": requested_model,
  "reasoning": {"effort": effort.value},
  "response_format": {"json_schema": {"name": "graphite_response", "schema": output_schema, "strict": True}, "type": "json_schema"},
  "stream": False,
  "temperature": 0,
  "usage": {"include": True},
}
```

- Failure codes: `request_invalid` (bad params, schema digest mismatch, undecodable prompt), `auth_required` (missing/empty key), `unavailable` (transport/HTTP/parse failures), `response_contract_invalid` (content not valid JSON or schema-shape violations the adapter re-checks: must be a JSON object), `model_mismatch` (response `model` field present and ≠ expected), `protocol` (missing/invalid usage), `cost_ceiling_exceeded`.

- [ ] **Step 1: Write the failing tests** (fake transport records the `request_body` and returns a canned completions response):

```python
def _completion(content: str, *, model: str = "moonshotai/kimi-k3",
                prompt_tokens: int = 100, completion_tokens: int = 50) -> bytes:
    return json.dumps({
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }).encode()


def test_execute_builds_canonical_schema_bound_request_and_returns_canonical_json():
    schema = {"additionalProperties": False, "properties": {"result": {"const": "GRAPHITE_EDIT_OK", "type": "string"}}, "required": ["result"], "type": "object"}
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    pricing = OpenRouterPricing(prompt="0.000001", completion="0.000002")
    outcome = execute_openrouter(
        api_key="k", prompt=b"do the approved task",
        requested_model="moonshotai/kimi-k3", expected_effective_model="moonshotai/kimi-k3",
        effort=Effort.HIGH, output_schema=schema,
        output_schema_sha256=_canonical_sha256(schema),
        pricing=pricing, max_output_tokens=4096,
        max_cost_microunits=10_000, timeout_seconds=120.0,
        transport=transport,
    )
    assert outcome.message == '{"result":"GRAPHITE_EDIT_OK"}'
    assert outcome.cost_microunits == 200  # 100*1 + 50*2 microunits
    body = json.loads(transport.request_body)
    assert body["temperature"] == 0 and body["stream"] is False
    assert body["response_format"]["json_schema"]["strict"] is True
    assert transport.endpoint.purpose is ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS


def test_execute_fails_closed_over_cost_ceiling():
    # 100*1 + 50*2 = 200 microunits > ceiling 199
    ... same call with max_cost_microunits=199 ...
    pytest.raises(AdapterError, match="^cost_ceiling_exceeded$")


def test_execute_rejects_non_json_content_and_wrong_model_echo():
    # content "prose" -> response_contract_invalid; model="other/model" -> model_mismatch
```

Also: schema digest mismatch → `request_invalid` with **zero transport calls**; missing usage → `protocol`; effort `Effort.XHIGH` → `request_invalid`.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** Serialize the body with `json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()`; call `transport(endpoint=HttpProbeEndpoint(LifecycleProviderId.OPENROUTER, "https", "openrouter.ai", 443, ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS), timeout_seconds=timeout_seconds, request_body=body, authorization=f"Bearer {api_key}", max_response_bytes=MAX_INFERENCE_RESPONSE_BYTES)`. Parse: exactly one choice; `message.content` must `json.loads` to a `dict`; canonical re-dump is `message`. Usage ints via bounded `_token` (mirror `codex_executor._token`). Cost via `completion_cost_microunits`; compare ≤ `max_cost_microunits`. `request_sha256`/`response_sha256` are sha256 hexdigests of the request body bytes and `HttpProbeResult.body`.

- [ ] **Step 4: Run the module tests** → PASS.

- [ ] **Step 5: Commit** — `feat(routing): bounded schema-bound OpenRouter execution`

---

### Task 6: Whole-file edit engine

**Files:**
- Modify: `src/graphite/routing/openrouter_executor.py` (add `apply_whole_file_edit`)
- Test: `tests/test_routing_openrouter_executor.py`

**Interfaces:**
- Produces:

```python
MAX_EDIT_FILE_BYTES: Final = 1_048_576
def apply_whole_file_edit(
    *,
    workspace: Path,
    payload: dict[str, object],          # json.loads(result.message)
    edit_scope: tuple[str, ...],         # exact relative POSIX paths
    max_total_bytes: int,
) -> tuple[str, ...]:                    # sorted applied paths
```

- Contract: `payload` must be exactly `{"result": "GRAPHITE_EDIT_OK", "files": [...]}` where the set of `files[].path` equals `set(edit_scope)` with no duplicates. Each path: `str`, relative, `/` separators only, no `\\`, no empty/`.`/`..` segments, no `:` (drive), ≤ 512 chars; resolved target must stay under `workspace` and no existing component from workspace to target may be a symlink or reparse point. Each `content`: `str`, UTF-8 encodable, encoded length ≤ `MAX_EDIT_FILE_BYTES`; the sum ≤ `max_total_bytes`. Violations raise `AdapterError("edit_scope_violation")` **before any filesystem change**.
- Atomicity: read and hold every target's original bytes (missing file allowed only if the manifest scope says so — for this engine every scope path must already exist; a missing target is `edit_scope_violation`), write all `*.graphite-tmp` siblings, then `os.replace` each into place; if any replace fails, restore already-replaced files from held originals and raise `AdapterError("edit_apply_failed")` — restore failures raise `edit_apply_failed` too (worktree is then quarantined by the harness, never promoted).

- [ ] **Step 1: Write the failing tests** — the hostile battery, each expecting `edit_scope_violation` with the workspace tree byte-identical afterwards (helper snapshots the tree into a dict before the call):
  - traversal `"../escape.py"`, absolute `"C:/x.py"` and `"/x.py"`, backslash `"src\\access.py"`, duplicate paths, scope omission (only one of two files), scope extra, missing `result` marker, wrong marker, `content` over `MAX_EDIT_FILE_BYTES`, aggregate over `max_total_bytes`, symlinked parent directory (create with `os.symlink`, `pytest.importorskip`-guarded on Windows privileges — use the same skip pattern the overlay tests use), non-existent scope target.
  - Success case: two files replaced byte-exactly (CRLF content preserved), return value sorted, temp files gone.
  - Mid-set failure: monkeypatch `os.replace` to fail on the second call → first file restored to original bytes, `edit_apply_failed` raised.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** exactly per the contract above (validation pass fully before the staging pass; staging pass fully before the replace pass).

- [ ] **Step 4: Run module tests + Ruff.**

- [ ] **Step 5: Commit** — `feat(routing): atomic whole-file edit engine for OpenRouter development edits`

---

### Task 7: Snapshot, lifecycle-binding, and route-pool parity for OpenRouter

**Files:**
- Test: `tests/test_routing_profiles.py`, `tests/test_routing_storage.py`, `tests/test_route_pool.py`
- Modify: only if a test exposes a provider-assumption gap (expected: none in `storage.py`/`route_pool.py`; possible small fix in any helper that maps `ProviderId` to `LifecycleProviderId` — extend the mapping table with `OPENROUTER`).

**Interfaces:**
- Consumes: `create_capability_snapshot`, `save_capability_snapshot`, `load_verified_capability_snapshots`, `save_lifecycle_snapshot_binding`, `verify_and_save_approved_edit_profile`, route-pool candidate/loader types.
- Produces: proof (tests) that an OpenRouter read-only snapshot round-trips storage, binds to a lifecycle identity, promotes to workspace-write through the existing promotion boundary, and loads as a route-pool candidate with `routing_policy_digest` required.

- [ ] **Step 1: Write the failing/passing tests**

```python
def _openrouter_snapshot(now: int) -> CapabilitySnapshot:
    requested = operator_openrouter_profile(
        model_id="moonshotai/kimi-k3", supported_efforts=(Effort.HIGH,),
        evidence_url="https://openrouter.ai/moonshotai/kimi-k3", evidence_accessed="2026-07-20",
    )
    identity = CliIdentity(ProviderId.OPENROUTER, "a" * 64, "1.0.0", "1.0.0")
    return create_capability_snapshot(
        requested=requested, identity=identity, effective_model="moonshotai/kimi-k3",
        effort=Effort.HIGH, capabilities=("code", "reasoning"),
        context_window_tokens=262_144, risk_ceiling=RiskTier.HIGH,
        permission_mode=PermissionMode.READ_ONLY, verified_at=now, ttl_seconds=86_400,
    )
```

Tests: storage round-trip preserves the digest and provider; lifecycle binding save/load; `verify_and_save_approved_edit_profile` promotes it with an `EditSmokeEvidence` exactly as the Codex tests do; a route-pool candidate built from the OpenRouter lifecycle identity (with `routing_policy_digest` set) is accepted and selected by the existing pool machinery.

- [ ] **Step 2: Run** — fix any surfaced provider-assumption gap minimally (each fix gets its own assertion).

- [ ] **Step 3: Commit** — `test(routing): OpenRouter snapshot, promotion, and route-pool parity`

---

### Task 8: Documentation, scope revocation, and full offline verification

**Files:**
- Modify: `docs/superpowers/implementation-notes/2026-07-18-claude-codex-router-evidence.md` (append a dated scope-revocation note under "Scope and authority" — do not rewrite history)
- Modify: `docs/superpowers/implementation-notes/2026-07-19-provider-lifecycle-evidence.md` (new section: offline OpenRouter implementation evidence with test counts)
- Test: whichever documentation-consistency tests exist in `tests/` that assert on these docs (run the documentation selection to find out; update them alongside)

**Steps:**

- [ ] Append the scope note: operator revoked "OpenRouter is reserved for application inference" on 2026-07-20; development participation is governed by the same snapshot/lifecycle/manifest regime; reference the spec path.
- [ ] Write the offline-evidence section with the real test numbers from this run.
- [ ] Run, in order, and record results: focused new selections (`tests/test_routing_openrouter_executor.py tests/test_provider_openrouter_probe.py tests/test_provider_probe_runner.py tests/test_routing_profiles.py tests/test_routing_process_runner.py`); the full routing selection (the 36-module list used in this branch's prior rounds); the complete suite `tests` with an out-of-repo `--basetemp`; `python -m ruff check src tests`; `git diff --check`; `python -m graphite build .` then `check .` (fresh required).
- [ ] Commit — `docs: record offline OpenRouter development participation implementation`

---

### Task 9: Live-acceptance harness authoring (stops at the approval gate)

**Files:**
- Create: `F:\tmp\graphite-live-acceptance-harness\_prepare_openrouter_probe.py`
- Create: `F:\tmp\graphite-live-acceptance-harness\_execute_openrouter_probe.py`
- Create: `F:\tmp\graphite-live-acceptance-harness\openrouter-edit-output-schema.json` (the whole-file schema with `edit_scope` paths and `GRAPHITE_EDIT_OK` const)

**Interfaces:**
- Consumes: `preflight_openrouter`, `observe_openrouter_with_pricing`, the rN harness conventions (bundle dict → sha256 digest → `--approved` gate, sanitized JSON receipts, store/worktree preflights).
- Produces: a catalog-probe bundle for the five slugs (`moonshotai/kimi-k3`, `moonshotai/kimi-k2.7-code`, `moonshotai/kimi-k2.6`, `z-ai/glm-5.2`, `meta/muse-spark-1.1`) whose execution performs **no inference** — auth health once, then per-slug catalog membership + pricing capture — and prints per-slug `{exists, pricing, identity_digest}` receipts. The API key is read only from the `OPENROUTER_API_KEY` session environment variable at execute time; absent key fails closed as `credential_missing`; the key never appears in the bundle, receipts, or argv.

**Steps:**

- [ ] Write the prepare script following `_prepare_live_r12.py` conventions (fresh `ISSUED_AT`/`EXPIRES_AT` at authoring time, purpose `graphite_openrouter_catalog_probe_batch`, allowed failure categories including `credential_missing`, `probe_model_unavailable`, `probe_auth_unhealthy`, and allowed evidence limited to slug, existence, pricing strings, digests, durations).
- [ ] Write the execute script with the standard preflight (bundle digest, expiry, implementation commit, clean worktree) and the per-slug probe loop; ruff-check both; `python _prepare_openrouter_probe.py` prints the bundle and digest.
- [ ] **STOP.** Display the complete bundle and request explicit operator approval. Verification bundles, edit smokes, reviews, and pool registration are authored only after the probe results exist, each behind its own approval.

---

## Self-Review Notes

- Spec coverage: executor+preflight (Tasks 4-5), edit engine (6), identity/profiles (1, 4), cost ceiling (3, 5), transport discipline (2), pool/snapshot parity (7), docs/scope revocation (8), live order (9, gated). Review-role execution needs no new code beyond Task 5 (the review schema is just a different `output_schema`).
- Type consistency: `OpenRouterPricing`/`completion_cost_microunits` defined in Task 3, consumed in Tasks 4-5 with matching signatures; `HttpProbeEndpoint` purpose from Task 2 used in Task 5's transport assertion.
- No placeholders: every step carries code or exact commands; Task 7's "fix only if a test exposes a gap" is bounded by named modules and the mapping-table remedy.

---

## Live-acceptance status (post-plan, manifest-gated)

Live acceptance runs as the separate manifest-gated phase noted in the Global
Constraints and Task 9, on the disposable fixture
`F:\tmp\graphite-production-live-fixture` (routing store 12/12/21). Each step
is behind its own operator approval:

- **Catalog probe** — DONE (five slugs; auth health once + per-slug catalog
  existence and pricing capture, no inference).
- **Verification** — DONE for `moonshotai/kimi-k2.7-code` and
  `moonshotai/kimi-k2.6` (both lifecycle-ACTIVE); `moonshotai/kimi-k3`,
  `z-ai/glm-5.2`, and `meta/muse-spark-1.1` remain `verification_required`.
- **Edit smokes** — DONE (r7 `kimi-k2.7-code`, r8 `kimi-k2.6`; single-call
  `json_object` shape; both edit-promoted; identical `diff_sha256
  005f1ae8…`). Root cause of the earlier truncation was strict `json_schema`
  structured output constraining free-form content; `json_object` fixed it.
- **Pool registration** — ✅ **DONE 2026-07-20.** Offline, read-only
  loadability + selectability proof: both edit-promoted models construct as an
  ordered WORKSPACE_WRITE `ApprovedRoutePool` and `select_route` selects
  `kimi-k2.7-code` primary (empty attempts) then `kimi-k2.6` after one
  synthesized `capacity_unavailable` attempt, each against its live ACTIVE
  `RouteAuthority`. Diagnostic positive (`edit_snapshots_directly_selectable` —
  no re-activation needed); `mutated: false`; both stores byte-unchanged; no
  `ApprovalAuthority.issue`, no network, no graphite source change. Spec:
  `docs/superpowers/specs/2026-07-20-openrouter-pool-registration-design.md`;
  plan: `docs/superpowers/plans/2026-07-20-openrouter-pool-registration.md`;
  evidence:
  `docs/superpowers/implementation-notes/2026-07-19-provider-lifecycle-evidence.md`
  (commit `7454283`).

This satisfies the parent spec's live-acceptance success criterion "verified
models are loadable pool candidates for their categories" for the edit
(ISOLATED_CODE / WORKSPACE_WRITE) category on both edit-promoted models.

Deferred (future operator decisions, each its own approval): the read-only
review / authorization pool; a live routed smoke through
`execute_approved_route_pool` (the persisted, signed-pool path — nothing was
persisted by the offline proof); the three unverified models; and branch
integration (push/merge of `feat/claude-codex-router`).
