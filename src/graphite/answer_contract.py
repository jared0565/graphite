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
        "code": "python-callback-registration",
        "relations": ("calls",),
        "languages": ("python",),
        "summary": (
            "a function passed as a VALUE to a registrar (threading.Timer, atexit.register, "
            "Thread(target=), signal.signal) produces no call edge, so an empty callers "
            "result may be wrong -- confirm with grep before treating it as dead"
        ),
        "since": "2026-08-06",
        # The first CONDITIONAL caveat. Every other entry is a scope
        # disclosure -- true of the relation x language, regardless of what
        # this particular answer found -- which means it cannot discriminate a
        # sound answer from an unsound one. aramid measured
        # `python-dynamic-dispatch` byte-identical across six `callers`
        # queries, five with non-zero counts (round 55). An always-on caveat
        # trains readers to ignore caveats, so a hedge that is meant to be
        # ACTED on has to be absent when it does not apply.
        "only_when_empty": True,
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
        "code": "js-require-emits-no-import-edge",
        "relations": ("imports",),
        "languages": ("javascript", "typescript"),
        "summary": (
            "a CommonJS require() is a call expression, not an import statement, so it "
            "emits NO import edge at all -- an empty imported-by or depends-on result "
            "may be wrong, and the imports ratio cannot see the omission"
        ),
        "since": "2026-08-10",
        # Declared the day it was measured, decoupled from the fix. This is the
        # first entry whose relation is `imports`, and it is why `imports`
        # joined NON_DETECTION_RELATIONS: a two-file fixture requiring one
        # module TWICE produced exactly one import edge (the unrelated ESM
        # one), a 1.0 imports ratio, and an empty `imported-by` at
        # decision_grade. A missing SITE cannot lower a ratio computed over
        # sites, so the metric graded its own blind spot healthy.
    },
    {
        "code": "js-module-object-calls-unbound",
        "relations": ("calls",),
        "languages": ("javascript", "typescript"),
        "summary": (
            "calls through a module object (const m = require('./x'); m.f(), or "
            "import * as ns from './x'; ns.f()) are not bound to the target"
        ),
        "since": "2026-08-10",
        # Distinct from `ts-destructured-locals-unbound`, which covers only
        # `const { f } = require(...)`. A published code's meaning never
        # changes, so the member-access shape gets its own entry rather than a
        # widened summary on that one. Python already binds this shape via
        # `alias_map` (extract/ast.py); JavaScript has no equivalent.
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


#: Relations where a real edge can be MISSING ENTIRELY rather than merely
#: unresolved, so an empty answer cannot be a trustworthy absence.
#:
#: `resolution_health` is a RESOLUTION metric, not a COVERAGE one: it measures
#: how many detected sites bound to a target. An invocation that produces no
#: site at all -- a function passed as a value to `threading.Timer`,
#: `atexit.register`, `Thread(target=)` -- never enters `total`, so it cannot
#: lower the ratio. A perfect 1.0 is therefore compatible with an entire class
#: of invocation being unmodelled, and grading an empty answer `decision_grade`
#: on the strength of that ratio reads the denominator as if it excluded
#: nothing. Measured: a file with two ordinary calls and two callback
#: registrations scored `total 2, bound 2, ratio 1.0` while `callers` on both
#: registered functions returned 0 at decision_grade (aramid, round 55).
#:
#: `imports` USED to be excluded here, on the reasoning that "an import is a
#: syntactic construct that extraction either sees or does not". CommonJS
#: falsifies that: `require('./mod')` is a call expression, so the import
#: extractor never sees it and no candidate edge is emitted. Measured on a
#: two-file fixture where `consumer.js` requires `./mod` TWICE -- the graph held
#: exactly one import edge (the unrelated ESM one), the imports cell read
#: `total 1, bound 1, ratio 1.0`, and `imported-by src/mod.js` answered nothing
#: at decision_grade. The rule that governed this entry was "add a relation only
#: with a measured non-detection case, not on suspicion", and that is now met.
#:
#: Scoped BY LANGUAGE rather than added outright. `None` means every language;
#: a frozenset restricts it. Rust `use` and Go imports have no dynamic form
#: graphite models, so their absences are still evidence and must not be
#: downgraded to buy a fix for JavaScript.
NON_DETECTION_RELATIONS: dict[str, frozenset[str] | None] = {
    "calls": None,
    "imports": frozenset({"javascript", "typescript"}),
}

#: Why an absence in this relation is not proof. Reaches the human listing, so
#: it names the construct a reader can go and grep for.
NON_DETECTION_REASONS = {
    "calls": "a callback-registered caller emits no edge",
    "imports": "a CommonJS `require()` emits no import edge",
}


def non_detecting_relations(
    relations: Iterable[str], languages: Iterable[str]
) -> list[str]:
    """Relations whose absence cannot be trusted for these languages."""
    language_set = set(languages)
    return sorted(
        relation
        for relation in set(relations)
        if relation in NON_DETECTION_RELATIONS
        and (
            NON_DETECTION_RELATIONS[relation] is None
            or language_set & NON_DETECTION_RELATIONS[relation]
        )
    )


def active_caveats() -> list[dict[str, Any]]:
    """Registry entries that are live (no retired_by), full published shape."""
    return [dict(e) for e in CAVEAT_REGISTRY if not e.get("retired_by")]


INCONCLUSIVE_EMPTY = "none found — INCONCLUSIVE: treat as unverified and confirm with grep"

#: Empty listing under a HEALTHY answer whose absence still cannot be trusted:
#: the cells are measured and fine, but the relation has a known non-detection
#: class (see NON_DETECTION_RELATIONS), so "none" may simply be unmodelled.
#:
#: Distinct wording from INCONCLUSIVE_EMPTY on purpose -- the two say different
#: things and collapsing them would misreport a good measurement as a failed
#: one. Distinct from a bare "none found" for the reason round 55 exists: a
#: grade the human line does not echo is a hedge the reader never sees.
def unverified_empty(reason: str) -> str:
    """The UNVERIFIED listing line for one non-detection reason."""
    return (
        f"none found — UNVERIFIED: {reason}, "
        "so this absence is not proof; confirm with grep"
    )


#: The `calls` wording, kept as a module constant because it is the published
#: shape round 55 introduced and is asserted verbatim.
UNVERIFIED_EMPTY = unverified_empty(NON_DETECTION_REASONS["calls"])


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
    if is_degraded(block) or is_unmeasured(block):
        return INCONCLUSIVE_EMPTY
    # Healthy cells, but the grade says the absence is not evidence. Echo that
    # in the human line: a machine-readable grade the printed listing
    # contradicts is exactly the hedge a reader misses (round 55).
    if block and block.get("grade") == GRADE_ADVISORY:
        # Name the construct that went undetected, not just "unverified" -- the
        # reader's next action is a grep, and which one depends on whether the
        # missing edge is a callback registration or a `require()`.
        triggered = non_detecting_relations(
            block.get("relations", ()), block.get("languages", ())
        )
        if triggered:
            return unverified_empty(
                " and ".join(NON_DETECTION_REASONS[r] for r in triggered)
            )
        return UNVERIFIED_EMPTY
    return "none found"


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
        relation_set = set(relations)
        language_set = set(langs)
        # No cells at all means nothing was measured for the relations x
        # languages this answer actually used. That is not evidence of health;
        # it is the absence of evidence, so it cannot grade decision_grade (#12).
        unmeasured = not cells
        # An empty answer over a relation with a known non-detection class is
        # NOT a trustworthy absence, however healthy the ratio -- the ratio is
        # blind to the edges that were never emitted. See
        # NON_DETECTION_RELATIONS. Advisory rather than inconclusive on
        # purpose: the health genuinely IS good, so calling it inconclusive
        # would misreport a measurement. What is unsupported is the absence,
        # and `advisory` already means "use it, and verify".
        undetectable_absence = empty and bool(
            non_detecting_relations(relation_set, language_set)
        )
        if degraded or unmeasured:
            grade = GRADE_INCONCLUSIVE if empty else GRADE_ADVISORY
        elif undetectable_absence:
            grade = GRADE_ADVISORY
        else:
            grade = GRADE_DECISION
        caveats = [
            {"code": e["code"], "summary": e["summary"]}
            for e in active_caveats()
            if relation_set.intersection(e["relations"])
            and language_set.intersection(e["languages"])
            # A conditional caveat is emitted only where it applies, so its
            # presence carries signal a reader can act on.
            and (empty or not e.get("only_when_empty"))
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
