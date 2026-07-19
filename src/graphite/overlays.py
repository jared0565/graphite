"""Non-authoritative, identity-bound storage for optional graph enrichment."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final
from urllib.parse import urlsplit

from .config import Config
from .freshness import FreshnessLimitError, check_graph_freshness
from .graph_io import GraphReadError, load_validated_graph_bundle
from .llm import enrich_report

OVERLAY_SCHEMA_VERSION: Final = 1
MAX_OVERLAY_FILE_BYTES: Final = 1024 * 1024
MAX_SUMMARY_BYTES: Final = 64 * 1024
MAX_CANONICAL_MANIFEST_BYTES: Final = 16 * 1024 * 1024

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_PROVIDERS = frozenset({"ollama", "openrouter"})
_FAILURE_CATEGORIES = frozenset(
    {"authentication", "configuration", "connection", "provider_error", "timeout"}
)
_IDENTITY_FIELDS = (
    "overlay_identity_digest",
    "provider",
    "provider_lifecycle_identity_digest",
    "model_identity_digest",
    "routing_policy_digest",
)
_PAYLOAD_FILE = re.compile(r"^payload-([0-9a-f]{64})\.json$")


class OverlayError(RuntimeError):
    """A stable, path-free overlay failure."""


def _digest(value: object, code: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _HEX_64.fullmatch(value) is None:
        raise ValueError(code)
    return value


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata)


@dataclass(frozen=True)
class OverlayRequest:
    repository_root: Path
    output_dir: Path
    provider: str
    provider_lifecycle_identity_digest: str
    model_identity_digest: str
    routing_policy_digest: str | None
    created_at: int

    def __post_init__(self) -> None:
        if not isinstance(self.repository_root, Path) or not self.repository_root.is_absolute():
            raise ValueError("repository_root_invalid")
        if not isinstance(self.output_dir, Path):
            raise ValueError("output_dir_invalid")
        if self.provider not in _PROVIDERS:
            raise ValueError("provider_invalid")
        lifecycle = _digest(
            self.provider_lifecycle_identity_digest,
            "provider_identity_digest_invalid",
        )
        model = _digest(self.model_identity_digest, "model_identity_digest_invalid")
        routing = _digest(
            self.routing_policy_digest,
            "routing_policy_digest_invalid",
            optional=True,
        )
        if self.provider == "openrouter" and routing is None:
            raise ValueError("routing_policy_digest_invalid")
        if self.provider != "openrouter" and routing is not None:
            raise ValueError("routing_policy_digest_invalid")
        if (
            isinstance(self.created_at, bool)
            or not isinstance(self.created_at, int)
            or not 0 <= self.created_at <= 10**12
        ):
            raise ValueError("created_at_invalid")
        object.__setattr__(self, "provider_lifecycle_identity_digest", lifecycle)
        object.__setattr__(self, "model_identity_digest", model)
        object.__setattr__(self, "routing_policy_digest", routing)

    @property
    def overlay_identity_digest(self) -> str:
        return _canonical_digest(
            {
                "provider": self.provider,
                "provider_lifecycle_identity_digest": self.provider_lifecycle_identity_digest,
                "model_identity_digest": self.model_identity_digest,
                "routing_policy_digest": self.routing_policy_digest,
            }
        )


def _lexical_output_root(request: OverlayRequest) -> Path:
    root = request.repository_root
    output = request.output_dir if request.output_dir.is_absolute() else root / request.output_dir
    lexical = output.absolute()
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise OverlayError("overlay_output_outside_root") from exc
    if ".." in relative.parts:
        raise OverlayError("overlay_output_outside_root")
    return lexical


def overlay_directory(request: OverlayRequest) -> Path:
    """Return the identity-derived overlay directory without touching storage."""
    return (
        _lexical_output_root(request)
        / "overlays"
        / request.provider
        / request.overlay_identity_digest
    )


def _validate_root(root: Path) -> None:
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise OverlayError("overlay_root_invalid") from exc
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode) or resolved != root:
        raise OverlayError("overlay_root_invalid")


def _validate_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OverlayError("overlay_path_unavailable") from exc
    if _is_link_or_reparse(metadata):
        raise OverlayError("overlay_path_reparse")
    if not stat.S_ISDIR(metadata.st_mode):
        raise OverlayError("overlay_path_invalid")


def _prepare_directory(request: OverlayRequest) -> Path:
    root = request.repository_root
    _validate_root(root)
    output = _lexical_output_root(request)
    _validate_directory(output)
    current = output
    for part in ("overlays", request.provider, request.overlay_identity_digest):
        current /= part
        if not current.exists() and not current.is_symlink():
            try:
                if os.name == "nt":
                    from .probe_workspace import _create_private_windows_directory

                    _create_private_windows_directory(current.parent, current.name)
                else:
                    current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise OverlayError("overlay_path_unavailable") from exc
        _validate_directory(current)
        if os.name == "nt":
            try:
                from .probe_workspace import _set_private_windows_dacl

                _set_private_windows_dacl(current)
            except OSError as exc:
                raise OverlayError("overlay_permissions_failed") from exc
        else:
            try:
                os.chmod(current, 0o700)
            except OSError as exc:
                raise OverlayError("overlay_permissions_failed") from exc
    return current


def _validate_existing_storage_path(request: OverlayRequest) -> None:
    """Reject an existing hostile component before processing provider config."""
    root = request.repository_root
    _validate_root(root)
    current = _lexical_output_root(request)
    _validate_directory(current)
    for part in ("overlays", request.provider, request.overlay_identity_digest):
        current /= part
        if not current.exists() and not current.is_symlink():
            break
        _validate_directory(current)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise OverlayError("overlay_missing") from None
    except OSError as exc:
        raise OverlayError("overlay_unreadable") from exc
    if _is_link_or_reparse(metadata):
        raise OverlayError("overlay_path_reparse")
    if not stat.S_ISREG(metadata.st_mode):
        raise OverlayError("overlay_path_invalid")
    if metadata.st_size > MAX_OVERLAY_FILE_BYTES:
        raise OverlayError("overlay_too_large")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            remaining = MAX_OVERLAY_FILE_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise OverlayError("overlay_unreadable") from exc
    def signature(item: os.stat_result) -> tuple[int, int, int, int]:
        return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
    if signature(opened) != signature(metadata) or signature(after) != signature(opened):
        raise OverlayError("overlay_changed")
    if len(raw) > MAX_OVERLAY_FILE_BYTES:
        raise OverlayError("overlay_too_large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise OverlayError("overlay_invalid") from exc
    if not isinstance(payload, dict):
        raise OverlayError("overlay_invalid")
    return payload


def _read_bytes(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise OverlayError("overlay_missing") from None
    except OSError as exc:
        raise OverlayError("overlay_unreadable") from exc
    if _is_link_or_reparse(metadata):
        raise OverlayError("overlay_path_reparse")
    if not stat.S_ISREG(metadata.st_mode):
        raise OverlayError("overlay_path_invalid")
    if metadata.st_size > MAX_OVERLAY_FILE_BYTES:
        raise OverlayError("overlay_too_large")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OverlayError("overlay_unreadable") from exc
    if len(raw) > MAX_OVERLAY_FILE_BYTES:
        raise OverlayError("overlay_too_large")
    return raw


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _atomic_write_secure(path: Path, payload: dict[str, Any]) -> str:
    raw = _json_bytes(payload)
    if len(raw) > MAX_OVERLAY_FILE_BYTES:
        raise OverlayError("overlay_too_large")
    _validate_directory(path.parent)
    if path.exists() or path.is_symlink():
        try:
            existing = path.lstat()
        except OSError as exc:
            raise OverlayError("overlay_write_failed") from exc
        if _is_link_or_reparse(existing):
            raise OverlayError("overlay_path_reparse")
        if not stat.S_ISREG(existing.st_mode):
            raise OverlayError("overlay_path_invalid")
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
    except OSError as exc:
        raise OverlayError("overlay_write_failed") from exc
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _validate_directory(path.parent)
        os.replace(temporary, path)
        if os.name == "nt":
            from .probe_workspace import _set_private_windows_dacl

            _set_private_windows_dacl(path)
        else:
            os.chmod(path, 0o600)
    except Exception as exc:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        finally:
            if isinstance(exc, OverlayError):
                raise
            raise OverlayError("overlay_write_failed") from exc
    return hashlib.sha256(raw).hexdigest()


def canonical_graph_fingerprint(path: Path, *, root: Path) -> str:
    """Hash the validated canonical bundle using routing's canonical JSON contract."""
    try:
        bundle, _ = load_validated_graph_bundle(path, root=root)
    except GraphReadError as exc:
        raise OverlayError(f"canonical_{exc.code}") from exc
    return _canonical_digest(bundle)


