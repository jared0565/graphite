"""Answer-scoped confidence contract (spec 2026-07-26).

Every canonical graph answer carries an `answer` block: the relations the
verb walked, the languages in scope, per-relation per-language health
cells, a derived grade, applicable caveat codes, and — when the primary
result is empty — what the emptiness means.

Fail-open: build_answer_block returns None on any internal failure and
callers omit the key; the block may be dropped, never wrong.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

import networkx as nx

from .health import RESOLUTION_HEALTHY_RATIO, _edge_language, resolution_health

ANSWER_SCHEMA = 1

GRADE_DECISION = "decision_grade"
GRADE_ADVISORY = "advisory"
GRADE_INCONCLUSIVE = "inconclusive"

# Confirmed blindspot classes. Process rule (spec §5): a confirmed class
# gets an entry the day it is confirmed, decoupled from its fix; fixed
# classes get retired_by and are never emitted again.
CAVEAT_REGISTRY: tuple[dict[str, Any], ...] = (
    {
        "code": "python-dynamic-dispatch",
        "relations": ("calls",),
        "languages": ("python",),
        "summary": "dynamically dispatched calls (getattr, decorator rebinding) are not modeled",
        "since": "2026-07-26",
    },
    {
        "code": "ts-external-calls-unclassified",
        "relations": ("calls",),
        "languages": ("typescript", "javascript"),
        "summary": "calls to external-package symbols, runtime globals, and destructured locals count as unbound",
        "since": "2026-07-26",
        "retired_by": "2026-07-27",
    },
    {
        "code": "ts-destructured-locals-unbound",
        "relations": ("calls",),
        "languages": ("typescript", "javascript"),
        "summary": "calls through destructured local bindings (const { f } = require(...)) count as unbound",
        "since": "2026-07-27",
    },
    {
        "code": "calls-unattributable-receiver-false-external",
        "relations": ("calls",),
        "languages": ("typescript", "javascript", "python"),
        "summary": "a call whose receiver is not a simple identifier is classified by its bare method name and may be wrongly excluded from the ratio as external",
        "since": "2026-07-27",
        # Fixed by #14: an unattributable receiver is no longer classified at
        # all, so it can no longer be excused from the ratio. The entry keeps
        # its original summary -- a published code's meaning never changes.
        "retired_by": "2026-07-27",
    },
)


def active_caveats() -> list[dict[str, Any]]:
    """Registry entries that are live (no retired_by), full published shape."""
    return [dict(e) for e in CAVEAT_REGISTRY if not e.get("retired_by")]


INCONCLUSIVE_EMPTY = "none found — INCONCLUSIVE: treat as unverified and confirm with grep"


def is_degraded(block: dict[str, Any] | None) -> bool:
    """True when any scoped health cell in an answer block is below threshold."""
    if not block:
        return False
    return any(
        not cell.get("healthy", True)
        for langs in block.get("health", {}).values()
        for cell in langs.values()
    )


def is_unmeasured(block: dict[str, Any] | None) -> bool:
    """True when an answer block exists but carries no scoped health cells.

    Distinct from a fail-open `None`: the block was built, but nothing was
    measured for the relations x languages the answer used. An empty listing
    under it is unverified, not a trustworthy absence (#12). A `None` block is
    the fail-open path and stays permissive.
    """
    if not block:
        return False
    return not any(langs for langs in (block.get("health") or {}).values())


def empty_marker(block: dict[str, Any] | None) -> str:
    """Empty-listing text for an answer surface, scoped to the answer's grade.

    A degraded-and-empty listing is `inconclusive` by this contract's own
    definition, even when the answer as a whole graded `advisory` because its
    other half was non-empty. See spec §5. An unmeasured block gets the same
    treatment: zero cells is zero evidence, so a bare "none found" would claim
    an absence nothing verified.
    """
    return INCONCLUSIVE_EMPTY if (is_degraded(block) or is_unmeasured(block)) else "none found"


def languages_for_nodes(g: nx.DiGraph, node_ids: Iterable[str]) -> list[str]:
    """Sorted unique languages of the nodes' source files ('other' dropped)."""
    langs: set[str] = set()
    for node_id in node_ids:
        if node_id is None or node_id not in g:
            continue
        language = _edge_language(g.nodes[node_id].get("source_file"))
        if language != "other":
            langs.add(language)
    return sorted(langs)


def build_answer_block(
    g: nx.DiGraph,
    *,
    relations: Sequence[str],
    languages: Sequence[str] | None,
    total: int,
    empty_meaning: str | None = None,
) -> dict[str, Any] | None:
    """The `answer` block for one graph answer, or None (fail-open).

    ``languages=None`` means "no filter, grade against every language in the
    graph" -- distinct from ``languages=()``/``[]``, which means the caller
    computed the matched nodes' languages and found none apply (e.g. the
    matched nodes are markdown/config files, not code). The two must not
    collapse to the same branch: no real caller passes ``None`` today (every
    call site derives its filter from the matched nodes via
    ``languages_for_nodes``), so treating an empty list as "no filter" meant
    a query about a non-code file silently graded against the WHOLE graph's
    unrelated-language health instead of having nothing to grade at all
    (found via operation-firewall dogfooding, 2026-07-31: a `README.md`
    query inherited Rust's and Python's degraded health and came back
    inconclusive, polluting the incident ledger over a file that structurally
    has no calls or imports). When no language applies, there is nothing to
    grade -- return None (fail-open), the same as the no-relations case just
    above, rather than a spuriously degraded or inconclusive block.
    """
    try:
        if not relations:
            return None
        if languages is not None and not languages:
            return None
        health = resolution_health(g)
        by_language = health.get("by_language") or {}
        threshold = health.get("threshold", RESOLUTION_HEALTHY_RATIO)
        langs = sorted(languages) if languages is not None else sorted(by_language)
        cells: dict[str, dict[str, dict[str, Any]]] = {}
        degraded = False
        for relation in relations:
            for language in langs:
                cell = (by_language.get(language) or {}).get(relation)
                if not cell or cell.get("ratio") is None:
                    continue
                healthy = cell["ratio"] >= threshold
                degraded = degraded or not healthy
                cells.setdefault(relation, {})[language] = {**cell, "healthy": healthy}
        empty = total == 0
        # No cells at all means nothing was measured for the relations x
        # languages this answer actually used. That is not evidence of health;
        # it is the absence of evidence, so it cannot grade decision_grade (#12).
        unmeasured = not cells
        if degraded or unmeasured:
            grade = GRADE_INCONCLUSIVE if empty else GRADE_ADVISORY
        else:
            grade = GRADE_DECISION
        relation_set = set(relations)
        language_set = set(langs)
        caveats = [
            {"code": e["code"], "summary": e["summary"]}
            for e in active_caveats()
            if relation_set.intersection(e["relations"])
            and language_set.intersection(e["languages"])
        ]
        block: dict[str, Any] = {
            "schema": ANSWER_SCHEMA,
            "relations": sorted(relation_set),
            "languages": langs,
            "health": cells,
            "grade": grade,
            "caveats": caveats,
        }
        if empty and empty_meaning:
            block["empty_meaning"] = empty_meaning
        return block
    except Exception:
        return None
