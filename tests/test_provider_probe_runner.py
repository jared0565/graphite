"""Bounded, non-inference provider probe boundary tests."""
from __future__ import annotations

import hashlib
import http.server
import socket
import sys
import threading
import time
from email.message import Message
from pathlib import Path

import pytest

import graphite.routing.probe_runner as probe_runner_module
from graphite.probe_process import ProbeProcessResult
from graphite.routing.lifecycle import LifecycleProviderId
from graphite.routing.probe_runner import (
    MAX_CATALOG_RESPONSE_BYTES,
    MAX_INFERENCE_RESPONSE_BYTES,
    HttpProbeEndpoint,
    ProbeEndpointPurpose,
    ProviderProbeError,
    run_http_probe,
    run_process_probe,
)

FAKE_CLI = Path(__file__).parent / "fake_clis" / "fake_cli.py"


class _FakeSocket:
    def __init__(self, peer: str) -> None:
        self._peer = peer

    def getpeername(self) -> tuple[str, int]:
        return self._peer, 443

    def settimeout(self, timeout: float) -> None:
        assert timeout > 0


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b'{"ok":true}',
        content_type: str = "application/json",
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.status = status
        self._body = body
        self._offset = 0
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(body))
        for name, value in extra_headers:
            self.headers[name] = value

    def read(self, amount: int) -> bytes:
        chunk = self._body[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk


class _FakeConnection:
    def __init__(self, peer: str, response: _FakeResponse) -> None:
        self.sock = _FakeSocket(peer)
        self.response = response
        self.request_args: tuple[object, ...] | None = None
        self.closed = False

    def connect(self) -> None:
        return None

    def request(self, *args: object, **kwargs: object) -> None:
        self.request_args = (*args, kwargs)

    def getresponse(self) -> _FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def _resolver(address: str) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


def test_process_probe_is_empty_input_single_shot_and_bounded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    observed: dict[str, object] = {}

    def runner(argv: list[str], **kwargs: object) -> ProbeProcessResult:
        observed["argv"] = argv
        observed.update(kwargs)
        return ProbeProcessResult(0, b"2.1.214\n", b"", 0.01)

    result = run_process_probe(
        argv=(sys.executable, str(FAKE_CLI), "echo"),
        cwd=workspace,
        provider=LifecycleProviderId.CLAUDE_CODE,
        credential_home=None,
        timeout_seconds=2,
        runner=runner,
        source_environment={},
    )

    assert observed["stdin"] == b""
    assert observed["check"] is False
    assert result.stdout == b"2.1.214\n"


def test_process_probe_preserves_only_sanitized_nonzero_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def runner(argv: list[str], **kwargs: object) -> ProbeProcessResult:
        return ProbeProcessResult(
            1,
            b"PRIVATE stdout",
            b"Selected model is at capacity. Please try a different model.",
            0.25,
        )

    with pytest.raises(ProviderProbeError, match="^process_nonzero$") as caught:
        run_process_probe(
            argv=(sys.executable, str(FAKE_CLI), "echo"),
            cwd=workspace,
            provider=LifecycleProviderId.CLAUDE_CODE,
            credential_home=None,
            timeout_seconds=2,
            runner=runner,
            source_environment={},
        )

    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics.failure_category == "capacity_unavailable"
    assert "PRIVATE" not in repr(caught.value.diagnostics)


def test_openrouter_probe_pins_public_address_and_injects_credential_at_boundary() -> None:
    response = _FakeResponse()
    connection = _FakeConnection("93.184.216.34", response)
    endpoint = HttpProbeEndpoint(
        provider=LifecycleProviderId.OPENROUTER,
        scheme="https",
        host="openrouter.ai",
        port=443,
        purpose=ProbeEndpointPurpose.OPENROUTER_AUTH_KEY,
    )

    result = run_http_probe(
        endpoint=endpoint,
        timeout_seconds=2,
        authorization="Bearer private-value",
        resolver=lambda *_args, **_kwargs: _resolver("93.184.216.34"),
        connection_factory=lambda *_args: connection,
    )

    assert result.status_code == 200
    assert result.body_sha256 == hashlib.sha256(b'{"ok":true}').hexdigest()
    assert connection.request_args is not None
    headers = connection.request_args[-1]["headers"]
    assert headers["Authorization"] == "Bearer private-value"
    assert "private-value" not in repr(result)
    assert connection.closed is True


def test_chunked_response_socket_close_after_final_read_is_not_a_failure() -> None:
    """OpenRouter sends chunked bodies; http.client closes the socket at the
    final chunk under Connection: close, and the read loop must not touch the
    dead socket again (live failure 2026-07-20: WinError 10038 on settimeout
    surfaced as probe_unavailable on the first authenticated call)."""

    class _ClosingSocket:
        def __init__(self) -> None:
            self.closed = False

        def getpeername(self) -> tuple[str, int]:
            return ("93.184.216.34", 443)

        def settimeout(self, timeout: float) -> None:
            if self.closed:
                raise OSError(10038, "operation on non-socket")
            assert timeout > 0

    class _ChunkedResponse:
        def __init__(self, body: bytes, sock: _ClosingSocket) -> None:
            self.status = 200
            self._body = body
            self._offset = 0
            self._socket = sock
            self.headers = Message()
            self.headers["Content-Type"] = "application/json"

        def read(self, amount: int) -> bytes:
            chunk = self._body[self._offset : self._offset + amount]
            self._offset += len(chunk)
            if self._offset >= len(self._body):
                self._socket.closed = True
            return chunk

        def isclosed(self) -> bool:
            return self._offset >= len(self._body)

    sock = _ClosingSocket()
    response = _ChunkedResponse(b'{"ok":true}', sock)

    class _Connection:
        def __init__(self) -> None:
            self.sock = sock

        def connect(self) -> None:
            return None

        def request(self, *args: object, **kwargs: object) -> None:
            return None

        def getresponse(self) -> _ChunkedResponse:
            return response

        def close(self) -> None:
            return None

    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER,
        "https",
        "openrouter.ai",
        443,
        ProbeEndpointPurpose.OPENROUTER_AUTH_KEY,
    )
    result = run_http_probe(
        endpoint=endpoint,
        timeout_seconds=5,
        authorization="Bearer test-key",
        resolver=lambda *_args, **_kwargs: _resolver("93.184.216.34"),
        connection_factory=lambda *_args: _Connection(),
    )
    assert result.status_code == 200
    assert result.body == b'{"ok":true}'


