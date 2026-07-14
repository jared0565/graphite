"""Adversarial routing boundary tests shared by context and execution."""
from __future__ import annotations

import base64
import os
from pathlib import Path
from types import SimpleNamespace

import networkx as nx
import pytest

import graphite.routing.context_builder as context_module
from graphite.routing.context_builder import ContextError, build_routing_context
from graphite.routing.contracts import TaskRequest
from graphite.routing.settings import RoutingSettings


def _request(root: Path, target: str) -> TaskRequest:
    return TaskRequest(
        objective="Review a safe file",
        repository_root=root,
        targets=(target,),
        max_input_tokens=8_000,
        max_output_tokens=2_000,
        data_policy="source_allowed",
    )


def test_context_rejects_symlink_target(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-private.py"
    outside.write_text("SECRET = 'private'\n", encoding="utf-8")
    link = tmp_path / "src/link.py"
    link.parent.mkdir()
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    bundle = build_routing_context(
        _request(tmp_path, "src/link.py"),
        nx.DiGraph(),
        RoutingSettings(),
    )

    assert bundle.private_items == ()
    assert bundle.manifest.excluded_count == 1


def test_context_rejects_file_replacement_during_read(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "src/app.py"
    target.parent.mkdir()
    target.write_text("print('safe')\n", encoding="utf-8")
    real_fstat = os.fstat
    calls = 0

    def unstable(descriptor: int):
        nonlocal calls
        calls += 1
        result = real_fstat(descriptor)
        if calls < 2:
            return result
        return SimpleNamespace(
            st_dev=result.st_dev,
            st_ino=result.st_ino,
            st_size=result.st_size,
            st_mtime_ns=result.st_mtime_ns + 1,
        )

    monkeypatch.setattr(context_module.os, "fstat", unstable)

    with pytest.raises(ContextError, match="context_file_changed"):
        build_routing_context(
            _request(tmp_path, "src/app.py"),
            nx.DiGraph(),
            RoutingSettings(),
        )


@pytest.mark.parametrize(
    "content",
    [
        "OPENAI_API_KEY=sk-private-secret-value\n",
        "Authorization: Bearer private-token-value\n",
        "-----BEGIN OPENSSH PRIVATE KEY-----\n",
    ],
)
def test_context_secret_patterns_fail_closed(tmp_path: Path, content: str) -> None:
    target = tmp_path / "src/app.py"
    target.parent.mkdir()
    target.write_text(content, encoding="utf-8")

    bundle = build_routing_context(
        _request(tmp_path, "src/app.py"),
        nx.DiGraph(),
        RoutingSettings(),
    )

    assert bundle.private_items == ()
    assert bundle.manifest.excluded_count == 1


def test_context_base64_encoded_secret_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "src/app.py"
    target.parent.mkdir()
    encoded = base64.b64encode(b"api_key=private-secret-value").decode("ascii")
    target.write_text(f"value = '{encoded}'\n", encoding="utf-8")

    bundle = build_routing_context(
        _request(tmp_path, "src/app.py"),
        nx.DiGraph(),
        RoutingSettings(),
    )

    assert bundle.private_items == ()
    assert bundle.manifest.excluded_count == 1
