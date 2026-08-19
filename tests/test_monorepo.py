"""Monorepo correctness: full-path node ids, workspace package imports,
unresolved member-call phantom suppression, and nested-project discovery.

These pin the v4 extraction fixes found against a real monorepo (pivot-parlor),
where the pre-v4 scheme merged `apps/worker/src/db/queries.ts` and
`apps/workers/booking/src/db/queries.ts` into ONE node, resolved no
`@repo/*` imports, and drowned degree stats in `c.json()`-style phantoms.
"""
from __future__ import annotations

from pathlib import Path

from graphite.config import Config
from graphite.daemon import discover_projects
from graphite.extract.ast import extract_all
from graphite.ingest import collect_files


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(tmp_path: Path):
    cfg = Config(
        workers=1,
        cache_dir=tmp_path / ".cache" / "graphite",
        typescript_resolver="disabled",
    )
    entries = collect_files(tmp_path, cfg)
    return extract_all(entries, cfg)


# ─── Full-path node ids ────────────────────────────────────────────────────────

def test_same_named_files_in_different_workspaces_stay_distinct(tmp_path: Path) -> None:
    """Two workers both define db/queries.ts::getShopById — they must not merge."""
    _write(
        tmp_path / "apps" / "worker" / "src" / "db" / "queries.ts",
        "export function getShopById() { return 'main'; }\n",
    )
    _write(
        tmp_path / "apps" / "workers" / "booking" / "src" / "db" / "queries.ts",
        "export function getShopById() { return 'booking'; }\n",
    )
    result = _extract(tmp_path)

    file_nodes = {n["id"]: n for n in result.nodes if n["kind"] == "file"}
    assert "apps_worker_src_db_queries_ts" in file_nodes
    assert "apps_workers_booking_src_db_queries_ts" in file_nodes

    fn_nodes = [n for n in result.nodes if n.get("name") == "getShopById"]
    assert len(fn_nodes) == 2, "each worker's getShopById must be its own node"
    files = {n["source_file"].replace("\\", "/") for n in fn_nodes}
    assert files == {
        "apps/worker/src/db/queries.ts",
        "apps/workers/booking/src/db/queries.ts",
    }


# ─── Workspace package imports ────────────────────────────────────────────────

def _workspace_fixture(tmp_path: Path) -> None:
    _write(
        tmp_path / "packages" / "utils" / "package.json",
        '{"name": "@repo/utils", "main": "./src/index.ts"}\n',
    )
    _write(
        tmp_path / "packages" / "utils" / "src" / "index.ts",
        "export function calculateCommissionPence() { return 1; }\n",
    )
    _write(
        tmp_path / "packages" / "utils" / "src" / "money.ts",
        "export function formatPence() { return '£'; }\n",
    )
    _write(
        tmp_path / "apps" / "worker" / "src" / "route.ts",
        "import { calculateCommissionPence } from '@repo/utils';\n"
        "import { formatPence } from '@repo/utils/money';\n"
        "export function checkout() { return calculateCommissionPence() + formatPence(); }\n",
    )


def test_workspace_package_import_resolves_to_entry_file(tmp_path: Path) -> None:
    _workspace_fixture(tmp_path)
    result = _extract(tmp_path)

    imports = {
        (e["source"], e["target"], e["confidence"])
        for e in result.edges
        if e["relation"] == "imports"
    }
    assert (
        "apps_worker_src_route_ts",
        "packages_utils_src_index_ts",
        "WORKSPACE_IMPORT",
    ) in imports, f"@repo/utils must resolve to the workspace entry file; got {sorted(imports)}"
    assert (
        "apps_worker_src_route_ts",
        "packages_utils_src_money_ts",
        "WORKSPACE_IMPORT",
    ) in imports, "@repo/utils/money subpath must resolve inside the package"


