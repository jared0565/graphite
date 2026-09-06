"""Tests for `graphite.routing.context_builder`, the bounded context selector.

Named after the module so aramid's mutation stage 1 (`tests/test_<module>.py`)
has something to run; `-k context_builder` selected nothing before this file.
`test_routing_context.py` covers the assembled bundle; this file pins the
helpers underneath it -- path normalisation, the exclusion rules, the secret
screen, stable reads and the graph-neighbour reasons -- one rule per test.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import networkx as nx
import pytest

from graphite.routing.context_builder import (
    CONTEXT_SCHEMA_VERSION,
    ContextError,
    ManifestItem,
    OutboundManifest,
    _candidate_reasons,
    _excluded_by_name,
    _normalize_relative,
    _read_stable,
    _safe_text,
    build_routing_context,
)
from graphite.routing.contracts import TaskRequest
from graphite.routing.settings import RoutingSettings


def _request(root: Path, targets: tuple[str, ...] = ("src/app.py",), policy: str = "source_allowed") -> TaskRequest:
    return TaskRequest(
        objective="Pin the context helpers",
        repository_root=root,
        targets=targets,
        max_input_tokens=8_000,
        max_output_tokens=2_000,
        data_policy=policy,
    )


# --- _normalize_relative ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("src/app.py", "src/app.py"),
        ("src\\app.py", "src/app.py"),
        ("a/b/../c", None),
        ("./a", "a"),  # PurePosixPath drops "." and doubled separators before the check
        ("a//b", "a/b"),
        ("/abs/path", None),
        ("C:/abs", None),
        ("c:\\abs", None),
        ("", None),
        ("nul\x00", None),
        (b"bytes", None),
        (None, None),
    ],
)
def test_normalize_relative_accepts_only_clean_relative_posix_paths(value: object, expected: str | None) -> None:
    assert _normalize_relative(value) == expected


# --- _excluded_by_name ---------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "node_modules/pkg/index.js",
        "src/__pycache__/x.pyc",
        ".git/HEAD",
        "Graph-Out/graph.json",  # components compare case-folded
        "dist/bundle.js",
        ".env",
        ".env.local",
        "config/.npmrc",
        "keys/id_rsa",
        "certs/server.PEM",
        "store.jks",
        "vault.kdbx",
    ],
)
def test_excluded_by_name_refuses_generated_and_sensitive_paths(relative: str) -> None:
    assert _excluded_by_name(relative, frozenset()) is True


@pytest.mark.parametrize("relative", ["src/app.py", "docs/env.md", "environment/settings.py", "src/keys.py"])
def test_excluded_by_name_keeps_ordinary_source(relative: str) -> None:
    assert _excluded_by_name(relative, frozenset()) is False


def test_excluded_by_name_honours_configured_exclusions_as_paths_or_prefixes() -> None:
    exclusions = frozenset({"private", "docs/internal/"})
    assert _excluded_by_name("private", exclusions) is True
    assert _excluded_by_name("private/notes.md", exclusions) is True
    assert _excluded_by_name("docs/internal/plan.md", exclusions) is True
    assert _excluded_by_name("private_but_not_excluded.md", exclusions) is False
    assert _excluded_by_name("docs/internals.md", exclusions) is False


# --- _safe_text ----------------------------------------------------------------------------


def test_safe_text_decodes_utf8() -> None:
    assert _safe_text("def run():\n    return 'ok'\n".encode("utf-8")) == "def run():\n    return 'ok'\n"


@pytest.mark.parametrize(
    "data",
    [
        b"binary\x00payload",
        b"\xff\xfe not utf-8",
        b"api_key = sk-live-0123456789abcdef\n",
        b"Authorization: Bearer abcdefghijklmnop\n",
        b"-----BEGIN RSA PRIVATE KEY-----\n",
        # Assembled so the source holds no credential-shaped literal.
        b"aws " + b"AKIA" + b"ABCDEFGHIJKLMNOP" + b" here\n",
    ],
    ids=["nul", "not-utf8", "assignment", "bearer", "pem", "akia"],
)
def test_safe_text_refuses_binary_and_plain_secrets(data: bytes) -> None:
    assert _safe_text(data) is None


def test_safe_text_refuses_a_secret_hidden_in_base64() -> None:
    encoded = base64.b64encode(b"password = hunter2hunter2\n").decode("ascii")
    assert len(encoded) >= 24
    assert _safe_text(f"blob = {encoded}\n".encode("utf-8")) is None


def test_safe_text_keeps_base64_that_decodes_to_something_harmless() -> None:
    encoded = base64.b64encode(b"just an ordinary sentence here").decode("ascii")
    text = f"blob = {encoded}\n"
    assert _safe_text(text.encode("utf-8")) == text


# --- _read_stable ---------------------------------------------------------------------------


def test_read_stable_returns_the_bytes_of_a_regular_file_within_the_limit(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(b"x = 1\n")
    assert _read_stable(tmp_path, "src/a.py", 64) == b"x = 1\n"
    assert _read_stable(tmp_path, "src/a.py", 6) == b"x = 1\n"


def test_read_stable_refuses_a_file_over_the_limit(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_bytes(b"0123456789")
    assert _read_stable(tmp_path, "big.txt", 9) is None


def test_read_stable_refuses_missing_paths_directories_and_escapes(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_bytes(b"outside")
    try:
        assert _read_stable(tmp_path, "missing.txt", 64) is None
        assert _read_stable(tmp_path, "dir", 64) is None
        assert _read_stable(tmp_path, "nope/missing.txt", 64) is None
    finally:
        outside.unlink()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_read_stable_refuses_symlinked_files_and_symlinked_parents(tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_bytes(b"real")
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "inner.txt").write_bytes(b"inner")
    try:
        os.symlink(target, tmp_path / "link.txt")
        os.symlink(real_dir, tmp_path / "link_dir", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # unprivileged Windows
        pytest.skip(f"cannot create symlinks here: {exc}")
    assert _read_stable(tmp_path, "link.txt", 64) is None
    assert _read_stable(tmp_path, "link_dir/inner.txt", 64) is None
    assert _read_stable(tmp_path, "real_dir/inner.txt", 64) == b"inner"


def test_read_stable_raises_when_the_file_changes_under_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "a.py"
    path.write_bytes(b"x = 1\n")
    real_fstat = os.fstat
    calls = 0

    def moving_fstat(fd: int) -> object:
        nonlocal calls
        calls += 1
        result = real_fstat(fd)
        if calls == 2:  # the post-read check sees a different size
            return SimpleNamespace(
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_size=result.st_size + 1,
                st_mtime_ns=result.st_mtime_ns,
            )
        return result

    monkeypatch.setattr(os, "fstat", moving_fstat)
    with pytest.raises(ContextError, match="context_file_changed"):
        _read_stable(tmp_path, "a.py", 64)


# --- _candidate_reasons ---------------------------------------------------------------------


def test_candidate_reasons_names_targets_then_graph_neighbours(tmp_path: Path) -> None:
    graph = nx.DiGraph()
    graph.add_node("app", source_file="src/app.py")
    graph.add_node("service", source_file="src/service.py")
    graph.add_node("test", source_file="tests\\test_app.py")  # normalised on the way out
    graph.add_node("bad", source_file="../escape.py")  # dropped
    graph.add_edge("app", "service")
    graph.add_edge("test", "app")
    reasons = _candidate_reasons(_request(tmp_path), graph)
    assert reasons == {
        "src/app.py": "explicit_target",
        "src/service.py": "dependency",
        "tests/test_app.py": "dependent",
    }


def test_candidate_reasons_keeps_the_first_reason_for_a_path() -> None:
    graph = nx.DiGraph()
    graph.add_node("app", source_file="src/app.py")
    graph.add_node("both", source_file="src/both.py")
    graph.add_edge("app", "both")
    graph.add_edge("both", "app")
    reasons = _candidate_reasons(_request(Path.cwd()), graph)
    assert reasons["src/both.py"] == "dependency"


def test_candidate_reasons_survives_a_graph_that_is_not_a_graph() -> None:
    request = _request(Path.cwd(), targets=("a.py", "b.py"))
    assert _candidate_reasons(request, None) == {"a.py": "explicit_target", "b.py": "explicit_target"}
    assert _candidate_reasons(request, object()) == {"a.py": "explicit_target", "b.py": "explicit_target"}


# --- build_routing_context ------------------------------------------------------------------


def test_build_context_hashes_the_public_manifest_and_carries_source_only_when_allowed(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_bytes(b"x = 1\n")
    graph = nx.DiGraph()
    graph.add_node("app", source_file="src/app.py")

    bundle = build_routing_context(_request(tmp_path), graph, RoutingSettings())
    item = ManifestItem("src/app.py", 6, hashlib.sha256(b"x = 1\n").hexdigest(), "explicit_target")
    assert bundle.manifest.items == (item,)
    assert bundle.manifest.schema_version == CONTEXT_SCHEMA_VERSION
    assert bundle.manifest.total_bytes == 6
    assert bundle.manifest.excluded_count == 0
    assert len(bundle.manifest.manifest_hash) == 64
    assert [(p.path, p.content) for p in bundle.private_items] == [("src/app.py", "x = 1\n")]

    metadata_only = build_routing_context(_request(tmp_path, policy="metadata_only"), graph, RoutingSettings())
    assert metadata_only.private_items == ()
    assert metadata_only.manifest == bundle.manifest


def test_build_context_counts_every_kind_of_exclusion(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_bytes(b"x = 1\n")
    (tmp_path / "src" / "secret.py").write_bytes(b"api_key = sk-live-0123456789abcdef\n")
    (tmp_path / ".env").write_bytes(b"A=1\n")
    graph = nx.DiGraph()
    graph.add_node("app", source_file="src/app.py")
    graph.add_node("secret", source_file="src/secret.py")
    graph.add_node("env", source_file=".env")
    graph.add_node("gone", source_file="src/missing.py")
    graph.add_node("cfg", source_file="config/private.py")
    for neighbour in ("secret", "env", "gone", "cfg"):
        graph.add_edge("app", neighbour)

    bundle = build_routing_context(
        _request(tmp_path), graph, RoutingSettings(), exclusions=("config/", "../ignored")
    )
    assert [item.path for item in bundle.manifest.items] == ["src/app.py"]
    assert bundle.manifest.excluded_count == 4


def test_build_context_rejects_a_missing_root_and_out_of_range_limits(tmp_path: Path) -> None:
    with pytest.raises(ContextError, match="context_root_invalid"):
        build_routing_context(_request(tmp_path / "missing"), nx.DiGraph(), RoutingSettings())
    with pytest.raises(ContextError, match="context_file_limit_invalid"):
        build_routing_context(_request(tmp_path), nx.DiGraph(), RoutingSettings(max_context_files=0))
    with pytest.raises(ContextError, match="context_byte_limit_invalid"):
        build_routing_context(_request(tmp_path), nx.DiGraph(), RoutingSettings(max_context_bytes=1))


def test_manifest_to_dict_is_the_hashed_material_plus_the_hash() -> None:
    item = ManifestItem("a.py", 1, "f" * 64, "explicit_target", redaction_count=2)
    manifest = OutboundManifest("1", (item,), 1, 0, "h" * 64)
    assert manifest.to_dict() == {
        "schema_version": "1",
        "items": [
            {"path": "a.py", "byte_count": 1, "sha256": "f" * 64, "reason": "explicit_target", "redaction_count": 2}
        ],
        "total_bytes": 1,
        "excluded_count": 0,
        "manifest_hash": "h" * 64,
    }
