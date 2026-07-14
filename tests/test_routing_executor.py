"""Contract tests for the single-shot loopback Ollama executor."""
from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from graphite.routing.approval import ApprovalAuthority
from graphite.routing.context_builder import ContextBundle, OutboundManifest, PrivateContextItem
from graphite.routing.contracts import ApprovalManifest, Effort, ExecutionOutcome
from graphite.routing.ollama_executor import ExecutorError, execute_ollama
from graphite.routing.settings import RoutingSettings
from graphite.routing.storage import RepositoryStore


MODEL_DIGESTS = {
    "kimi-k2.7-code:cloud": "a" * 64,
    "minimax-m2.7:cloud": "b" * 64,
    "nemotron-3-super:cloud": "c" * 64,
    "minimax-m3:cloud": "d" * 64,
}
MODEL = "kimi-k2.7-code:cloud"
DIGEST = MODEL_DIGESTS[MODEL]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.server.requests.append(("GET", self.path, dict(self.headers), b""))
        self._reply(self.server.inventory_status, self.server.inventory)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(("POST", self.path, dict(self.headers), body))
        self._reply(self.server.chat_status, self.server.chat)

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


@contextmanager
def _server(
    *,
    model: str = MODEL,
    digest: str = DIGEST,
    chat_status: int = 200,
    chat: bytes | None = None,
):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    server.inventory_status = 200
    server.inventory = json.dumps({"models": [{
        "name": model,
        "model": model,
        "digest": digest,
        "details": {"context_length": 262144},
        "capabilities": ["code"],
    }]}).encode()
    server.chat_status = chat_status
    server.chat = chat or json.dumps({
        "model": model,
        "message": {"role": "assistant", "content": "suggested change"},
        "done": True,
        "prompt_eval_count": 123,
        "eval_count": 17,
    }).encode()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _approved(
    tmp_path: Path, model: str = MODEL
) -> tuple[ApprovalAuthority, object, ApprovalManifest, ContextBundle]:
    root = tmp_path / "repo"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    authority = ApprovalAuthority(
        store,
        key_path=tmp_path / "machine" / "approval.key",
        quota_path=tmp_path / "machine" / "quota.sqlite3",
        now=lambda: 150,
    )
    manifest = OutboundManifest("1", (), 0, 0, "b" * 64)
    context = ContextBundle(manifest, (PrivateContextItem("src/app.py", "print('safe')"),))
    approval = ApprovalManifest(
        approval_id="approval-1",
        task_id="task-1",
        decision_id="decision-1",
        graph_fingerprint="a" * 64,
        context_manifest_hash=manifest.manifest_hash,
        model_id=model,
        effort=Effort.DEFAULT,
        max_input_tokens=8000,
        max_output_tokens=2000,
        policy_version="1",
        issued_at=100,
        expires_at=200,
        nonce="nonce-1",
    )
    return authority, authority.issue(approval), approval, context


@pytest.mark.parametrize("model", tuple(MODEL_DIGESTS))
def test_executor_revalidates_then_posts_fixed_bounded_shape(
    tmp_path: Path, model: str
) -> None:
    digest = MODEL_DIGESTS[model]
    authority, signed, approval, context = _approved(tmp_path, model)
    with _server(model=model, digest=digest) as server:
        result = execute_ollama(
            authority=authority,
            signed_approval=signed,
            current_manifest=approval,
            context=context,
            objective="Review this implementation",
            expected_digest=digest,
            settings=RoutingSettings(),
            host="127.0.0.1",
            port=server.server_port,
            allowed_ports=frozenset({server.server_port}),
        )

    assert result.text == "suggested change"
    assert result.receipt.outcome is ExecutionOutcome.SUCCEEDED
    assert result.receipt.input_tokens == 123
    assert result.receipt.output_tokens == 17
    assert result.receipt.response_hash == hashlib.sha256(b"suggested change").hexdigest()
    assert [(method, path) for method, path, _, _ in server.requests] == [
        ("GET", "/api/tags"), ("POST", "/api/chat")
    ]
    headers = server.requests[1][2]
    payload = json.loads(server.requests[1][3])
    assert "Authorization" not in headers
    assert result.receipt.model_id == model
    assert payload["model"] == model
    assert payload["stream"] is False
    assert payload["options"] == {"num_predict": 2000}
    assert set(payload) == {"model", "messages", "options", "stream"}
    assert "no execution authority" in payload["messages"][0]["content"]
    prompt = payload["messages"][1]["content"]
    assert "src/app.py" in prompt