def test_inference_purpose_allows_post_body_and_long_deadline() -> None:
    connection = _FakeConnection("93.184.216.34", _FakeResponse())
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER,
        "https",
        "openrouter.ai",
        443,
        ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS,
    )

    result = run_http_probe(
        endpoint=endpoint,
        timeout_seconds=480.0,
        request_body=b'{"model":"moonshotai/kimi-k3"}',
        authorization="Bearer test-key",
        max_response_bytes=MAX_INFERENCE_RESPONSE_BYTES,
        resolver=lambda *_args, **_kwargs: _resolver("93.184.216.34"),
        connection_factory=lambda *_args: connection,
    )

    assert result.status_code == 200
    assert connection.request_args is not None
    assert connection.request_args[0] == "POST"
    assert connection.request_args[1] == "/api/v1/chat/completions"


def test_inference_purpose_requires_request_body() -> None:
    connection = _FakeConnection("93.184.216.34", _FakeResponse())
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER,
        "https",
        "openrouter.ai",
        443,
        ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS,
    )
    with pytest.raises(ProviderProbeError, match="^probe_request_invalid$"):
        run_http_probe(
            endpoint=endpoint,
            timeout_seconds=10.0,
            request_body=None,
            authorization="Bearer test-key",
            resolver=lambda *_args, **_kwargs: _resolver("93.184.216.34"),
            connection_factory=lambda *_args: connection,
        )


