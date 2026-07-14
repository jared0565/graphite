"""Contained, bounded context selection with a public outbound manifest."""
from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from .contracts import TaskRequest
from .settings import RoutingSettings

CONTEXT_SCHEMA_VERSION: Final = "1"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_EXCLUDED_COMPONENTS = frozenset(
    {
        ".git",
        ".graphite",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        "graph-out",
        "node_modules",
        "dist",
        "build",
        "coverage",
        "vendor",
        ".venv",
        "venv",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der"})
_SENSITIVE_NAMES = frozenset(
    {
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
        "keystore",
    }
)
_ENCODED_TOKEN = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/])")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*[^\s]{8,}"),
    re.compile(r"(?i)authorization\s*:\s*bearer\s+[^\s]{8,}"),
    re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class ContextError(RuntimeError):
    """A stable, path-free context construction failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ManifestItem:
    path: str
    byte_count: int
    sha256: str
    reason: str
    redaction_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "reason": self.reason,
            "redaction_count": self.redaction_count,
        }


@dataclass(frozen=True)
class PrivateContextItem:
    path: str
    content: str


@dataclass(frozen=True)
class OutboundManifest:
    schema_version: str
    items: tuple[ManifestItem, ...]
    total_bytes: int
    excluded_count: int
    manifest_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "items": [item.to_dict() for item in self.items],
            "total_bytes": self.total_bytes,
            "excluded_count": self.excluded_count,
            "manifest_hash": self.manifest_hash,
        }


@dataclass(frozen=True)
class ContextBundle:
    manifest: OutboundManifest
    private_items: tuple[PrivateContextItem, ...]


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _signature(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _normalize_relative(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if normalized.startswith("/") or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    if pure.parts and re.match(r"^[A-Za-z]:$", pure.parts[0]):
        return None
    return pure.as_posix()


def _candidate_reasons(request: TaskRequest, graph: Any) -> dict[str, str]:
    reasons: dict[str, str] = {target: "explicit_target" for target in request.targets}
    target_set = set(request.targets)
    matched_nodes: list[str] = []
    try:
        nodes = graph.nodes(data=True)
    except (AttributeError, TypeError):
        return reasons
    for node_id, data in nodes:
        if isinstance(data, dict) and data.get("source_file") in target_set:
            matched_nodes.append(str(node_id))
    for node in sorted(set(matched_nodes)):
        try:
            dependencies = sorted(graph.successors(node), key=str)
            dependents = sorted(graph.predecessors(node), key=str)
        except (AttributeError, KeyError):
            continue
        for neighbor, reason in (
            *((item, "dependency") for item in dependencies),
            *((item, "dependent") for item in dependents),
        ):
            try:
                data = graph.nodes[neighbor]
            except (AttributeError, KeyError):
                continue
            relative = _normalize_relative(data.get("source_file") if isinstance(data, dict) else None)
            if relative is not None:
                reasons.setdefault(relative, reason)
    return reasons


def _excluded_by_name(relative: str, exclusions: frozenset[str]) -> bool:
    pure = PurePosixPath(relative)
    lowered_parts = tuple(part.casefold() for part in pure.parts)
    name = pure.name.casefold()
    if any(part in _EXCLUDED_COMPONENTS for part in lowered_parts):
        return True
    if (
        name == ".env"
        or name.startswith(".env.")
        or name in _SENSITIVE_NAMES
        or pure.suffix.casefold() in _SENSITIVE_SUFFIXES
        or pure.suffix.casefold() in {".jks", ".kdbx", ".keystore"}
    ):
        return True
    return any(relative == exclusion or relative.startswith(exclusion.rstrip("/") + "/") for exclusion in exclusions)


def _read_stable(root: Path, relative: str, limit: int) -> bytes | None:
    candidate = root / PurePosixPath(relative)
    current = root
    for part in PurePosixPath(relative).parts[:-1]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            return None
    try:
        before = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if stat.S_ISLNK(before.st_mode) or _is_reparse(before) or not stat.S_ISREG(before.st_mode):
        return None
    if before.st_size > limit:
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(candidate, flags)
        try:
            opened = os.fstat(descriptor)
            if _signature(opened) != _signature(before):
                raise ContextError("context_file_changed")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except ContextError:
        raise
    except OSError:
        return None
    data = b"".join(chunks)
    if len(data) > limit:
        return None
    if _signature(after) != _signature(opened):
        raise ContextError("context_file_changed")
    return data


def _safe_text(data: bytes) -> str | None:
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        return None
    for match in _ENCODED_TOKEN.finditer(text):
        try:
            decoded = base64.b64decode(match.group(0), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if any(pattern.search(decoded) for pattern in _SECRET_PATTERNS):
            return None
    return text


def build_routing_context(
    request: TaskRequest,
    graph: Any,
    settings: RoutingSettings,
    *,
    exclusions: tuple[str, ...] = (),
) -> ContextBundle:
    """Build private context plus a source-free public approval manifest."""
    root = request.repository_root
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise ContextError("context_root_invalid") from exc
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ContextError("context_root_invalid")
    root = root.resolve(strict=True)
    if not 1 <= settings.max_context_files <= 128:
        raise ContextError("context_file_limit_invalid")
    if not 16_384 <= settings.max_context_bytes <= 4_194_304:
        raise ContextError("context_byte_limit_invalid")
    normalized_exclusions = frozenset(
        item for value in exclusions if (item := _normalize_relative(value)) is not None
    )
    reasons = _candidate_reasons(request, graph)
    items: list[ManifestItem] = []
    private: list[PrivateContextItem] = []
    excluded_count = 0
    total_bytes = 0
    for raw_relative in sorted(reasons):
        relative = _normalize_relative(raw_relative)
        if relative is None or _excluded_by_name(relative, normalized_exclusions):
            excluded_count += 1
            continue
        if len(items) >= settings.max_context_files:
            excluded_count += 1
            continue
        remaining = settings.max_context_bytes - total_bytes
        if remaining <= 0:
            excluded_count += 1
            continue
        data = _read_stable(root, relative, remaining)
        if data is None:
            excluded_count += 1
            continue
        text = _safe_text(data)
        if text is None:
            excluded_count += 1
            continue
        digest = hashlib.sha256(data).hexdigest()
        items.append(ManifestItem(relative, len(data), digest, reasons[raw_relative]))
        total_bytes += len(data)
        if request.data_policy == "source_allowed":
            private.append(PrivateContextItem(relative, text))
    material = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "items": [item.to_dict() for item in items],
        "total_bytes": total_bytes,
        "excluded_count": excluded_count,
    }
    manifest_hash = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = OutboundManifest(
        CONTEXT_SCHEMA_VERSION,
        tuple(items),
        total_bytes,
        excluded_count,
        manifest_hash,
    )
    return ContextBundle(manifest, tuple(private))