@pytest.mark.parametrize("model", ("glm-5:cloud", "kimi-k2.6:cloud"))
def test_removed_profile_fails_before_chat_without_consuming_approval(
    tmp_path: Path, model: str
) -> None:
    digest = "f" * 64
    authority, signed, approval, context = _approved(tmp_path, model)
    with _server(model=model, digest=digest) as server:
        with pytest.raises(ExecutorError, match="model_profile_missing"):
            execute_ollama(
                authority=authority,
                signed_approval=signed,
                current_manifest=approval,
                context=context,
                objective="review",
                expected_digest=digest,
                settings=RoutingSettings(),
                host="127.0.0.1",
                port=server.server_port,
                allowed_ports=frozenset({server.server_port}),
            )
    assert [(method, path) for method, path, _, _ in server.requests] == [("GET", "/api/tags")]
    assert authority.store.approval_status("approval-1") == "issued"


@pytest.mark.parametrize(
    ("host", "port", "allowed"),
    [
        ("localhost", 11434, frozenset({11434})),
        ("example.com", 11434, frozenset({11434})),
        ("127.0.0.2", 11434, frozenset({11434})),
        ("http://127.0.0.1", 11434, frozenset({11434})),
        ("127.0.0.1", 9999, frozenset({11434})),
    ],
)
def test_executor_rejects_noncanonical_endpoint_before_consuming(
    tmp_path: Path, host: str, port: int, allowed: frozenset[int]
) -> None:
    authority, signed, approval, context = _approved(tmp_path)
    with pytest.raises(ExecutorError, match="executor_endpoint_invalid"):
        execute_ollama(
            authority=authority, signed_approval=signed, current_manifest=approval,
            context=context, objective="review", expected_digest=DIGEST,
            settings=RoutingSettings(), host=host, port=port, allowed_ports=allowed,
        )
    assert authority.store.approval_status("approval-1") == "issued"


def test_redirect_is_rejected_and_context_is_not_forwarded(tmp_path: Path) -> None:
    authority, signed, approval, context = _approved(tmp_path)
    with _server(chat_status=307, chat=b'{"secret":"print safe"}') as server:
        with pytest.raises(ExecutorError, match="provider_protocol") as caught:
            execute_ollama(
                authority=authority, signed_approval=signed, current_manifest=approval,
                context=context, objective="review", expected_digest=DIGEST,
                settings=RoutingSettings(), host="127.0.0.1", port=server.server_port,
                allowed_ports=frozenset({server.server_port}),
            )
    assert len(server.requests) == 2
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("chat", "code"),
    [
        (b"\xff", "provider_protocol"),
        (b"{", "provider_protocol"),
        (b"{}", "provider_protocol"),
        (json.dumps({"model": MODEL, "message": {"content": "x"}, "done": False}).encode(), "provider_protocol"),
        (json.dumps({"model": "kimi-k2.6:cloud", "message": {"content": "x"}, "done": True}).encode(), "provider_protocol"),
        (json.dumps({"model": MODEL, "message": {"content": "x"}, "done": True, "eval_count": 2001}).encode(), "response_limit"),
    ],
)
def test_invalid_responses_have_fixed_sanitized_errors(
    tmp_path: Path, chat: bytes, code: str
) -> None:
    authority, signed, approval, context = _approved(tmp_path)
    with _server(chat=chat) as server:
        with pytest.raises(ExecutorError) as caught:
            execute_ollama(
                authority=authority, signed_approval=signed, current_manifest=approval,
                context=context, objective="TOP SECRET", expected_digest=DIGEST,
                settings=RoutingSettings(), host="127.0.0.1", port=server.server_port,
                allowed_ports=frozenset({server.server_port}),
            )
    assert caught.value.code == code
    assert str(caught.value) == code
    assert "TOP SECRET" not in str(caught.value)