def test_models_catalog_purpose_allows_bounded_large_response() -> None:
    body = (b'{"data":"' + b"x" * 100_000 + b'"}')
    connection = _FakeConnection("93.184.216.34", _FakeResponse(body=body))
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER,
        "https",
        "openrouter.ai",
        443,
        ProbeEndpointPurpose.OPENROUTER_MODELS,
    )
    result = run_http_probe(
        endpoint=endpoint,
        timeout_seconds=10.0,
        authorization="Bearer test-key",
        max_response_bytes=MAX_CATALOG_RESPONSE_BYTES,
        resolver=lambda *_args, **_kwargs: _resolver("93.184.216.34"),
        connection_factory=lambda *_args: connection,
    )
    assert result.status_code == 200
    assert len(result.body) == len(body)


def test_non_catalog_probe_purposes_keep_sixty_four_kib_response_ceiling() -> None:
    connection = _FakeConnection("93.184.216.34", _FakeResponse())
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER,
        "https",
        "openrouter.ai",
        443,
        ProbeEndpointPurpose.OPENROUTER_AUTH_KEY,
    )
    with pytest.raises(ProviderProbeError, match="^probe_request_invalid$"):
        run_http_probe(
            endpoint=endpoint,
            timeout_seconds=10.0,
            authorization="Bearer test-key",
            max_response_bytes=64 * 1024 + 1,
            resolver=lambda *_args, **_kwargs: _resolver("93.184.216.34"),
            connection_factory=lambda *_args: connection,
        )


def test_non_inference_purposes_keep_thirty_second_ceiling() -> None:
    connection = _FakeConnection("93.184.216.34", _FakeResponse())
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER,
        "https",
        "openrouter.ai",
        443,
        ProbeEndpointPurpose.OPENROUTER_MODELS,
    )
    with pytest.raises(ProviderProbeError, match="^probe_request_invalid$"):
        run_http_probe(
            endpoint=endpoint,
            timeout_seconds=31.0,
            resolver=lambda *_args, **_kwargs: _resolver("93.184.216.34"),
            connection_factory=lambda *_args: connection,
        )


def test_real_loopback_ollama_metadata_probe_uses_allowlisted_ephemeral_port() -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert self.path == "/api/version"
            body = b'{"version":"0.12.0"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        endpoint = HttpProbeEndpoint(
            LifecycleProviderId.OLLAMA,
            "http",
            "127.0.0.1",
            port,
            ProbeEndpointPurpose.OLLAMA_VERSION,
            allowed_ollama_ports=frozenset({port}),
        )
        result = run_http_probe(
            endpoint=endpoint,
            timeout_seconds=2,
            resolver=lambda *_args, **_kwargs: [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("127.0.0.1", port),
                )
            ],
        )
        assert result.body == b'{"version":"0.12.0"}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("provider", "scheme", "host", "port", "purpose", "code"),
    [
        (
            LifecycleProviderId.OLLAMA,
            "http",
            "192.168.1.20",
            11434,
            ProbeEndpointPurpose.OLLAMA_VERSION,
            "probe_endpoint_invalid",
        ),
        (
            LifecycleProviderId.OPENROUTER,
            "http",
            "openrouter.ai",
            443,
            ProbeEndpointPurpose.OPENROUTER_MODELS,
            "probe_endpoint_invalid",
        ),
        (
            LifecycleProviderId.OPENROUTER,
            "https",
            "example.com",
            443,
            ProbeEndpointPurpose.OPENROUTER_MODELS,
            "probe_endpoint_invalid",
        ),
    ],
)
def test_endpoint_policy_rejects_unapproved_destinations(
    provider: LifecycleProviderId,
    scheme: str,
    host: str,
    port: int,
    purpose: ProbeEndpointPurpose,
    code: str,
) -> None:
    with pytest.raises(ProviderProbeError, match=f"^{code}$"):
        HttpProbeEndpoint(provider, scheme, host, port, purpose)


