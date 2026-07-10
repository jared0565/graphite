"""Source resolution helpers for Graphite extraction."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .config import Config
from .ts_bridge import TypeScriptCompilerEdge, TypeScriptCompilerIndex, build_typescript_index

_TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_INDEX_NAMES = tuple(f"index{ext}" for ext in _TS_EXTENSIONS)
_BUILTIN_OBJECTS = {
    "Array", "Boolean", "Date", "Error", "Intl", "JSON", "Math", "Number",
    "Object", "Promise", "Reflect", "RegExp", "String", "Symbol", "URL",
    "URLSearchParams", "console", "process", "window", "document",
}
_NOISY_MEMBER_CALLS = {
    "addEventListener", "append", "appendChild", "catch", "concat", "delete", "endsWith",
    "entries", "every", "filter", "find", "findIndex", "flat", "flatMap", "forEach",
    "get", "has", "includes", "join", "keys", "log", "map", "match", "max", "min",
    "parse", "push", "reduce", "remove", "removeEventListener", "replace", "set", "slice",
    "some", "sort", "split", "startsWith", "stringify", "then", "toISOString",
    "toLocaleString", "toLowerCase", "toString", "toUpperCase", "trim", "values",
}


@dataclass(frozen=True)
class ResolvedImport:
    rel_path: str
    confidence: str


@dataclass(frozen=True)
class SourceIndex:
    """Known project files plus tsconfig path aliases and optional TS compiler resolution."""

    root: Path
    rel_paths: frozenset[str]
    path_aliases: tuple[tuple[str, tuple[str, ...]], ...]
    typescript: TypeScriptCompilerIndex
    # (package name, package dir rel path, entry file rel path) per workspace
    # package.json found in the scan — lets bare imports like "@repo/utils"
    # resolve to the workspace source file instead of an external phantom.
    workspace_packages: tuple[tuple[str, str, str], ...] = ()

    @classmethod
    def from_entries(cls, entries: Iterable[object], cfg: Config | None = None) -> "SourceIndex":
        entries = list(entries)
        root = Path.cwd().resolve()
        rel_paths: set[str] = set()
        if entries:
            first = entries[0]
            root = Path(first.abs_path).resolve()
            for _ in PurePosixPath(first.rel_path).parts:
                root = root.parent
        for entry in entries:
            rel_paths.add(PurePosixPath(entry.rel_path).as_posix())
        cfg = cfg or Config()
        ts_index = build_typescript_index(root, entries, cfg)
        return cls(
            root=root,
            rel_paths=frozenset(rel_paths),
            path_aliases=_load_tsconfig_aliases(root),
            typescript=ts_index,
            workspace_packages=_load_workspace_packages(root, frozenset(rel_paths)),
        )

    def resolve_ts_import(self, rel_path: str, source_lit: str) -> str | None:
        """Resolve TS/JS imports to a known relative file path when possible."""
        resolved = self.resolve_ts_import_detail(rel_path, source_lit)
        return resolved.rel_path if resolved else None

    def resolve_ts_import_detail(self, rel_path: str, source_lit: str) -> ResolvedImport | None:
        """Resolve imports, preferring TypeScript compiler results over heuristics."""
        compiler_edge = self.typescript.resolve_import(rel_path, source_lit)
        if compiler_edge:
            return ResolvedImport(compiler_edge.target, compiler_edge.confidence)

        candidates: list[str] = []
        if source_lit.startswith("."):
            base = PurePosixPath(rel_path).parent.joinpath(source_lit)
            candidates.extend(_candidate_paths(base))
        else:
            for pattern, targets in self.path_aliases:
                matched = _match_alias(pattern, source_lit)
                if matched is None:
                    continue
                for target in targets:
                    candidates.extend(_candidate_paths(PurePosixPath(target.replace("*", matched))))

        for candidate in candidates:
            normalized = PurePosixPath(candidate).as_posix().lstrip("./")
            if normalized in self.rel_paths:
                return ResolvedImport(normalized, "EXACT_IMPORT")

        workspace = self._resolve_workspace_import(source_lit)
        if workspace is not None:
            return workspace
        return None

    def _resolve_workspace_import(self, source_lit: str) -> ResolvedImport | None:
        """Resolve a bare import against workspace packages (monorepo).

        Exact name ("@repo/utils") resolves to the package entry file; a
        subpath ("@repo/utils/money") resolves inside the package dir (also
        trying src/). Longest package name wins so scoped packages nest safely.
        """
        for name, pkg_dir, entry in sorted(
            self.workspace_packages, key=lambda item: len(item[0]), reverse=True
        ):
            if source_lit == name:
                return ResolvedImport(entry, "WORKSPACE_IMPORT")
            if source_lit.startswith(name + "/"):
                remainder = source_lit[len(name) + 1:]
                candidates: list[str] = []
                candidates.extend(_candidate_paths(PurePosixPath(pkg_dir).joinpath(remainder)))
                candidates.extend(_candidate_paths(PurePosixPath(pkg_dir).joinpath("src", remainder)))
                for candidate in candidates:
                    normalized = PurePosixPath(candidate).as_posix().lstrip("./")
                    if normalized in self.rel_paths:
                        return ResolvedImport(normalized, "WORKSPACE_IMPORT")
        return None

    def supplemental_ts_edges(self, rel_path: str) -> tuple[TypeScriptCompilerEdge, ...]:
        """Compiler-discovered export/dynamic-import edges for a file."""
        return self.typescript.supplemental_edges(rel_path)


def should_keep_call_target(called: str) -> bool:
    """Drop common built-in/member calls that create graph noise."""
    if not called:
        return False
    if "." not in called:
        return True
    obj, prop = called.rsplit(".", 1)
    root = obj.split(".", 1)[0]
    if root in _BUILTIN_OBJECTS:
        return False
    if prop in _NOISY_MEMBER_CALLS:
        return False
    return True


def _candidate_paths(base: PurePosixPath) -> list[str]:
    base_str = base.as_posix()
    out = [base_str]
    if base.suffix:
        return out
    out.extend(f"{base_str}{ext}" for ext in _TS_EXTENSIONS)
    out.extend(f"{base_str}/{name}" for name in _INDEX_NAMES)
    return out


def _load_workspace_packages(
    root: Path, rel_paths: frozenset[str]
) -> tuple[tuple[str, str, str], ...]:
    """Map workspace package names to their entry source file.

    Every scanned package.json (node_modules is never scanned) whose "name"
    can be tied to an in-repo entry file becomes (name, pkg_dir, entry).
    Entry preference: exports["."] (string or the first string among
    import/default/require/module/main condition keys), then "module", then
    "main", then src/index.* / index.* fallbacks. The root package.json is
    included too — resolving an import of the root package name is harmless.
    """
    packages: list[tuple[str, str, str]] = []
    for rel in sorted(rel_paths):
        posix = PurePosixPath(rel)
        if posix.name != "package.json":
            continue
        try:
            data = json.loads((root / posix).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        pkg_dir = posix.parent.as_posix()
        if pkg_dir == ".":
            pkg_dir = ""
        entry = _package_entry(data, pkg_dir, rel_paths)
        if entry is None:
            continue
        packages.append((name.strip(), pkg_dir, entry))
    return tuple(packages)


def _package_entry(
    data: dict, pkg_dir: str, rel_paths: frozenset[str]
) -> str | None:
    declared: list[str] = []
    exports = data.get("exports")
    if isinstance(exports, str):
        declared.append(exports)
    elif isinstance(exports, dict):
        dot = exports.get(".")
        if isinstance(dot, str):
            declared.append(dot)
        elif isinstance(dot, dict):
            for key in ("import", "default", "require", "module", "main"):
                value = dot.get(key)
                if isinstance(value, str):
                    declared.append(value)
    for key in ("module", "main", "types"):
        value = data.get(key)
        if isinstance(value, str):
            declared.append(value)

    candidates: list[str] = []
    base = PurePosixPath(pkg_dir) if pkg_dir else PurePosixPath(".")
    for spec in declared:
        candidates.extend(_candidate_paths(base.joinpath(spec.lstrip("./"))))
    for fallback in ("src/index", "index"):
        candidates.extend(_candidate_paths(base.joinpath(fallback)))

    for candidate in candidates:
        normalized = PurePosixPath(candidate).as_posix().lstrip("./")
        if normalized in rel_paths:
            return normalized
    return None


def _load_tsconfig_aliases(root: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    path = root / "tsconfig.json"
    if not path.exists():
        return ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    compiler = data.get("compilerOptions", {}) if isinstance(data, dict) else {}
    base_url = compiler.get("baseUrl", ".")
    paths = compiler.get("paths", {})
    if not isinstance(paths, dict):
        return ()
    aliases: list[tuple[str, tuple[str, ...]]] = []
    for pattern, targets in sorted(paths.items()):
        if isinstance(pattern, str) and isinstance(targets, list):
            normalized_targets = []
            for target in targets:
                if isinstance(target, str):
                    normalized_targets.append(PurePosixPath(base_url).joinpath(target).as_posix())
            if normalized_targets:
                aliases.append((pattern, tuple(normalized_targets)))
    return tuple(aliases)


def _match_alias(pattern: str, import_name: str) -> str | None:
    if "*" not in pattern:
        return "" if pattern == import_name else None
    prefix, suffix = pattern.split("*", 1)
    if not import_name.startswith(prefix) or not import_name.endswith(suffix):
        return None
    return import_name[len(prefix): len(import_name) - len(suffix) if suffix else len(import_name)]