def _canonical_bundle(request: OverlayRequest, cfg: Config) -> tuple[dict[str, Any], str]:
    output = _lexical_output_root(request)
    manifest_path = output / ".graphite_manifest.json"
    try:
        manifest_metadata = manifest_path.lstat()
    except OSError as exc:
        raise OverlayError("canonical_graph_missing") from exc
    if _is_link_or_reparse(manifest_metadata):
        raise OverlayError("canonical_graph_reparse")
    if (
        not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_metadata.st_size > MAX_CANONICAL_MANIFEST_BYTES
    ):
        raise OverlayError("canonical_graph_invalid")
    selected = cfg.canonical_graph()
    selected.output_dir = output
    try:
        freshness = check_graph_freshness(
            request.repository_root,
            selected,
            max_manifest_bytes=MAX_CANONICAL_MANIFEST_BYTES,
        )
    except FreshnessLimitError as exc:
        raise OverlayError("canonical_graph_invalid") from exc
    if freshness.get("stale", True):
        raise OverlayError("canonical_graph_stale")
    try:
        bundle, _ = load_validated_graph_bundle(
            output / "graph.json", root=request.repository_root
        )
    except GraphReadError as exc:
        raise OverlayError(f"canonical_{exc.code}") from exc
    return bundle, _canonical_digest(bundle)


