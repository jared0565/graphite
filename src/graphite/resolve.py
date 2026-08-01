"""Source resolution helpers for Graphite extraction."""
from __future__ import annotations

import hashlib
import json
import posixpath
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Final, Iterable

from .config import Config
from .ts_bridge import TypeScriptCompilerEdge, TypeScriptCompilerIndex, build_typescript_index


@lru_cache(maxsize=8)
def _rel_path_set_digest(rel_paths: frozenset[str]) -> str:
    """Short, order-independent digest of a set of repo-relative paths.

    Memoized on the frozenset because it is consulted once per extracted file
    and the set is fixed for a build; recomputing it per file would be
    quadratic in the repo size.
    """
    digest = hashlib.blake2b(digest_size=8)
    for rel in sorted(rel_paths):
        digest.update(rel.encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return digest.hexdigest()

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
class RustUseResolution:
    """Outcome of resolving one Rust `use` path.

    Three distinct states, and the difference matters to resolution_health:
    bound (`rel_path` set), confirmed external, or unresolved. Only a
    *confirmed* external may be tagged EXTERNAL_IMPORT; an unresolved target
    must keep default confidence so it stays counted against the ratio.
    """

    rel_path: str | None
    external: bool


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
    # (crate ident, crate dir rel path, src root rel path) per Cargo.toml in the
    # scan, so `use other_crate::…` binds to that crate's source instead of a
    # phantom, and declared dependencies can be told apart from typos.
    cargo_crates: tuple[tuple[str, str, str], ...] = ()
    # Union across all manifests. FALLBACK ONLY, for a file no manifest claims.
    cargo_dependencies: frozenset[str] = frozenset()
    # (crate dir, declared idents) per manifest -- the authoritative answer to
    # "did THIS file's crate declare that dependency". See issue #38.
    cargo_crate_dependencies: tuple[tuple[str, frozenset[str]], ...] = ()

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
            cargo_crates=_load_cargo_crates(root, frozenset(rel_paths)),
            cargo_dependencies=_load_cargo_dependencies(root, frozenset(rel_paths)),
            cargo_crate_dependencies=_load_cargo_crate_dependencies(root, frozenset(rel_paths)),
        )

    def file_set_digest(self) -> str:
        """Stable digest of the repo's file set.

        Import resolution runs at extraction time, so a cached extraction
        embeds conclusions about which sibling modules existed *then*. Folding
        this into the cache key makes adding or removing a file re-resolve the
        importers that never changed themselves (#2).
        """
        return _rel_path_set_digest(self.rel_paths)

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

    def resolve_python_module(
        self, importer_rel_path: str, module: str, relative_dots: int = 0
    ) -> str | None:
        """Lexically resolve a Python module path to a repo rel_path, else None.

        Absolute: roots "" then "src"; relative: anchored at the importer's
        directory, one level up per dot beyond the first. Purely repo-relative:
        no sys.path, so a repo file shadowing a stdlib name wins.
        """
        parts = [p for p in module.split(".") if p] if module else []
        candidates: list[str] = []
        if relative_dots > 0:
            anchor = PurePosixPath(importer_rel_path).parent
            for _ in range(relative_dots - 1):
                anchor = anchor.parent
            base = anchor.joinpath(*parts).as_posix() if parts else anchor.as_posix()
            if parts:
                candidates.extend(_python_module_candidates(base))
            else:
                candidates.append(posixpath.normpath(f"{base}/__init__.py"))
        else:
            if not parts:
                return None
            joined = "/".join(parts)
            for root in ("", "src"):
                base = f"{root}/{joined}" if root else joined
                candidates.extend(_python_module_candidates(base))
        for candidate in candidates:
            normalized = posixpath.normpath(candidate).lstrip("./")
            if normalized in self.rel_paths:
                return normalized
        return None

    def _rust_crate_for(self, rel_path: str) -> tuple[str, str, str] | None:
        """Innermost crate containing this file (longest matching crate dir).

        Falls back to the nearest `src/` ancestor when no manifest matches, so
        `crate::` still resolves in a repo whose Cargo.toml was not scanned.
        Without this, crate-relative imports silently fail to bind.
        """
        best: tuple[str, str, str] | None = None
        for entry in self.cargo_crates:
            crate_dir = entry[1]
            if crate_dir and not rel_path.startswith(f"{crate_dir}/"):
                continue
            if best is None or len(crate_dir) > len(best[1]):
                best = entry
        if best is not None:
            return best
        parts = PurePosixPath(rel_path).parts
        if "src" in parts:
            index = len(parts) - 1 - parts[::-1].index("src")
            return ("", "/".join(parts[:index]), "/".join(parts[: index + 1]))
        return None

    def _rust_walk(self, anchor: str, rest: list[str]) -> str | None:
        """Longest-module-prefix-first search for a file under `anchor`."""
        modules = _rust_module_segments(rest)
        for depth in range(len(modules), -1, -1):
            parts = modules[:depth]
            if parts:
                joined = "/".join(parts)
                base = f"{anchor}/{joined}" if anchor else joined
                candidates = _rust_module_candidates(base)
            else:
                candidates = [
                    f"{anchor}/{name}" if anchor else name
                    for name in ("lib.rs", "mod.rs", "main.rs")
                ]
            for candidate in candidates:
                normalized = posixpath.normpath(candidate).lstrip("./")
                if normalized in self.rel_paths:
                    return normalized
        return None

    def resolve_rust_mod(self, importer_rel_path: str, mod_name: str) -> str | None:
        """Resolve a `mod foo;` declaration to the file it pulls in.

        Unambiguous, unlike `use`: exactly two legal layouts. This is Rust's
        file-inclusion mechanism and the only structural link between the files
        of a multi-file crate.
        """
        if not mod_name:
            return None
        base_dir = _rust_module_dir(importer_rel_path)
        base = f"{base_dir}/{mod_name}" if base_dir else mod_name
        for candidate in _rust_module_candidates(base):
            normalized = posixpath.normpath(candidate).lstrip("./")
            if normalized in self.rel_paths:
                return normalized
        return None

    def _rust_declares(self, importer_rel_path: str, name: str) -> bool:
        """Did the importing file's own crate declare this dependency?

        Asking per-crate rather than repo-wide is issue #38: an import tagged
        external is excluded from the resolution-health ratio, so letting a
        sibling crate's dependency vouch for an undeclared import removed a real
        miss from the denominator instead of counting it.

        Falls back to the union when no manifest claims the file, so a repo whose
        Cargo.toml was never scanned keeps resolving exactly as before rather
        than regressing to "everything unresolved".
        """
        crate = self._rust_crate_for(importer_rel_path)
        if crate is not None:
            for crate_dir, declared in self.cargo_crate_dependencies:
                if crate_dir == crate[1]:
                    return name in declared
        return name in self.cargo_dependencies

    def resolve_rust_use(
        self, importer_rel_path: str, use_path: str, inline_mod_depth: int = 0
    ) -> RustUseResolution:
        """Lexically resolve a Rust `use` path. See RustUseResolution."""
        segments = [s for s in use_path.replace(" ", "").split("::") if s]
        if not segments:
            return _RUST_UNRESOLVED
        head, rest = segments[0], segments[1:]
        if head in _RUST_EXTERNAL_ROOTS:
            return _RUST_EXTERNAL

        if head == "crate":
            crate = self._rust_crate_for(importer_rel_path)
            if crate is None:
                return _RUST_UNRESOLVED
            anchor = crate[2]
        elif head == "self":
            anchor = _rust_module_dir(importer_rel_path)
        elif head == "super":
            if inline_mod_depth > 0:
                # `super` from inside an inline `mod` block is still this file.
                return RustUseResolution(importer_rel_path, False)
            parent_raw = PurePosixPath(_rust_module_dir(importer_rel_path)).parent.as_posix()
            anchor = "" if parent_raw == "." else parent_raw
        else:
            target = next((c for c in self.cargo_crates if c[0] == head), None)
            if target is None:
                declared = self._rust_declares(importer_rel_path, head)
                return _RUST_EXTERNAL if declared else _RUST_UNRESOLVED
            return RustUseResolution(self._rust_walk(target[2], rest), False)

        return RustUseResolution(self._rust_walk(anchor, rest), False)


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


