"""TypeScript compiler-backed resolver tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.ingest import collect_files
from graphite.resolve import SourceIndex


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _compiler_index_or_skip(tmp_path: Path) -> tuple[SourceIndex, list[object], Config]:
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite", typescript_resolver="auto")
    entries = collect_files(tmp_path, cfg)
    source_index = SourceIndex.from_entries(entries, cfg)
    if not source_index.typescript.available:
        pytest.skip(f"TypeScript compiler bridge unavailable: {source_index.typescript.reason}")
    return source_index, entries, cfg


def test_typescript_compiler_resolves_alias_barrel_exports_and_dynamic_imports(tmp_path: Path) -> None:
    _write(
        tmp_path / "tsconfig.json",
        json.dumps(
            {
                "compilerOptions": {
                    "target": "ES2022",
                    "module": "ESNext",
                    "moduleResolution": "Bundler",
                    "baseUrl": ".",
                    "paths": {
                        "@lib": ["src/lib/index.ts"],
                        "@lib/*": ["src/lib/*"],
                    },
                }
            }
        ),
    )
    _write(tmp_path / "src" / "lib" / "foo.ts", "export function foo() { return 1; }\n")
    _write(tmp_path / "src" / "lib" / "index.ts", "export { foo } from './foo';\n")
    _write(tmp_path / "src" / "feature.ts", "export const feature = 1;\n")
    _write(
        tmp_path / "src" / "app.ts",
        "import { foo } from '@lib';\nexport async function app() { await import('./feature'); return foo(); }\n",
    )

    source_index, entries, cfg = _compiler_index_or_skip(tmp_path)

    resolved = source_index.resolve_ts_import_detail("src/app.ts", "@lib")
    assert resolved is not None
    assert resolved.rel_path == "src/lib/index.ts"
    assert resolved.confidence == "TS_COMPILER_IMPORT"

    result = extract_all(entries, cfg)
    edges = {(e["source"], e["target"], e["relation"], e.get("confidence")) for e in result.edges}

    assert ("src_app_ts", "lib_index", "imports", "TS_COMPILER_IMPORT") in edges
    assert ("lib_index", "lib_foo", "exports", "TS_COMPILER_EXPORT") in edges
    assert ("src_app_ts", "src_feature_ts", "imports", "TS_COMPILER_DYNAMIC_IMPORT") in edges


def test_typescript_resolver_can_be_disabled_for_heuristic_fallback(tmp_path: Path) -> None:
    _write(
        tmp_path / "tsconfig.json",
        json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"@lib/*": ["src/lib/*"]}}}),
    )
    _write(tmp_path / "src" / "lib" / "foo.ts", "export const foo = 1;\n")
    _write(tmp_path / "src" / "app.ts", "import { foo } from '@lib/foo';\nconsole.log(foo);\n")

    cfg = Config(workers=1, typescript_resolver="disabled", cache_dir=tmp_path / ".cache" / "graphite")
    entries = collect_files(tmp_path, cfg)
    source_index = SourceIndex.from_entries(entries, cfg)

    assert source_index.typescript.available is False
    resolved = source_index.resolve_ts_import_detail("src/app.ts", "@lib/foo")
    assert resolved is not None
    assert resolved.rel_path == "src/lib/foo.ts"
    assert resolved.confidence == "EXACT_IMPORT"

    result = extract_all(entries, cfg)
    imports = {(e["source"], e["target"], e["relation"], e.get("confidence")) for e in result.edges if e["relation"] == "imports"}
    assert ("src_app_ts", "src_lib_foo_ts", "imports", "EXACT_IMPORT") in imports


def test_heuristic_resolves_js_extension_import_to_ts_source(tmp_path: Path) -> None:
    """NodeNext/ESM: a relative import written with a ``.js`` extension must
    resolve to the ``.ts`` source of the same stem on the heuristic (no-compiler)
    path. Regression for the ``_candidate_paths`` bug that bailed on any suffix,
    so ``import '../db/queries.js'`` never tried ``queries.ts`` and produced no
    edge (real-world: worker code that imports with explicit ``.js`` extensions).
    """
    _write(tmp_path / "src" / "db" / "queries.ts", "export const q = 1;\n")
    _write(
        tmp_path / "src" / "routes" / "invites.ts",
        "import { q } from '../db/queries.js';\nconsole.log(q);\n",
    )

    cfg = Config(workers=1, typescript_resolver="disabled", cache_dir=tmp_path / ".cache" / "graphite")
    entries = collect_files(tmp_path, cfg)
    source_index = SourceIndex.from_entries(entries, cfg)

    assert source_index.typescript.available is False
    resolved = source_index.resolve_ts_import_detail("src/routes/invites.ts", "../db/queries.js")
    assert resolved is not None
    assert resolved.rel_path == "src/db/queries.ts"
    assert resolved.confidence == "EXACT_IMPORT"

    result = extract_all(entries, cfg)
    imports = {(e["source"], e["target"], e["relation"], e.get("confidence")) for e in result.edges if e["relation"] == "imports"}
    assert ("src_routes_invites_ts", "src_db_queries_ts", "imports", "EXACT_IMPORT") in imports


def test_heuristic_prefers_ts_source_over_js_twin_for_js_extension_import(tmp_path: Path) -> None:
    """When both a ``.ts`` source and a compiled ``.js`` twin exist, a ``.js``
    specifier must resolve to the ``.ts`` source (matches TypeScript, and avoids
    pointing edges at build artefacts). Guards the .ts-before-literal ordering.
    """
    _write(tmp_path / "src" / "util.ts", "export const u = 1;\n")
    _write(tmp_path / "src" / "util.js", "export const u = 1;\n")
    _write(tmp_path / "src" / "app.ts", "import { u } from './util.js';\nconsole.log(u);\n")

    cfg = Config(workers=1, typescript_resolver="disabled", cache_dir=tmp_path / ".cache" / "graphite")
    entries = collect_files(tmp_path, cfg)
    source_index = SourceIndex.from_entries(entries, cfg)

    assert source_index.typescript.available is False
    resolved = source_index.resolve_ts_import_detail("src/app.ts", "./util.js")
    assert resolved is not None
    assert resolved.rel_path == "src/util.ts"


def test_heuristic_resolves_alias_from_package_level_tsconfig(tmp_path: Path) -> None:
    """Monorepo packages declare aliases in their OWN tsconfig.json (e.g. a
    Remix app maps ``~/*`` to ``./app/*`` in apps/web/tsconfig.json). Regression
    for ``_load_tsconfig_aliases`` only ever reading the repo-root tsconfig, so
    every ``~/``-aliased import in a workspace package produced no edge.
    """
    _write(
        tmp_path / "apps" / "web" / "tsconfig.json",
        json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"~/*": ["./app/*"]}}}),
    )
    _write(tmp_path / "apps" / "web" / "app" / "lib" / "useModal.ts", "export const useModal = 1;\n")
    _write(
        tmp_path / "apps" / "web" / "app" / "components" / "review.ts",
        "import { useModal } from '~/lib/useModal';\nconsole.log(useModal);\n",
    )

    cfg = Config(workers=1, typescript_resolver="disabled", cache_dir=tmp_path / ".cache" / "graphite")
    entries = collect_files(tmp_path, cfg)
    source_index = SourceIndex.from_entries(entries, cfg)

    assert source_index.typescript.available is False
    resolved = source_index.resolve_ts_import_detail(
        "apps/web/app/components/review.ts", "~/lib/useModal"
    )
    assert resolved is not None
    assert resolved.rel_path == "apps/web/app/lib/useModal.ts"
    assert resolved.confidence == "EXACT_IMPORT"

    result = extract_all(entries, cfg)
    imports = {(e["source"], e["target"], e["relation"], e.get("confidence")) for e in result.edges if e["relation"] == "imports"}
    assert (
        "apps_web_app_components_review_ts",
        "apps_web_app_lib_usemodal_ts_5991d0",
        "imports",
        "EXACT_IMPORT",
    ) in imports


def test_package_level_alias_is_scoped_to_its_directory(tmp_path: Path) -> None:
    """An alias declared in apps/web/tsconfig.json must not apply to files in
    OTHER packages: a ``~/`` import from apps/worker has no alias and must stay
    unresolved rather than pointing into apps/web.
    """
    _write(
        tmp_path / "apps" / "web" / "tsconfig.json",
        json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"~/*": ["./app/*"]}}}),
    )
    _write(tmp_path / "apps" / "web" / "app" / "lib" / "useModal.ts", "export const useModal = 1;\n")
    _write(
        tmp_path / "apps" / "worker" / "src" / "app.ts",
        "import { useModal } from '~/lib/useModal';\nconsole.log(useModal);\n",
    )

    cfg = Config(workers=1, typescript_resolver="disabled", cache_dir=tmp_path / ".cache" / "graphite")
    entries = collect_files(tmp_path, cfg)
    source_index = SourceIndex.from_entries(entries, cfg)

    assert source_index.typescript.available is False
    assert source_index.resolve_ts_import_detail("apps/worker/src/app.ts", "~/lib/useModal") is None


def test_nearest_tsconfig_alias_wins_over_root(tmp_path: Path) -> None:
    """When the root and a package tsconfig both define the same pattern, a
    file inside the package resolves through its own (nearest) tsconfig.
    """
    _write(
        tmp_path / "tsconfig.json",
        json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"~/*": ["./rootlib/*"]}}}),
    )
    _write(
        tmp_path / "apps" / "web" / "tsconfig.json",
        json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"~/*": ["./app/*"]}}}),
    )
    _write(tmp_path / "rootlib" / "thing.ts", "export const thing = 'root';\n")
    _write(tmp_path / "apps" / "web" / "app" / "thing.ts", "export const thing = 'web';\n")
    _write(
        tmp_path / "apps" / "web" / "app" / "page.ts",
        "import { thing } from '~/thing';\nconsole.log(thing);\n",
    )

    cfg = Config(workers=1, typescript_resolver="disabled", cache_dir=tmp_path / ".cache" / "graphite")
    entries = collect_files(tmp_path, cfg)
    source_index = SourceIndex.from_entries(entries, cfg)

    assert source_index.typescript.available is False
    resolved = source_index.resolve_ts_import_detail("apps/web/app/page.ts", "~/thing")
    assert resolved is not None
    assert resolved.rel_path == "apps/web/app/thing.ts"


def test_typescript_compiler_adds_file_level_symbol_reference_edges(tmp_path: Path) -> None:
    _write(
        tmp_path / "tsconfig.json",
        json.dumps({"compilerOptions": {"target": "ES2022", "module": "ESNext", "moduleResolution": "Bundler"}}),
    )
    _write(
        tmp_path / "src" / "domain.ts",
        "export interface User { id: string; }\nexport function normalize(user: User) { return user.id; }\n",
    )
    _write(
        tmp_path / "src" / "app.ts",
        "import type { User } from './domain';\nimport { normalize } from './domain';\nexport function run(user: User) { return normalize(user); }\n",
    )

    _, entries, cfg = _compiler_index_or_skip(tmp_path)
    result = extract_all(entries, cfg)
    edges = {(e["source"], e["target"], e["relation"], e.get("confidence")) for e in result.edges}

    assert ("src_app_ts", "src_domain_ts", "references", "TS_COMPILER_SYMBOL_REFERENCE") in edges
    assert ("src_app_ts", "src_domain_ts", "type_references", "TS_COMPILER_TYPE_REFERENCE") in edges


def test_typescript_symbol_reference_edges_can_be_disabled(tmp_path: Path) -> None:
    _write(
        tmp_path / "tsconfig.json",
        json.dumps({"compilerOptions": {"target": "ES2022", "module": "ESNext", "moduleResolution": "Bundler"}}),
    )
    _write(tmp_path / "src" / "domain.ts", "export function normalize() { return 1; }\n")
    _write(tmp_path / "src" / "app.ts", "import { normalize } from './domain';\nexport const value = normalize();\n")

    cfg = Config(
        workers=1,
        cache_dir=tmp_path / ".cache" / "graphite",
        typescript_resolver="auto",
        typescript_symbol_references=False,
    )
    entries = collect_files(tmp_path, cfg)
    source_index = SourceIndex.from_entries(entries, cfg)
    if not source_index.typescript.available:
        pytest.skip(f"TypeScript compiler bridge unavailable: {source_index.typescript.reason}")

    result = extract_all(entries, cfg)

    assert not any(e["relation"] == "references" for e in result.edges)