def _validate_model_config(request: OverlayRequest, cfg: Config) -> None:
    if cfg.llm_provider.strip().casefold().replace("_", "-") != request.provider:
        raise OverlayError("overlay_provider_mismatch")
    expected_mode = "local" if request.provider == "ollama" else "cloud"
    if cfg.llm_mode != expected_mode:
        raise OverlayError("overlay_mode_invalid")
    if (
        not isinstance(cfg.llm_model, str)
        or not cfg.llm_model
        or len(cfg.llm_model) > 256
        or any(ord(character) < 32 for character in cfg.llm_model)
    ):
        raise OverlayError("overlay_model_invalid")
    if (
        not math.isfinite(cfg.llm_timeout_seconds)
        or not 0 < cfg.llm_timeout_seconds <= 300
        or isinstance(cfg.llm_max_input_chars, bool)
        or not 1 <= cfg.llm_max_input_chars <= 1_000_000
        or isinstance(cfg.llm_max_output_tokens, bool)
        or not 1 <= cfg.llm_max_output_tokens <= 4_096
    ):
        raise OverlayError("overlay_limits_invalid")
    if request.provider == "ollama":
        endpoint = cfg.llm_base_url or "http://localhost:11434"
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise OverlayError("overlay_endpoint_invalid")
    elif cfg.llm_base_url is not None and cfg.llm_base_url.rstrip("/") != "https://openrouter.ai/api/v1":
        raise OverlayError("overlay_endpoint_invalid")


def _identity_values(request: OverlayRequest) -> dict[str, Any]:
    return {
        "overlay_identity_digest": request.overlay_identity_digest,
        "provider": request.provider,
        "provider_lifecycle_identity_digest": request.provider_lifecycle_identity_digest,
        "model_identity_digest": request.model_identity_digest,
        "routing_policy_digest": request.routing_policy_digest,
    }


def _check_collision(directory: Path, request: OverlayRequest) -> None:
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists() and not manifest_path.is_symlink():
        return
    try:
        existing = _read_json(manifest_path)
    except OverlayError as exc:
        raise OverlayError("overlay_identity_collision") from exc
    expected = _identity_values(request)
    if any(existing.get(field) != expected[field] for field in _IDENTITY_FIELDS):
        raise OverlayError("overlay_identity_collision")