def _python_module_candidates(base: str) -> list[str]:
    normalized = posixpath.normpath(base)
    if normalized in (".", ""):
        return ["__init__.py"]
    return [f"{normalized}.py", f"{normalized}/__init__.py"]


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


# --- Rust ------------------------------------------------------------------
# Roots that always leave the repo. Kept deliberately small: EXTERNAL_IMPORT
# means "a resolver confirmed this is external", never "we failed to resolve
# it" -- tagging misses as external would hide real phantom linkage from
# resolution_health.
_RUST_EXTERNAL_ROOTS: Final = frozenset({"std", "core", "alloc", "proc_macro"})
_CARGO_DEPENDENCY_TABLES: Final = ("dependencies", "dev-dependencies", "build-dependencies")
_RUST_MODULE_ROOT_FILES: Final = frozenset({"lib.rs", "main.rs", "mod.rs"})
_RUST_UNRESOLVED: Final = RustUseResolution(None, False)
_RUST_EXTERNAL: Final = RustUseResolution(None, True)


def _rust_module_dir(rel_path: str) -> str:
    """Directory Rust searches for this file's child modules.

    `lib.rs`/`main.rs`/`mod.rs` are their module's root, so their children sit
    beside them. Any other `foo.rs` owns a sibling directory `foo/`.
    """
    path = PurePosixPath(rel_path)
    parent_raw = path.parent.as_posix()
    parent = "" if parent_raw == "." else parent_raw
    if path.name in _RUST_MODULE_ROOT_FILES:
        return parent
    return f"{parent}/{path.stem}" if parent else path.stem