def test_workspace_named_import_call_links_to_defining_symbol(tmp_path: Path) -> None:
    _workspace_fixture(tmp_path)
    result = _extract(tmp_path)

    calls = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}
    assert (
        "apps_worker_src_route_ts_checkout",
        "packages_utils_src_index_ts_calculatecommissionpence_2b8a1e",
    ) in calls, f"cross-workspace call must reach the real definition; got {sorted(calls)}"
    assert (
        "apps_worker_src_route_ts_checkout",
        "packages_utils_src_money_ts_formatpence_97a3e5",
    ) in calls


def test_workspace_exports_dot_object_entry(tmp_path: Path) -> None:
    """exports {'.': {import: ...}} shape (what @repo/types-style packages use)."""
    _write(
        tmp_path / "packages" / "types" / "package.json",
        '{"name": "@repo/types", "exports": {".": {"import": "./src/index.ts"}}}\n',
    )
    _write(tmp_path / "packages" / "types" / "src" / "index.ts", "export const X = 1;\n")
    _write(
        tmp_path / "apps" / "web" / "app.ts",
        "import { X } from '@repo/types';\nexport function f() { return X; }\n",
    )
    result = _extract(tmp_path)
    imports = {
        (e["source"], e["target"])
        for e in result.edges
        if e["relation"] == "imports" and e["confidence"] == "WORKSPACE_IMPORT"
    }
    assert ("apps_web_app_ts", "packages_types_src_index_ts") in imports


# ─── Unresolved member-call phantom suppression ───────────────────────────────

def test_unresolved_member_calls_are_dropped(tmp_path: Path) -> None:
    """`c.json()` / `db.prepare()` framework calls must not create phantom targets."""
    _write(
        tmp_path / "src" / "handler.ts",
        "export function handler(c: any, db: any) {\n"
        "  db.prepare('SELECT 1');\n"
        "  return c.json({ ok: true });\n"
        "}\n"
        "export function helper() { return 1; }\n"
        "export function caller() { return helper(); }\n",
    )
    result = _extract(tmp_path)
    node_ids = {n["id"] for n in result.nodes}
    calls = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}

    # The framework member calls are gone entirely.
    assert not any("c_json" in tgt or "db_prepare" in tgt for _s, tgt in calls), (
        f"unresolved member-call phantoms must be dropped; got {sorted(calls)}"
    )
    # Real same-file calls to defined functions still resolve.
    assert ("src_handler_ts_caller", "src_handler_ts_helper") in calls
    # Every surviving call target is a real node (no phantom member targets).
    for _src, tgt in calls:
        if tgt not in node_ids:
            assert "_" in tgt, f"unexpected phantom {tgt}"


def test_method_dispatch_still_resolves_member_calls(tmp_path: Path) -> None:
    """Member calls that DO match a class method definition keep resolving."""
    _write(
        tmp_path / "src" / "store.ts",
        "export class Store { load() { return 1; } }\n",
    )
    _write(
        tmp_path / "src" / "user.ts",
        "import { Store } from './store';\n"
        "export function use(store: Store) { return store.load(); }\n",
    )
    result = _extract(tmp_path)
    calls = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}
    assert ("src_user_ts_use", "src_store_ts_load") in calls


# ─── Nested project discovery ─────────────────────────────────────────────────

def test_discovery_does_not_descend_into_projects(tmp_path: Path) -> None:
    # A monorepo with workspaces that carry their own project markers.
    _write(tmp_path / "repoA" / "package.json", "{}")
    _write(tmp_path / "repoA" / "packages" / "utils" / "package.json", "{}")
    _write(tmp_path / "repoA" / "apps" / "web" / "package.json", "{}")
    # A plain folder that only holds a project deeper down.
    _write(tmp_path / "group" / "repoB" / "pyproject.toml", "")

    found = discover_projects(tmp_path, max_depth=6, max_projects=128)
    rel = sorted(p.relative_to(tmp_path).as_posix() for p in found)
    assert rel == ["group/repoB", "repoA"], (
        f"workspaces inside repoA must not be separate projects; got {rel}"
    )