def _validate_success_manifest(manifest: dict[str, Any]) -> None:
    try:
        provider = manifest["provider"]
        payload_file = manifest["payload_file"]
        payload_digest = manifest["payload_sha256"]
        created_at = manifest["created_at"]
        limits = manifest["limits"]
    except KeyError as exc:
        raise OverlayError("overlay_invalid") from exc
    payload_match = _PAYLOAD_FILE.fullmatch(payload_file) if isinstance(payload_file, str) else None
    if (
        manifest.get("schema_version") != OVERLAY_SCHEMA_VERSION
        or manifest.get("non_authoritative") is not True
        or manifest.get("outcome_category") != "succeeded"
        or provider not in _PROVIDERS
        or payload_match is None
        or isinstance(created_at, bool)
        or not isinstance(created_at, int)
        or not 0 <= created_at <= 10**12
        or not isinstance(limits, dict)
        or set(limits)
        != {"timeout_seconds", "max_input_chars", "max_output_tokens"}
    ):
        raise OverlayError("overlay_invalid")
    try:
        normalized_payload_digest = _digest(
            payload_digest, "payload_digest_invalid"
        )
    except ValueError as exc:
        raise OverlayError("overlay_invalid") from exc
    if normalized_payload_digest != payload_match.group(1):
        raise OverlayError("overlay_invalid")
    for field, code in (
        ("canonical_graph_fingerprint", "canonical_graph_fingerprint_invalid"),
        ("overlay_identity_digest", "overlay_identity_digest_invalid"),
        ("provider_lifecycle_identity_digest", "provider_identity_digest_invalid"),
        ("model_identity_digest", "model_identity_digest_invalid"),
    ):
        try:
            _digest(manifest.get(field), code)
        except ValueError as exc:
            raise OverlayError("overlay_invalid") from exc
    routing = manifest.get("routing_policy_digest")
    try:
        _digest(routing, "routing_policy_digest_invalid", optional=True)
    except ValueError as exc:
        raise OverlayError("overlay_invalid") from exc
    if (provider == "openrouter") != (routing is not None):
        raise OverlayError("overlay_invalid")
    timeout = limits["timeout_seconds"]
    max_input = limits["max_input_chars"]
    max_output = limits["max_output_tokens"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 300
        or isinstance(max_input, bool)
        or not isinstance(max_input, int)
        or not 1 <= max_input <= 1_000_000
        or isinstance(max_output, bool)
        or not isinstance(max_output, int)
        or not 1 <= max_output <= 4_096
    ):
        raise OverlayError("overlay_invalid")
    expected_identity = _canonical_digest(
        {
            "provider": provider,
            "provider_lifecycle_identity_digest": manifest[
                "provider_lifecycle_identity_digest"
            ],
            "model_identity_digest": manifest["model_identity_digest"],
            "routing_policy_digest": routing,
        }
    )
    if manifest["overlay_identity_digest"] != expected_identity:
        raise OverlayError("overlay_invalid")


def _failure_category(result: dict[str, Any]) -> str:
    category = result.get("error_category")
    return category if category in _FAILURE_CATEGORIES else "provider_error"


def _result_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    summary = result.get("summary")
    values = (
        result.get("input_tokens", 0),
        result.get("output_tokens", 0),
        result.get("tokens", 0),
    )
    if (
        not isinstance(summary, str)
        or not summary
        or len(summary.encode("utf-8")) > MAX_SUMMARY_BYTES
        or any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**9 for value in values)
    ):
        return None
    return {
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "non_authoritative": True,
        "summary": summary,
        "input_tokens": values[0],
        "output_tokens": values[1],
        "total_tokens": values[2],
    }


def _base_record(request: OverlayRequest, canonical_fingerprint: str) -> dict[str, Any]:
    return {
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "non_authoritative": True,
        "canonical_graph_fingerprint": canonical_fingerprint,
        **_identity_values(request),
        "created_at": request.created_at,
    }


def _write_failure(
    directory: Path,
    request: OverlayRequest,
    canonical_fingerprint: str,
    category: str,
) -> dict[str, Any]:
    marker = {
        **_base_record(request, canonical_fingerprint),
        "outcome_category": "failed",
        "failure_category": category if category in _FAILURE_CATEGORIES else "provider_error",
    }
    _atomic_write_secure(directory / "failure.json", marker)
    return marker