def test_openrouter_rejects_private_dns_and_peer_rebinding() -> None:
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER,
        "https",
        "openrouter.ai",
        443,
        ProbeEndpointPurpose.OPENROUTER_MODELS,
    )
    with pytest.raises(ProviderProbeError, match="^probe_address_invalid$"):
        run_http_probe(
            endpoint=endpoint,
            timeout_seconds=2,
            resolver=lambda *_args, **_kwargs: _resolver("127.0.0.1"),
        )

    connection = _FakeConnection("127.0.0.1", _FakeResponse())
    with pytest.raises(ProviderProbeError, match="^probe_peer_invalid$"):
        run_http_probe(
            endpoint=endpoint,
            timeout_seconds=2,
            resolver=lambda *_args, **_kwargs: _resolver("93.184.216.34"),
            connection_factory=lambda *_args: connection,
        )


def test_dns_timeout_is_bounded_and_failure_contains_no_endpoint() -> None:
    """The probe must abandon a resolver that never returns, and name no endpoint.

    This asserted `elapsed < 0.2` against a resolver that slept 0.2s, i.e. it
    proved "did not wait for the resolver" by racing a 0.1s budget against a
    0.2s sleep. That margin does not exist on darwin. MEASURED on
    macos-latest 3.12.10, nothing else running, a BARE `queue.get(timeout=0.1)`
    with no probe involved at all:

        macos     min 0.1003   median 0.1596   max 0.1751
        ubuntu    min 0.1001   median 0.1002   max 0.1002

    So darwin overshoots a 0.1s timed wait by up to 75%, and `run_http_probe`
    measured no worse than that bare control -- the probe was never the slow
    part. Under full-suite load the same jitter reached 0.238s and failed the
    old bound, which is the last of graphite#46's macOS failures.

    A blocking resolver removes the race instead of widening it. It cannot
    return, so `probe_timeout` reaching this frame is itself proof that the
    probe stopped waiting -- a structural fact rather than a timing comparison.
    The elapsed bound stays only as a hang guard, and is now loose enough that
    no scheduler can trip it while still failing well before the resolver's own
    cap. Do not "tighten" it back: precision here measures the platform's timer,
    not this code.
    """
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER,
        "https",
        "openrouter.ai",
        443,
        ProbeEndpointPurpose.OPENROUTER_MODELS,
    )
    released = threading.Event()

    def resolver(*args: object, **kwargs: object) -> list[object]:
        released.wait(5.0)
        return []

    started = time.monotonic()
    try:
        with pytest.raises(ProviderProbeError, match="^probe_timeout$") as caught:
            run_http_probe(endpoint=endpoint, timeout_seconds=0.1, resolver=resolver)
        elapsed = time.monotonic() - started
    finally:
        # Let the worker finish now rather than sit out its cap holding a DNS
        # slot, which the next test would see as `probe_dns_busy`.
        released.set()
    assert elapsed < 2.0
    assert "openrouter" not in str(caught.value)


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (_FakeResponse(status=302), "probe_redirect_rejected"),
        (_FakeResponse(content_type="text/html"), "probe_content_type_invalid"),
        (_FakeResponse(body=b"x" * 1025), "probe_response_limit"),
        (_FakeResponse(body=b"\xff"), "probe_protocol_invalid"),
        (_FakeResponse(body=b"not-json"), "probe_protocol_invalid"),
    ],
)
def test_http_probe_rejects_redirect_malformed_type_and_oversize(
    response: _FakeResponse,
    code: str,
) -> None:
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER,
        "https",
        "openrouter.ai",
        443,
        ProbeEndpointPurpose.OPENROUTER_MODELS,
    )
    with pytest.raises(ProviderProbeError, match=f"^{code}$"):
        run_http_probe(
            endpoint=endpoint,
            timeout_seconds=2,
            max_response_bytes=1024,
            resolver=lambda *_args, **_kwargs: _resolver("93.184.216.34"),
            connection_factory=lambda *_args: _FakeConnection(
                "93.184.216.34", response
            ),
        )


