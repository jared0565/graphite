from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from graphite.cli import main
from graphite.config import Config
from graphite.overlays import _atomic_write_secure
from graphite.overlays import (
    OverlayError,
    OverlayRequest,
    build_overlay,
    canonical_graph_fingerprint,
    evaluate_overlay_staleness,
    load_overlay_manifest,
    overlay_directory,
)


def _write_fixture(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def answer() -> int:\n    return 42\n",
        encoding="utf-8",
    )


def _build_fixture(root: Path) -> Path:
    _write_fixture(root)
    output = root / "graph-out"
    assert main(["--output-dir", str(output), "build", str(root)]) == 0
    return output


def _request(
    root: Path,
    output: Path,
    *,
    provider: str = "ollama",
    lifecycle: str = "a" * 64,
    model: str = "b" * 64,
    routing: str | None = None,
    created_at: int = 1_721_337_600,
) -> OverlayRequest:
    return OverlayRequest(
        repository_root=root.resolve(),
        output_dir=output,
        provider=provider,
        provider_lifecycle_identity_digest=lifecycle,
        model_identity_digest=model,
        routing_policy_digest=routing,
        created_at=created_at,
    )


def _success(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "enabled": True,
        "status": "ok",
        "summary": "- deterministic overlay\n- non-authoritative",
        "input_tokens": 12,
        "output_tokens": 5,
        "tokens": 17,
        "provider": "ignored-provider-label",
        "model": "ignored-model-label",
    }


def _canonical_digests(output: Path) -> dict[str, str]:
    names = (
        ".graphite_analysis.json",
        ".graphite_clusters.json",
        ".graphite_graph.json",
        ".graphite_manifest.json",
        ".graphite_validation.json",
        "GRAPH_REPORT.md",
        "graph.html",
        "graph.json",
    )
    return {
        name: hashlib.sha256((output / name).read_bytes()).hexdigest()
        for name in names
    }


def test_overlay_manifest_binds_canonical_and_provider_identity(tmp_path: Path) -> None:
    root = tmp_path / "project"
    output = _build_fixture(root)
    request = _request(root, output)
    cfg = Config(
        output_dir=output,
        llm_mode="local",
        llm_provider="ollama",
        llm_model="qwen2.5-coder:7b",
        llm_timeout_seconds=8,
        llm_max_input_chars=4_000,
        llm_max_output_tokens=128,
    )

    result = build_overlay(request, cfg, enrich=_success)

    directory = overlay_directory(request)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    payload = json.loads((directory / manifest["payload_file"]).read_text(encoding="utf-8"))
    assert result["outcome_category"] == "succeeded"
    assert manifest == {
        "schema_version": 1,
        "non_authoritative": True,
        "canonical_graph_fingerprint": canonical_graph_fingerprint(
            output / "graph.json", root=root
        ),
        "provider": "ollama",
        "provider_lifecycle_identity_digest": "a" * 64,
        "model_identity_digest": "b" * 64,
        "routing_policy_digest": None,
        "overlay_identity_digest": request.overlay_identity_digest,
        "limits": {
            "timeout_seconds": 8.0,
            "max_input_chars": 4_000,
            "max_output_tokens": 128,
        },
        "created_at": 1_721_337_600,
        "outcome_category": "succeeded",
        "payload_file": manifest["payload_file"],
        "payload_sha256": manifest["payload_sha256"],
    }
    assert payload == {
        "schema_version": 1,
        "non_authoritative": True,
        "summary": "- deterministic overlay\n- non-authoritative",
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
    }
    assert hashlib.sha256((directory / manifest["payload_file"]).read_bytes()).hexdigest() == manifest[
        "payload_sha256"
    ]
    if os.name != "nt":
        assert stat.S_IMODE((directory / "manifest.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((directory / manifest["payload_file"]).stat().st_mode) == 0o600


def test_openrouter_requires_routing_policy_digest(tmp_path: Path) -> None:
    root = tmp_path / "project"
    output = _build_fixture(root)

    with pytest.raises(ValueError, match="routing_policy_digest_invalid"):
        _request(root, output, provider="openrouter")

    request = _request(
        root,
        output,
        provider="openrouter",
        routing="c" * 64,
    )
    assert request.routing_policy_digest == "c" * 64


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("provider", "../ollama", "provider_invalid"),
        ("provider", "openai", "provider_invalid"),
        ("lifecycle", "../" + "a" * 61, "provider_identity_digest_invalid"),
        ("model", "B" * 64, "model_identity_digest_invalid"),
        ("routing", "c" * 63, "routing_policy_digest_invalid"),
    ],
)
def test_overlay_identity_rejects_unsafe_or_noncanonical_values(
    tmp_path: Path, field: str, value: str, code: str
) -> None:
    root = tmp_path / "project"
    output = _build_fixture(root)
    values: dict[str, object] = {
        "provider": "openrouter" if field == "routing" else "ollama",
        "lifecycle": "a" * 64,
        "model": "b" * 64,
        "routing": "c" * 64 if field == "routing" else None,
    }
    values[field] = value

    with pytest.raises(ValueError, match=code):
        _request(root, output, **values)  # type: ignore[arg-type]


