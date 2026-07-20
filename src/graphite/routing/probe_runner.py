"""Bounded non-inference process and HTTP probe boundary."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import queue
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from graphite.probe_process import run_bounded_process

from .contracts import ProviderId
from .lifecycle import LifecycleProviderId
from .process_runner import (
    CliProcessFailureDiagnostics,
    CliProcessError,
    CliProcessResult,
    ProcessRunner,
    run_cli_process,
)

MAX_PROBE_RESPONSE_BYTES: Final = 64 * 1024
MAX_PROBE_REQUEST_BYTES: Final = 64 * 1024
MAX_INFERENCE_REQUEST_BYTES: Final = 1_048_576
MAX_INFERENCE_RESPONSE_BYTES: Final = 4_194_304
MAX_INFERENCE_TIMEOUT_SECONDS: Final = 600.0
MAX_PROBE_HEADERS: Final = 64
MAX_PROBE_HEADER_BYTES: Final = 16 * 1024
MAX_DNS_WORKERS: Final = 4
_OPENROUTER_HOSTS: Final = frozenset({"openrouter.ai"})
_DNS_SLOTS = threading.BoundedSemaphore(MAX_DNS_WORKERS)


class ProviderProbeError(RuntimeError):
    """Stable probe failure containing no endpoint, credential, or response data."""

    def __init__(
        self,
        code: str,
        *,
        diagnostics: CliProcessFailureDiagnostics | None = None,
    ) -> None:
        self.code = code
        self.diagnostics = (
            diagnostics
            if isinstance(diagnostics, CliProcessFailureDiagnostics)
            else None
        )
        super().__init__(code)


class ProbeEndpointPurpose(StrEnum):
    OLLAMA_VERSION = "ollama_version"
    OLLAMA_TAGS = "ollama_tags"
    OLLAMA_SHOW = "ollama_show"
    OPENROUTER_MODELS = "openrouter_models"
    OPENROUTER_AUTH_KEY = "openrouter_auth_key"
    OPENROUTER_CHAT_COMPLETIONS = "openrouter_chat_completions"


_PURPOSE_POLICY: Final = {
    ProbeEndpointPurpose.OLLAMA_VERSION: (
        LifecycleProviderId.OLLAMA,
        "GET",
        "/api/version",
    ),
    ProbeEndpointPurpose.OLLAMA_TAGS: (
        LifecycleProviderId.OLLAMA,
        "GET",
        "/api/tags",
    ),
    ProbeEndpointPurpose.OLLAMA_SHOW: (
        LifecycleProviderId.OLLAMA,
        "POST",
        "/api/show",
    ),
    ProbeEndpointPurpose.OPENROUTER_MODELS: (
        LifecycleProviderId.OPENROUTER,
        "GET",
        "/api/v1/models",
    ),
    ProbeEndpointPurpose.OPENROUTER_AUTH_KEY: (
        LifecycleProviderId.OPENROUTER,
        "GET",
        "/api/v1/auth/key",
    ),
    ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS: (
        LifecycleProviderId.OPENROUTER,
        "POST",
        "/api/v1/chat/completions",
    ),
}
_BODY_PURPOSES: Final = frozenset(
    {ProbeEndpointPurpose.OLLAMA_SHOW, ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS}
)
_INFERENCE_PURPOSES: Final = frozenset({ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS})


@dataclass(frozen=True, slots=True)
class HttpProbeEndpoint:
    provider: LifecycleProviderId
    scheme: str
    host: str
    port: int
    purpose: ProbeEndpointPurpose
    allowed_ollama_ports: frozenset[int] = frozenset({11434})

    def __post_init__(self) -> None:
        try:
            provider = LifecycleProviderId(self.provider)
            purpose = ProbeEndpointPurpose(self.purpose)
        except (TypeError, ValueError):
            raise ProviderProbeError("probe_endpoint_invalid") from None
        if (
            not isinstance(self.host, str)
            or not self.host
            or self.host != self.host.casefold()
            or isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
            or _PURPOSE_POLICY[purpose][0] is not provider
        ):
            raise ProviderProbeError("probe_endpoint_invalid")
        if provider is LifecycleProviderId.OLLAMA:
            try:
                address = ipaddress.ip_address(self.host)
            except ValueError:
                raise ProviderProbeError("probe_endpoint_invalid") from None
            if (
                self.scheme != "http"
                or not address.is_loopback
                or not self.allowed_ollama_ports
                or self.port not in self.allowed_ollama_ports
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 1 <= value <= 65535
                    for value in self.allowed_ollama_ports
                )
            ):
                raise ProviderProbeError("probe_endpoint_invalid")
        elif (
            provider is not LifecycleProviderId.OPENROUTER
            or self.scheme != "https"
            or self.host not in _OPENROUTER_HOSTS
            or self.port != 443
            or self.allowed_ollama_ports != frozenset({11434})
        ):
            raise ProviderProbeError("probe_endpoint_invalid")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "purpose", purpose)


@dataclass(frozen=True, slots=True)
class HttpProbeResult:
    status_code: int
    body: bytes = field(repr=False)
    body_sha256: str
    duration_seconds: float


class _Response(Protocol):
    status: int
    headers: object

    def read(self, amount: int) -> bytes: ...


class _Connection(Protocol):
    sock: object

    def connect(self) -> None: ...
    def request(self, method: str, path: str, **kwargs: object) -> None: ...
    def getresponse(self) -> _Response: ...
    def close(self) -> None: ...


Resolver = Callable[..., Sequence[tuple[object, ...]]]
ConnectionFactory = Callable[[HttpProbeEndpoint, str, float], _Connection]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        raw = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _default_connection(
    endpoint: HttpProbeEndpoint,
    address: str,
    timeout: float,
) -> _Connection:
    if endpoint.scheme == "https":
        return _PinnedHTTPSConnection(endpoint.host, endpoint.port, address, timeout)
    return _PinnedHTTPConnection(endpoint.host, endpoint.port, address, timeout)


def _remaining(deadline: float, clock: Callable[[], float]) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise ProviderProbeError("probe_timeout")
    return remaining


def _resolve(
    endpoint: HttpProbeEndpoint,
    *,
    deadline: float,
    resolver: Resolver,
    clock: Callable[[], float],
) -> tuple[str, ...]:
    if not _DNS_SLOTS.acquire(blocking=False):
        raise ProviderProbeError("probe_dns_busy")
    results: queue.Queue[object] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            value: object = resolver(
                endpoint.host,
                endpoint.port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except Exception:
            value = ProviderProbeError("probe_unavailable")
        try:
            results.put_nowait(value)
        except queue.Full:
            pass
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=worker, daemon=True).start()
    try:
        resolved = results.get(timeout=_remaining(deadline, clock))
    except queue.Empty:
        raise ProviderProbeError("probe_timeout") from None
    if isinstance(resolved, ProviderProbeError):
        raise resolved
    if not isinstance(resolved, (tuple, list)) or not resolved:
        raise ProviderProbeError("probe_address_invalid")
    addresses: list[str] = []
    for record in resolved:
        if not isinstance(record, tuple) or len(record) < 5:
            raise ProviderProbeError("probe_address_invalid")
        socket_address = record[4]
        if not isinstance(socket_address, tuple) or not socket_address:
            raise ProviderProbeError("probe_address_invalid")
        raw_address = socket_address[0]
        try:
            address = ipaddress.ip_address(raw_address)
        except (TypeError, ValueError):
            raise ProviderProbeError("probe_address_invalid") from None
        if endpoint.provider is LifecycleProviderId.OLLAMA:
            permitted = address.is_loopback
        else:
            permitted = address.is_global
        if not permitted:
            raise ProviderProbeError("probe_address_invalid")
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
    return tuple(addresses)


def _peer_address(connection: _Connection) -> str:
    try:
        peer = connection.sock.getpeername()
        address = ipaddress.ip_address(peer[0])
    except (AttributeError, OSError, TypeError, ValueError, IndexError):
        raise ProviderProbeError("probe_peer_invalid") from None
    return str(address)


def _read_response(
    response: _Response,
    maximum: int,
    *,
    transport_socket: object,
    deadline: float,
    clock: Callable[[], float],
) -> bytes:
    try:
        headers = list(response.headers.items())
    except (AttributeError, TypeError, ValueError):
        raise ProviderProbeError("probe_headers_invalid") from None
    if len(headers) > MAX_PROBE_HEADERS:
        raise ProviderProbeError("probe_response_limit")
    header_size = sum(len(str(name)) + len(str(value)) + 4 for name, value in headers)
    if header_size > MAX_PROBE_HEADER_BYTES:
        raise ProviderProbeError("probe_response_limit")
    content_type = response.headers.get("Content-Type")
    if (
        not isinstance(content_type, str)
        or content_type.split(";", 1)[0].strip().casefold() != "application/json"
    ):
        raise ProviderProbeError("probe_content_type_invalid")
    declared_value = response.headers.get("Content-Length")
    declared: int | None = None
    if declared_value is not None:
        try:
            declared = int(declared_value, 10)
        except (TypeError, ValueError):
            raise ProviderProbeError("probe_headers_invalid") from None
        if declared < 0 or declared > maximum:
            raise ProviderProbeError("probe_response_limit")
    chunks: list[bytes] = []
    size = 0
    while True:
        transport_socket.settimeout(_remaining(deadline, clock))
        chunk = response.read(min(8192, maximum + 1 - size))
        if not isinstance(chunk, bytes):
            raise ProviderProbeError("probe_protocol_invalid")
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > maximum:
            raise ProviderProbeError("probe_response_limit")
        if declared is not None and size == declared:
            break
    if declared is not None and declared != size:
        raise ProviderProbeError("probe_protocol_invalid")
    body = b"".join(chunks)
    try:
        json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ProviderProbeError("probe_protocol_invalid") from None
    return body


def run_process_probe(
    *,
    argv: tuple[str, ...],
    cwd: Path,
    provider: LifecycleProviderId | str,
    credential_home: Path | None,
    timeout_seconds: float,
    runner: ProcessRunner = run_bounded_process,
    source_environment: Mapping[str, str] | None = None,
    max_output_bytes: int = MAX_PROBE_RESPONSE_BYTES,
) -> CliProcessResult:
    """Run one fixed no-input CLI identity probe through the hardened transport."""
    try:
        lifecycle_provider = LifecycleProviderId(provider)
        cli_provider = ProviderId(lifecycle_provider.value)
    except (TypeError, ValueError):
        raise ProviderProbeError("probe_provider_invalid") from None
    try:
        return run_cli_process(
            argv=argv,
            cwd=cwd,
            stdin=b"",
            provider=cli_provider,
            credential_home=credential_home,
            timeout_seconds=timeout_seconds,
            max_input_bytes=1,
            max_output_bytes=max_output_bytes,
            runner=runner,
            source_environment=source_environment,
        )
    except CliProcessError as exc:
        raise ProviderProbeError(exc.code, diagnostics=exc.diagnostics) from None


def run_http_probe(
    *,
    endpoint: HttpProbeEndpoint,
    timeout_seconds: float,
    request_body: bytes | None = None,
    authorization: str | None = None,
    max_response_bytes: int = MAX_PROBE_RESPONSE_BYTES,
    resolver: Resolver = socket.getaddrinfo,
    connection_factory: ConnectionFactory = _default_connection,
    clock: Callable[[], float] = time.monotonic,
) -> HttpProbeResult:
    """Perform one pinned, redirect-free metadata request under one deadline."""
    if not isinstance(endpoint, HttpProbeEndpoint):
        raise ProviderProbeError("probe_endpoint_invalid")
    inference = endpoint.purpose in _INFERENCE_PURPOSES
    timeout_ceiling = MAX_INFERENCE_TIMEOUT_SECONDS if inference else 30.0
    request_ceiling = MAX_INFERENCE_REQUEST_BYTES if inference else MAX_PROBE_REQUEST_BYTES
    response_ceiling = MAX_INFERENCE_RESPONSE_BYTES if inference else MAX_PROBE_RESPONSE_BYTES
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0.1 <= timeout_seconds <= timeout_ceiling
        or isinstance(max_response_bytes, bool)
        or not isinstance(max_response_bytes, int)
        or not 1 <= max_response_bytes <= response_ceiling
        or request_body is not None
        and (
            not isinstance(request_body, bytes)
            or len(request_body) > request_ceiling
        )
        or authorization is not None
        and (
            not isinstance(authorization, str)
            or not authorization
            or "\x00" in authorization
            or "\r" in authorization
            or "\n" in authorization
            or len(authorization) > 4096
        )
    ):
        raise ProviderProbeError("probe_request_invalid")
    provider, method, path = _PURPOSE_POLICY[endpoint.purpose]
    if provider is not endpoint.provider:
        raise ProviderProbeError("probe_endpoint_invalid")
    if (request_body is not None) is (endpoint.purpose not in _BODY_PURPOSES):
        raise ProviderProbeError("probe_request_invalid")
    if endpoint.provider is LifecycleProviderId.OLLAMA and authorization is not None:
        raise ProviderProbeError("probe_request_invalid")
    started = clock()
    deadline = started + float(timeout_seconds)
    addresses = _resolve(endpoint, deadline=deadline, resolver=resolver, clock=clock)
    connection: _Connection | None = None
    try:
        connection = connection_factory(endpoint, addresses[0], _remaining(deadline, clock))
        connection.connect()
        if _peer_address(connection) not in addresses:
            raise ProviderProbeError("probe_peer_invalid")
        transport_socket = connection.sock
        transport_socket.settimeout(_remaining(deadline, clock))
        headers = {"Accept": "application/json", "Connection": "close"}
        if authorization is not None:
            headers["Authorization"] = authorization
        if request_body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(request_body))
        connection.request(method, path, body=request_body, headers=headers)
        transport_socket.settimeout(_remaining(deadline, clock))
        response = connection.getresponse()
        if 300 <= response.status <= 399:
            raise ProviderProbeError("probe_redirect_rejected")
        if not 200 <= response.status <= 299:
            raise ProviderProbeError("probe_http_status")
        body = _read_response(
            response,
            max_response_bytes,
            transport_socket=transport_socket,
            deadline=deadline,
            clock=clock,
        )
        duration = clock() - started
        if duration < 0 or duration > timeout_seconds:
            raise ProviderProbeError("probe_timeout")
        return HttpProbeResult(
            response.status,
            body,
            hashlib.sha256(body).hexdigest(),
            duration,
        )
    except ProviderProbeError:
        raise
    except (TimeoutError, socket.timeout):
        raise ProviderProbeError("probe_timeout") from None
    except (OSError, ssl.SSLError, http.client.HTTPException):
        raise ProviderProbeError("probe_unavailable") from None
    except Exception:
        raise ProviderProbeError("probe_failed") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