def test_changed_inventory_blocks_before_approval_consumption(tmp_path: Path) -> None:
    authority, signed, approval, context = _approved(tmp_path)
    with _server() as server:
        server.inventory = server.inventory.replace(DIGEST.encode(), ("e" * 64).encode())
        with pytest.raises(ExecutorError, match="model_identity_changed"):
            execute_ollama(
                authority=authority, signed_approval=signed, current_manifest=approval,
                context=context, objective="review", expected_digest=DIGEST,
                settings=RoutingSettings(), host="127.0.0.1", port=server.server_port,
                allowed_ports=frozenset({server.server_port}),
            )
    assert authority.store.approval_status("approval-1") == "issued"


def test_manifest_change_blocks_before_network(tmp_path: Path) -> None:
    authority, signed, approval, context = _approved(tmp_path)
    changed = replace(approval, context_manifest_hash="c" * 64)
    with pytest.raises(ExecutorError, match="approval_manifest_changed"):
        execute_ollama(
            authority=authority, signed_approval=signed, current_manifest=changed,
            context=context, objective="review", expected_digest=DIGEST,
            settings=RoutingSettings(), host="127.0.0.1", port=11434,
        )


def test_cancellation_blocks_before_network_and_consumption(tmp_path: Path) -> None:
    authority, signed, approval, context = _approved(tmp_path)

    with pytest.raises(ExecutorError, match="cancelled"):
        execute_ollama(
            authority=authority, signed_approval=signed, current_manifest=approval,
            context=context, objective="review", expected_digest=DIGEST,
            settings=RoutingSettings(), cancelled=lambda: True,
        )
    assert authority.store.approval_status("approval-1") == "issued"


def test_connection_failure_is_sanitized_and_not_retried(tmp_path: Path) -> None:
    authority, signed, approval, context = _approved(tmp_path)
    probe = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    unused_port = probe.server_port
    probe.server_close()

    with pytest.raises(ExecutorError) as caught:
        execute_ollama(
            authority=authority, signed_approval=signed, current_manifest=approval,
            context=context, objective="PRIVATE OBJECTIVE", expected_digest=DIGEST,
            settings=RoutingSettings(), host="127.0.0.1", port=unused_port,
            allowed_ports=frozenset({unused_port}),
        )
    assert caught.value.code == "provider_unavailable"
    assert str(caught.value) == "provider_unavailable"
    assert authority.store.approval_status("approval-1") == "issued"


def test_unknown_usage_is_charged_at_approved_maximum(tmp_path: Path) -> None:
    authority, signed, approval, context = _approved(tmp_path)
    chat = json.dumps({
        "model": MODEL,
        "message": {"role": "assistant", "content": "ok"},
        "done": True,
    }).encode()
    with _server(chat=chat) as server:
        result = execute_ollama(
            authority=authority, signed_approval=signed, current_manifest=approval,
            context=context, objective="review", expected_digest=DIGEST,
            settings=RoutingSettings(), host="127.0.0.1", port=server.server_port,
            allowed_ports=frozenset({server.server_port}),
        )
    assert result.receipt.input_tokens == approval.max_input_tokens
    assert result.receipt.output_tokens == approval.max_output_tokens


def test_request_body_limit_blocks_before_approval_consumption(tmp_path: Path) -> None:
    authority, signed, approval, context = _approved(tmp_path)
    context = replace(
        context,
        private_items=(PrivateContextItem("src/app.py", "x" * 100_000),),
    )
    tiny = replace(RoutingSettings(), max_context_bytes=16_384)
    with _server() as server:
        with pytest.raises(ExecutorError, match="request_limit"):
            execute_ollama(
                authority=authority, signed_approval=signed, current_manifest=approval,
                context=context, objective="review", expected_digest=DIGEST,
                settings=tiny, host="127.0.0.1", port=server.server_port,
                allowed_ports=frozenset({server.server_port}),
            )
    assert len(server.requests) == 1
    assert authority.store.approval_status("approval-1") == "issued"
