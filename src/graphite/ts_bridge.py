"""Optional TypeScript compiler bridge for accurate import/export resolution."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Any

from .config import Config

_TS_LANGUAGES = {"javascript", "typescript", "tsx", "jsx"}


@dataclass(frozen=True)
class TypeScriptCompilerEdge:
    source: str
    target: str
    specifier: str
    syntax: str
    relation: str
    confidence: str
    line: int | None = None


@dataclass(frozen=True)
class TypeScriptCompilerIndex:
    available: bool
    reason: str | None = None
    typescript_version: str | None = None
    config_path: str | None = None
    import_map: dict[tuple[str, str], TypeScriptCompilerEdge] = field(default_factory=dict)
    edges_by_file: dict[str, tuple[TypeScriptCompilerEdge, ...]] = field(default_factory=dict)

    def resolve_import(self, rel_path: str, specifier: str) -> TypeScriptCompilerEdge | None:
        return self.import_map.get((PurePosixPath(rel_path).as_posix(), specifier))

    def supplemental_edges(self, rel_path: str) -> tuple[TypeScriptCompilerEdge, ...]:
        return self.edges_by_file.get(PurePosixPath(rel_path).as_posix(), ())


_DISABLED = {"0", "false", "off", "none", "disabled", "heuristic"}


def build_typescript_index(root: Path, entries: Iterable[object], cfg: Config) -> TypeScriptCompilerIndex:
    """Build a compiler-backed TS module-resolution index, falling back silently on failure."""
    mode = cfg.typescript_resolver.strip().lower()
    if mode in _DISABLED:
        return TypeScriptCompilerIndex(available=False, reason="disabled")

    rel_files = sorted(
        PurePosixPath(entry.rel_path).as_posix()
        for entry in entries
        if getattr(entry, "language", None) in _TS_LANGUAGES
    )
    if not rel_files:
        return TypeScriptCompilerIndex(available=False, reason="no_typescript_files")

    script = Path(__file__).with_name("ts_resolver.mjs")
    payload = json.dumps({"root": str(root), "files": rel_files, "symbolReferences": cfg.typescript_symbol_references}, ensure_ascii=False)
    try:
        completed = subprocess.run(
            ["node", str(script)],
            input=payload,
            text=True,
            capture_output=True,
            cwd=str(Path.cwd()),
            timeout=cfg.typescript_resolver_timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        return TypeScriptCompilerIndex(available=False, reason="node_not_available")
    except subprocess.TimeoutExpired:
        return TypeScriptCompilerIndex(available=False, reason="timeout")
    except Exception as exc:
        return TypeScriptCompilerIndex(available=False, reason=f"bridge_error: {exc}")

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:500]
        return TypeScriptCompilerIndex(available=False, reason=f"node_exit_{completed.returncode}: {detail}")

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return TypeScriptCompilerIndex(available=False, reason=f"invalid_json: {exc}")

    if not data.get("ok"):
        return TypeScriptCompilerIndex(available=False, reason=str(data.get("reason") or "unavailable"))

    import_map: dict[tuple[str, str], TypeScriptCompilerEdge] = {}
    edges_by_file: dict[str, list[TypeScriptCompilerEdge]] = {}
    for raw in data.get("edges", []):
        edge = _edge_from_raw(raw)
        if edge is None:
            continue
        if edge.syntax == "import":
            import_map[(edge.source, edge.specifier)] = edge
        else:
            edges_by_file.setdefault(edge.source, []).append(edge)

    return TypeScriptCompilerIndex(
        available=True,
        typescript_version=data.get("typescriptVersion"),
        config_path=data.get("configPath"),
        import_map=import_map,
        edges_by_file={k: tuple(v) for k, v in edges_by_file.items()},
    )


def _edge_from_raw(raw: dict[str, Any]) -> TypeScriptCompilerEdge | None:
    try:
        source = PurePosixPath(str(raw["source"])).as_posix()
        target = PurePosixPath(str(raw["target"])).as_posix()
        specifier = str(raw["specifier"])
        syntax = str(raw["syntax"])
        relation = str(raw["relation"])
        confidence = str(raw["confidence"])
        line_raw = raw.get("line")
        line = int(line_raw) if line_raw is not None else None
    except (KeyError, TypeError, ValueError):
        return None
    if not source or not target or source == target:
        return None
    return TypeScriptCompilerEdge(
        source=source,
        target=target,
        specifier=specifier,
        syntax=syntax,
        relation=relation,
        confidence=confidence,
        line=line,
    )