def test_http_failure_does_not_reflect_body_or_credential() -> None:
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER,
        "https",
        "openrouter.ai",
        443,
        ProbeEndpointPurpose.OPENROUTER_AUTH_KEY,
    )
    response = _FakeResponse(status=429, body=b"PRIVATE provider account detail")
    with pytest.raises(ProviderProbeError, match="^probe_http_status$") as caught:
        run_http_probe(
            endpoint=endpoint,
            timeout_seconds=2,
            authorization="Bearer PRIVATE-credential",
            resolver=lambda *_args, **_kwargs: _resolver("93.184.216.34"),
            connection_factory=lambda *_args: _FakeConnection(
                "93.184.216.34", response
            ),
        )
    serialized = str(caught.value) + repr(caught.value)
    assert "PRIVATE" not in serialized
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("status", [401, 429])
def test_auth_and_rate_limit_statuses_are_sanitized(status: int) -> None:
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER,
        "https",
        "openrouter.ai",
        443,
        ProbeEndpointPurpose.OPENROUTER_AUTH_KEY,
    )
    with pytest.raises(ProviderProbeError, match="^probe_http_status$"):
        run_http_probe(
            endpoint=endpoint,
            timeout_seconds=2,
            authorization="Bearer private",
            resolver=lambda *_args, **_kwargs: _resolver("93.184.216.34"),
            connection_factory=lambda *_args: _FakeConnection(
                "93.184.216.34", _FakeResponse(status=status)
            ),
        )


def test_connection_failure_is_sanitized_as_unavailable() -> None:
    class UnavailableConnection(_FakeConnection):
        def connect(self) -> None:
            raise OSError("PRIVATE endpoint diagnostic")

    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER,
        "https",
        "openrouter.ai",
        443,
        ProbeEndpointPurpose.OPENROUTER_MODELS,
    )
    with pytest.raises(ProviderProbeError, match="^probe_unavailable$") as caught:
        run_http_probe(
            endpoint=endpoint,
            timeout_seconds=2,
            resolver=lambda *_args, **_kwargs: _resolver("93.184.216.34"),
            connection_factory=lambda *_args: UnavailableConnection(
                "93.184.216.34", _FakeResponse()
            ),
        )
    assert caught.value.__cause__ is None


def test_only_governed_chat_completions_purpose_reaches_inference() -> None:
    assert probe_runner_module._INFERENCE_PURPOSES == frozenset(
        {
            ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS,
            ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS,
        }
    )
    values = " ".join(
        value
        for item in ProbeEndpointPurpose
        if item not in probe_runner_module._INFERENCE_PURPOSES
        for value in (
            item.value,
            probe_runner_module._PURPOSE_POLICY[item][1],
            probe_runner_module._PURPOSE_POLICY[item][2],
        )
    )
    for forbidden in (
        "generate",
        "chat",
        "completion",
        "embedding",
        "responses",
        "tool",
    ):
        assert forbidden not in values


def test_zai_chat_completions_endpoint_constructs() -> None:
    from graphite.routing.probe_runner import (
        _BODY_PURPOSES,
        _INFERENCE_PURPOSES,
        _PURPOSE_POLICY,
        _ZAI_HOSTS,
        HttpProbeEndpoint,
        ProbeEndpointPurpose,
    )
    from graphite.routing.lifecycle import LifecycleProviderId

    assert _ZAI_HOSTS == frozenset({"api.z.ai"})
    prov, method, path = _PURPOSE_POLICY[ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS]
    assert prov is LifecycleProviderId.ZAI and method == "POST" and path == "/api/paas/v4/chat/completions"
    assert ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS in _BODY_PURPOSES
    assert ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS in _INFERENCE_PURPOSES
    ep = HttpProbeEndpoint(
        LifecycleProviderId.ZAI, "https", "api.z.ai", 443,
        ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS)
    assert ep.host == "api.z.ai"


def test_openrouter_endpoint_still_constructs() -> None:  # OpenRouter path untouched
    from graphite.routing.probe_runner import HttpProbeEndpoint, ProbeEndpointPurpose
    from graphite.routing.lifecycle import LifecycleProviderId

    HttpProbeEndpoint(LifecycleProviderId.OPENROUTER, "https", "openrouter.ai", 443,
                      ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS)