def test_overlay_rejects_output_outside_repository(tmp_path: Path) -> None:
    root = tmp_path / "project"
    output = _build_fixture(root)
    request = _request(root, tmp_path / "outside")

    with pytest.raises(OverlayError, match="overlay_output_outside_root"):
        build_overlay(
            request,
            Config(
                output_dir=output,
                llm_mode="local",
                llm_provider="ollama",
                llm_model="fake/model",
            ),
            enrich=_success,
        )


def test_overlay_rejects_symlinked_storage_component(tmp_path: Path) -> None:
    root = tmp_path / "project"
    output = _build_fixture(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (output / "overlays").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(OverlayError, match="overlay_path_reparse"):
        build_overlay(
            _request(root, output),
            Config(
                output_dir=output,
                llm_mode="local",
                llm_provider="ollama",
                llm_model="fake/model",
            ),
            enrich=_success,
        )
    assert not list(outside.iterdir())


def test_existing_identity_collision_is_rejected_before_enrichment(tmp_path: Path) -> None:
    root = tmp_path / "project"
    output = _build_fixture(root)
    request = _request(root, output)
    directory = overlay_directory(request)
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "overlay_identity_digest": request.overlay_identity_digest,
                "provider": "ollama",
                "provider_lifecycle_identity_digest": "d" * 64,
                "model_identity_digest": "b" * 64,
                "routing_policy_digest": None,
            }
        ),
        encoding="utf-8",
    )
    called = False

    def forbidden(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return _success()

    with pytest.raises(OverlayError, match="overlay_identity_collision"):
        build_overlay(
            request,
                Config(
                    output_dir=output,
                    llm_mode="local",
                    llm_provider="ollama",
                    llm_model="fake/model",
                ),
            enrich=forbidden,
        )
    assert called is False


def test_existing_payload_digest_collision_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    output = _build_fixture(root)
    request = _request(root, output)
    cfg = Config(
        output_dir=output,
        llm_mode="local",
        llm_provider="ollama",
        llm_model="fake/model",
    )
    build_overlay(request, cfg, enrich=_success)
    directory = overlay_directory(request)
    manifest_before = (directory / "manifest.json").read_bytes()
    payload_name = json.loads(manifest_before)["payload_file"]
    (directory / payload_name).write_text("collision", encoding="utf-8")

    with pytest.raises(OverlayError, match="overlay_payload_collision"):
        build_overlay(request, cfg, enrich=_success)

    assert (directory / "manifest.json").read_bytes() == manifest_before


def test_opt_in_manifest_read_rejects_corrupt_or_missing_payload(tmp_path: Path) -> None:
    root = tmp_path / "project"
    output = _build_fixture(root)
    request = _request(root, output)
    cfg = Config(
        output_dir=output,
        llm_mode="local",
        llm_provider="ollama",
        llm_model="fake/model",
    )
    build_overlay(request, cfg, enrich=_success)
    manifest = load_overlay_manifest(request)
    payload_path = overlay_directory(request) / manifest["payload_file"]
    payload_path.unlink()

    with pytest.raises(OverlayError, match="overlay_missing"):
        load_overlay_manifest(request)


def test_success_is_not_published_if_canonical_bundle_changes_during_call(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    output = _build_fixture(root)
    request = _request(root, output)
    cfg = Config(
        output_dir=output,
        llm_mode="local",
        llm_provider="ollama",
        llm_model="fake/model",
    )

    def change_bundle(*_args: object, **_kwargs: object) -> dict[str, object]:
        graph_path = output / "graph.json"
        bundle = json.loads(graph_path.read_text(encoding="utf-8"))
        bundle["analysis"]["concurrent_marker"] = True
        graph_path.write_text(json.dumps(bundle), encoding="utf-8")
        return _success()

    with pytest.raises(OverlayError, match="canonical_graph_changed"):
        build_overlay(request, cfg, enrich=change_bundle)

    assert not (overlay_directory(request) / "manifest.json").exists()


def test_failure_marker_symlink_is_rejected_without_following_it(tmp_path: Path) -> None:
    root = tmp_path / "project"
    output = _build_fixture(root)
    request = _request(root, output)
    directory = overlay_directory(request)
    directory.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    try:
        (directory / "failure.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(OverlayError, match="overlay_path_reparse"):
        build_overlay(
            request,
            Config(
                output_dir=output,
                llm_mode="local",
                llm_provider="ollama",
                llm_model="fake/model",
            ),
            enrich=lambda *_args, **_kwargs: {
                "status": "error",
                "error_category": "connection",
            },
        )

    assert outside.read_text(encoding="utf-8") == "outside"


def test_failed_enrichment_preserves_last_valid_overlay_and_sanitizes_marker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    output = _build_fixture(root)
    request = _request(root, output)
    cfg = Config(
        output_dir=output,
        llm_mode="local",
        llm_provider="ollama",
        llm_model="fake/model",
    )
    build_overlay(request, cfg, enrich=_success)
    directory = overlay_directory(request)
    manifest_before = (directory / "manifest.json").read_bytes()
    payload_name = json.loads(manifest_before)["payload_file"]
    payload_before = (directory / payload_name).read_bytes()

    failed = build_overlay(
        request,
        cfg,
        enrich=lambda *_args, **_kwargs: {
            "enabled": True,
            "status": "error",
            "error_category": "authentication",
            "error": "RAW provider diagnostic with private-secret",
            "tokens": 0,
        },
    )

    assert failed["outcome_category"] == "failed"
    assert (directory / "manifest.json").read_bytes() == manifest_before
    assert (directory / payload_name).read_bytes() == payload_before
    marker = json.loads((directory / "failure.json").read_text(encoding="utf-8"))
    assert marker["failure_category"] == "authentication"
    assert set(marker) == {
        "schema_version",
        "non_authoritative",
        "canonical_graph_fingerprint",
        "provider",
        "provider_lifecycle_identity_digest",
        "model_identity_digest",
        "routing_policy_digest",
        "overlay_identity_digest",
        "created_at",
        "outcome_category",
        "failure_category",
    }
    assert "RAW" not in json.dumps(marker)
    assert "private-secret" not in json.dumps(marker)


def test_overlay_staleness_is_independent_and_reasoned(tmp_path: Path) -> None:
    root = tmp_path / "project"
    output = _build_fixture(root)
    request = _request(root, output)
    cfg = Config(
        output_dir=output,
        llm_mode="local",
        llm_provider="ollama",
        llm_model="fake/model",
    )
    build_overlay(request, cfg, enrich=_success)
    manifest = json.loads(
        (overlay_directory(request) / "manifest.json").read_text(encoding="utf-8")
    )

    current = evaluate_overlay_staleness(
        manifest,
        canonical_graph_fingerprint=manifest["canonical_graph_fingerprint"],
        provider_lifecycle_identity_digest="a" * 64,
        model_identity_digest="b" * 64,
        routing_policy_digest=None,
    )
    stale = evaluate_overlay_staleness(
        manifest,
        canonical_graph_fingerprint="f" * 64,
        provider_lifecycle_identity_digest="d" * 64,
        model_identity_digest="e" * 64,
        routing_policy_digest=None,
    )

    assert current == {"status": "current", "stale_reasons": []}
    assert stale == {
        "status": "stale",
        "stale_reasons": [
            "canonical_graph_changed",
            "model_identity_changed",
            "provider_identity_changed",
        ],
    }
    assert main(["--output-dir", str(output), "check", str(root), "--json"]) == 0


@pytest.mark.parametrize(
    ("provider", "mode", "routing", "failure_category"),
    [
        ("ollama", "local", None, "connection"),
        ("openrouter", "cloud", "c" * 64, "authentication"),
    ],
)
def test_provider_failure_deletion_and_drift_cannot_change_canonical_artifacts(
    tmp_path: Path,
    provider: str,
    mode: str,
    routing: str | None,
    failure_category: str,
) -> None:
    root = tmp_path / provider
    output = _build_fixture(root)
    before = _canonical_digests(output)
    request = _request(root, output, provider=provider, routing=routing)
    cfg = Config(
        output_dir=output,
        llm_mode=mode,
        llm_provider=provider,
        llm_model="fake/model",
    )

    result = build_overlay(
        request,
        cfg,
        enrich=lambda *_args, **_kwargs: {
            "enabled": True,
            "status": "error",
            "error_category": failure_category,
            "tokens": 0,
        },
    )
    assert result["outcome_category"] == "failed"
    assert _canonical_digests(output) == before
    overlay = overlay_directory(request)
    (overlay / "failure.json").unlink()
    overlay.rmdir()
    assert _canonical_digests(output) == before
    assert main(["--output-dir", str(output), "check", str(root), "--json"]) == 0
    assert _canonical_digests(output) == before


def test_overlay_cli_requires_fresh_graph_and_writes_only_overlay_tree(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "project"
    output = _build_fixture(root)
    before = _canonical_digests(output)
    capsys.readouterr()
    monkeypatch.setattr("graphite.overlays.enrich_report", _success)
    monkeypatch.setattr("graphite.cli.time.time", lambda: 1_721_337_600)

    result = main(
        [
            "--output-dir",
            str(output),
            "--llm",
            "local",
            "--llm-provider",
            "ollama",
            "--llm-model",
            "fake/model",
            "overlay",
            "build",
            str(root),
            "--provider-identity-digest",
            "a" * 64,
            "--model-identity-digest",
            "b" * 64,
            "--json",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome_category"] == "succeeded"
    assert _canonical_digests(output) == before
    _write_fixture(root / "new-source")
    assert main(
        [
            "--output-dir",
            str(output),
            "--llm",
            "local",
            "--llm-provider",
            "ollama",
            "--llm-model",
            "fake/model",
            "overlay",
            "build",
            str(root),
            "--provider-identity-digest",
            "a" * 64,
            "--model-identity-digest",
            "b" * 64,
            "--json",
        ]
    ) == 3
    error = json.loads(capsys.readouterr().out)
    assert error == {"error": "canonical_graph_stale"}
    assert _canonical_digests(output) == before


def test_overlay_cli_rejects_credential_in_argv_before_enrichment(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "project"
    output = _build_fixture(root)
    capsys.readouterr()

    def forbidden(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("credential-bearing command reached enrichment")

    monkeypatch.setattr("graphite.overlays.enrich_report", forbidden)
    result = main(
        [
            "--output-dir",
            str(output),
            "--llm",
            "cloud",
            "--llm-provider",
            "openrouter",
            "--llm-model",
            "fake/model",
            "--llm-api-key",
            "must-not-be-used",
            "overlay",
            "build",
            str(root),
            "--provider-identity-digest",
            "a" * 64,
            "--model-identity-digest",
            "b" * 64,
            "--routing-policy-digest",
            "c" * 64,
            "--json",
        ]
    )

    assert result == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "overlay_credential_argv_forbidden"
    }
    assert not (output / "overlays").exists()


def test_overlay_write_outlasts_a_reader_holding_the_file_open(
    tmp_path: Path,
    replace_retry_clock: object,
    replace_denied: object,
) -> None:
    # Same Windows rename race as the graph writer (#59): a reader's handle
    # made the single os.replace raise, surfacing as overlay_write_failed.
    target = tmp_path / "manifest.json"
    _atomic_write_secure(target, {"n": 1})
    attempts = replace_denied(2)  # type: ignore[operator]

    _atomic_write_secure(target, {"n": 2})

    assert attempts == [1, 2, 3]
    assert json.loads(target.read_text(encoding="utf-8")) == {"n": 2}
    assert list(tmp_path.glob(".manifest.json.*.tmp")) == []