def build_overlay(
    request: OverlayRequest,
    cfg: Config,
    *,
    enrich: Callable[[dict[str, Any], dict[str, Any], dict[str, Any], Config], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one optional overlay without mutating canonical graph artifacts."""
    _validate_existing_storage_path(request)
    _validate_model_config(request, cfg)
    bundle, canonical_fingerprint = _canonical_bundle(request, cfg)
    directory = _prepare_directory(request)
    _check_collision(directory, request)

    if request.provider == "openrouter" and not cfg.llm_api_key:
        return _write_failure(
            directory, request, canonical_fingerprint, "authentication"
        )

    graph_data = {
        "nodes": bundle.get("nodes", []),
        "edges": bundle.get("edges", []),
        "metadata": {
            "node_count": (bundle.get("metadata") or {}).get("node_count", 0),
            "edge_count": (bundle.get("metadata") or {}).get("edge_count", 0),
        },
    }
    clusters = {
        "clusters": bundle.get("clusters", []),
        "count": (bundle.get("metadata") or {}).get("community_count", 0),
    }
    analysis = bundle.get("analysis") or {}
    runner = enrich or enrich_report
    try:
        result = runner(graph_data, clusters, analysis, cfg)
    except Exception:
        result = {"status": "error", "error_category": "provider_error"}
    if not isinstance(result, dict) or result.get("status") != "ok":
        selected = result if isinstance(result, dict) else {}
        return _write_failure(
            directory,
            request,
            canonical_fingerprint,
            _failure_category(selected),
        )
    payload = _result_payload(result)
    if payload is None:
        return _write_failure(
            directory, request, canonical_fingerprint, "provider_error"
        )

    _, current_fingerprint = _canonical_bundle(request, cfg)
    if current_fingerprint != canonical_fingerprint:
        raise OverlayError("canonical_graph_changed")

    payload_bytes = _json_bytes(payload)
    payload_digest = hashlib.sha256(payload_bytes).hexdigest()
    payload_name = f"payload-{payload_digest}.json"
    payload_path = directory / payload_name
    if payload_path.exists() or payload_path.is_symlink():
        if _read_bytes(payload_path) != payload_bytes:
            raise OverlayError("overlay_payload_collision")
        written_digest = payload_digest
    else:
        written_digest = _atomic_write_secure(payload_path, payload)
    if written_digest != payload_digest:
        raise OverlayError("overlay_write_failed")
    manifest = {
        **_base_record(request, canonical_fingerprint),
        "limits": {
            "timeout_seconds": float(cfg.llm_timeout_seconds),
            "max_input_chars": cfg.llm_max_input_chars,
            "max_output_tokens": cfg.llm_max_output_tokens,
        },
        "outcome_category": "succeeded",
        "payload_file": payload_name,
        "payload_sha256": payload_digest,
    }
    _atomic_write_secure(directory / "manifest.json", manifest)
    return manifest


def evaluate_overlay_staleness(
    manifest: dict[str, Any],
    *,
    canonical_graph_fingerprint: str,
    provider_lifecycle_identity_digest: str,
    model_identity_digest: str,
    routing_policy_digest: str | None,
) -> dict[str, Any]:
    """Evaluate opt-in overlay freshness without affecting canonical freshness."""
    _validate_success_manifest(manifest)
    current_graph = _digest(
        canonical_graph_fingerprint, "canonical_graph_fingerprint_invalid"
    )
    current_provider = _digest(
        provider_lifecycle_identity_digest, "provider_identity_digest_invalid"
    )
    current_model = _digest(model_identity_digest, "model_identity_digest_invalid")
    current_routing = _digest(
        routing_policy_digest, "routing_policy_digest_invalid", optional=True
    )
    reasons: list[str] = []
    if manifest.get("canonical_graph_fingerprint") != current_graph:
        reasons.append("canonical_graph_changed")
    if manifest.get("model_identity_digest") != current_model:
        reasons.append("model_identity_changed")
    if manifest.get("provider_lifecycle_identity_digest") != current_provider:
        reasons.append("provider_identity_changed")
    if manifest.get("routing_policy_digest") != current_routing:
        reasons.append("routing_policy_changed")
    return {
        "status": "stale" if reasons else "current",
        "stale_reasons": sorted(reasons),
    }


def load_overlay_manifest(request: OverlayRequest) -> dict[str, Any]:
    """Opt in to reading a bounded manifest for one exact overlay identity."""
    _validate_existing_storage_path(request)
    directory = overlay_directory(request)
    manifest = _read_json(directory / "manifest.json")
    _validate_success_manifest(manifest)
    expected = _identity_values(request)
    if any(manifest.get(field) != expected[field] for field in _IDENTITY_FIELDS):
        raise OverlayError("overlay_identity_collision")
    payload = _read_bytes(directory / manifest["payload_file"])
    if hashlib.sha256(payload).hexdigest() != manifest["payload_sha256"]:
        raise OverlayError("overlay_invalid")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise OverlayError("overlay_invalid") from exc
    if not isinstance(decoded, dict):
        raise OverlayError("overlay_invalid")
    return manifest
