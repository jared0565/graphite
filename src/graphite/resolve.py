"""Source resolution helpers for Graphite extraction."""
from __future__ import annotations

import json
import posixpath
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
    # (scope dir rel path, pattern, target globs) per tsconfig.json found in
    # the scan — an alias only applies to files under its tsconfig's directory,
    # ordered longest scope first so the nearest tsconfig wins.
    path_aliases: tuple[tuple[str, str, tuple[str, ...]], ...]
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
            path_aliases=_load_tsconfig_aliases(root, frozenset(rel_paths)),
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
            for scope, pattern, targets in self.path_aliases:
                if scope and not rel_path.startswith(scope + "/"):
                    continue
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


# TypeScript NodeNext/ESM writes relative import specifiers with a JS-family
# extension (the *emitted* file name) while the source on disk is the matching
# TS extension — e.g. `import '../db/queries.js'` resolves to `queries.ts`. Map
# each JS extension to the TS source extensions to try. Order matters: TS source
# is preferred over the literal path so a `.js` specifier resolves to the source,
# not a compiled `.js` twin sitting beside it.
_JS_TO_TS_EXTENSIONS: dict[str, tuple[str, ...]] = {
    ".js": (".ts", ".tsx"),
    ".jsx": (".tsx",),
    ".mjs": (".mts",),
    ".cjs": (".cts",),
}


def _candidate_paths(base: PurePosixPath) -> list[str]:
    # Collapse any ".."/"." segments so a relative specifier like
    # "../db/queries" from "src/routes/x.ts" becomes "src/db/queries" and can
    # match a scanned rel_path (PurePosixPath.joinpath does NOT normalize "..").
    base_str = posixpath.normpath(base.as_posix())
    root, suffix = posixpath.splitext(base_str)
    if suffix:
        ts_swaps = _JS_TO_TS_EXTENSIONS.get(suffix)
        if ts_swaps:
            # Prefer the TS source of the same stem, then fall back to the
            # literal path (a real .js/.jsx file with no TS source may exist).
            return [f"{root}{ext}" for ext in ts_swaps] + [base_str]
        return [base_str]
    out = [base_str]
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


def _load_tsconfig_aliases(
    root: Path, rel_paths: frozenset[str]
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """Path aliases from every scanned tsconfig.json, scoped to its directory.

    Monorepo packages declare their own aliases (a Remix app maps ``~/*`` to
    ``./app/*`` in apps/web/tsconfig.json), so reading only the root tsconfig
    left every aliased import in a workspace package unresolved. Targets are
    joined as ``<tsconfig dir>/<baseUrl>/<target>``. The root tsconfig.json is
    also read straight from disk in case json files were excluded from the
    scan. ``extends`` chains are not followed.
    """
    configs = {rel for rel in rel_paths if PurePosixPath(rel).name == "tsconfig.json"}
    if (root / "tsconfig.json").exists():
        configs.add("tsconfig.json")
    aliases: list[tuple[str, str, tuple[str, ...]]] = []
    for rel in sorted(configs):
        try:
            data = json.loads((root / rel).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        compiler = data.get("compilerOptions", {}) if isinstance(data, dict) else {}
        if not isinstance(compiler, dict):
            continue
        base_url = compiler.get("baseUrl", ".")
        if not isinstance(base_url, str):
            base_url = "."
        paths = compiler.get("paths", {})
        if not isinstance(paths, dict):
            continue
        scope = PurePosixPath(rel).parent.as_posix()
        if scope == ".":
            scope = ""
        for pattern, targets in sorted(paths.items()):
            if not (isinstance(pattern, str) and isinstance(targets, list)):
                continue
            normalized_targets = []
            for target in targets:
                if isinstance(target, str):
                    joined = PurePosixPath(scope).joinpath(base_url, target)
                    normalized_targets.append(posixpath.normpath(joined.as_posix()))
            if normalized_targets:
                aliases.append((scope, pattern, tuple(normalized_targets)))
    # Longest scope first: candidates from the nearest tsconfig are tried
    # before the root's, so the nearest match wins.
    aliases.sort(key=lambda item: (-len(item[0]), item[0], item[1]))
    return tuple(aliases)


def _match_alias(pattern: str, import_name: str) -> str | None:
    if "*" not in pattern:
        return "" if pattern == import_name else None
    prefix, suffix = pattern.split("*", 1)
    if not import_name.startswith(prefix) or not import_name.endswith(suffix):
        return None
    return import_name[len(prefix): len(import_name) - len(suffix) if suffix else len(import_name)]