def _rust_module_candidates(base: str) -> list[str]:
    """The two legal on-disk layouts for a Rust module."""
    return [f"{base}.rs", f"{base}/mod.rs"]


def _rust_module_segments(segments: list[str]) -> list[str]:
    """Leading segments of a `use` path that can be modules.

    A `use` path blends module path and item name with no lexical boundary.
    Rust modules are snake_case and items CamelCase, so an uppercase initial
    ends the module portion.
    """
    modules: list[str] = []
    for segment in segments:
        if segment[:1].isupper():
            break
        modules.append(segment)
    return modules


def _normalize_crate_name(raw: str) -> str:
    """Cargo maps a package name's hyphens to underscores for the crate ident."""
    return raw.strip().replace("-", "_")


def _read_cargo_manifest(root: Path, rel: str) -> dict[str, object] | None:
    """Parse one Cargo.toml. Total: unreadable or malformed yields None.

    Extraction must never fail because a repo ships a bad manifest.
    """
    try:
        return tomllib.loads((root / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


def _cargo_manifest_paths(rel_paths: frozenset[str]) -> list[str]:
    return sorted(rel for rel in rel_paths if PurePosixPath(rel).name == "Cargo.toml")


def _load_cargo_crates(root: Path, rel_paths: frozenset[str]) -> tuple[tuple[str, str, str], ...]:
    """(crate ident, crate dir, src root) per real package manifest in the scan."""
    crates: list[tuple[str, str, str]] = []
    for rel in _cargo_manifest_paths(rel_paths):
        data = _read_cargo_manifest(root, rel)
        if data is None:
            continue
        package = data.get("package")
        if not isinstance(package, dict):
            continue  # virtual/workspace-only manifest declares no crate
        name = package.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        parent = PurePosixPath(rel).parent.as_posix()
        crate_dir = "" if parent == "." else parent
        src_root = f"{crate_dir}/src" if crate_dir else "src"
        crates.append((_normalize_crate_name(name), crate_dir, src_root))
    return tuple(crates)


def _dependency_names(scope: object) -> set[str]:
    """Dependency idents declared in one manifest scope."""
    names: set[str] = set()
    if not isinstance(scope, dict):
        return names
    for table in _CARGO_DEPENDENCY_TABLES:
        section = scope.get(table)
        if isinstance(section, dict):
            names.update(_normalize_crate_name(key) for key in section if isinstance(key, str))
    return names


def _load_cargo_dependencies(root: Path, rel_paths: frozenset[str]) -> frozenset[str]:
    """Union of declared dependency idents across every manifest.

    Kept only as the fallback for a file that cannot be attributed to a crate.
    Do NOT use it to decide whether a specific file's import is declared -- see
    `_load_cargo_crate_dependencies` and issue #38.
    """
    names: set[str] = set()
    for rel in _cargo_manifest_paths(rel_paths):
        data = _read_cargo_manifest(root, rel)
        if data is None:
            continue
        names |= _dependency_names(data)
        names |= _dependency_names(data.get("workspace"))
    return frozenset(names)


def _load_cargo_crate_dependencies(
    root: Path, rel_paths: frozenset[str]
) -> tuple[tuple[str, frozenset[str]], ...]:
    """Declared dependency idents **per crate**, as (crate dir, idents).

    Issue #38: the union above let one crate's dependency vouch for another
    crate's undeclared import. Since an import classified external is EXCLUDED
    from the resolution-health denominator, that quietly removed a broken import
    from the ratio instead of counting it against -- inflating the number strict
    graph-first enforcement gates on.

    Workspace-level `[workspace.dependencies]` IS folded into every crate's set.
    `dep = { workspace = true }` is legitimate inheritance, and those
    declarations are shared by design; only cross-crate leakage is the defect.
    The cost is that a member using a workspace dependency it never opted into
    still reads as external -- narrower than the union, and deliberate.
    """
    workspace: set[str] = set()
    per_crate: list[tuple[str, set[str]]] = []
    for rel in _cargo_manifest_paths(rel_paths):
        data = _read_cargo_manifest(root, rel)
        if data is None:
            continue
        workspace |= _dependency_names(data.get("workspace"))
        parent = PurePosixPath(rel).parent.as_posix()
        per_crate.append(("" if parent == "." else parent, _dependency_names(data)))
    return tuple((crate_dir, frozenset(names | workspace)) for crate_dir, names in per_crate)
