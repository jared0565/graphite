"""Every human-output listing surface, its kind, and its marker.

Adding a listing surface means adding a row here. A surface with no row is
not covered by the contract, which is the failure mode this round removed.
See docs/superpowers/specs/2026-07-26-listing-marker-contract-design.md §4.
"""

import re

import pytest

from graphite import cli, context, daemon_health

MARKER = re.compile(r"\.\.\. \d+ more")

# (id, kind) -- kinds are ANSWER, COUNT_IN_SUMMARY, CONDITIONAL (spec §4)
SURFACES = [
    ("cli.cmd_impact", "ANSWER"),
    ("cli.cmd_daemon_status", "COUNT_IN_SUMMARY"),
    ("cli.cmd_validate", "COUNT_IN_SUMMARY"),
    ("cli._print_watch_impact", "CONDITIONAL"),
    ("context.impacted_files", "ANSWER"),
    ("context.likely_tests", "ANSWER"),
    ("daemon_health._issue_lines", "CONDITIONAL"),
]

# context._append_neighbor_section is deliberately absent: its cap is a
# data-layer defect that also affects `context --json`, tracked as issue #11
# and out of this round's renderer-only charter (spec §7.1).


def test_every_surface_declares_a_known_kind():
    assert {kind for _, kind in SURFACES} <= {"ANSWER", "COUNT_IN_SUMMARY", "CONDITIONAL"}
    assert len(SURFACES) == len({name for name, _ in SURFACES})


def test_named_render_entrypoints_still_exist():
    """Guards against a rename silently invalidating the list below -- a
    hand-maintained duplicate of part of the table above, not the table
    itself. (The table-vs-source-code reconciliation lives in
    test_every_listing_lines_call_site_is_accounted_for.)

    Only the rows that name a real module attribute are checked; the
    context.* rows name listing sites inside format_context_markdown, which
    has no per-site attribute to resolve.
    """
    for module, attribute in [
        (cli, "cmd_impact"),
        (cli, "cmd_daemon_status"),
        (cli, "cmd_validate"),
        (cli, "_print_watch_impact"),
        (context, "format_context_markdown"),
        (daemon_health, "_issue_lines"),
    ]:
        assert callable(getattr(module, attribute)), f"{module.__name__}.{attribute}"


def test_caps_are_named_constants_not_inline_slices():
    """Checks the constants' values only -- not that each call site actually
    references the constant rather than an inline [:N], which is how every
    defect in this round was written. That requires reading the call sites
    themselves (see test_every_listing_lines_call_site_is_accounted_for),
    not just that a same-named constant exists with the right value."""
    assert cli._DAEMON_STATUS_PROJECT_CAP == 20
    assert cli._VALIDATE_ERROR_CAP == 10
    assert cli._WATCH_IMPACTED_CAP == 20
    assert cli._WATCH_TESTS_CAP == 30
    assert context._CONTEXT_LIST_CAP == 30
    assert daemon_health._MAX_ISSUE_LINES == 20


@pytest.mark.parametrize(
    "cap_value,total",
    [(20, 26), (10, 15), (30, 41)],
)
def test_listing_lines_marker_shape_is_uniform(cap_value, total):
    """Whatever the surface, the marker reads the same and reconciles."""
    from graphite.listing import listing_lines

    lines = listing_lines([f"v{i}" for i in range(total)], cap=cap_value)
    marker = lines[-1]

    assert MARKER.search(marker)
    shown = sum(1 for line in lines if line.lstrip().startswith("- "))
    dropped = int(re.search(r"(\d+) more", marker).group(1))
    assert shown + dropped == total


def test_answer_surfaces_emit_their_header_when_empty():
    """ANSWER-kind surfaces answer every half of the question they were asked."""
    from graphite.listing import listing_lines

    assert listing_lines([], header="Likely tests:", empty="none found") == [
        "Likely tests:",
        "  - none found",
    ]


def test_non_answer_surfaces_emit_nothing_when_empty():
    from graphite.listing import listing_lines

    assert listing_lines([], header="[graphite] likely tests:", empty=None) == []


def test_context_likely_tests_cap_is_marked():
    """Coverage gap flagged in Task 6's review: site 6 (context.py's
    likely-tests listing, line 179) is the only capped call site with no
    test discriminating its cap from an uncapped one. The generic
    ``test_listing_lines_marker_shape_is_uniform`` above exercises
    ``listing_lines`` directly and would keep passing even if the call site
    stopped passing ``cap=_CONTEXT_LIST_CAP`` -- it never calls
    ``format_context_markdown``. This test fails if that cap is dropped.
    """
    from graphite.context import format_context_markdown

    ctx = {
        "metadata": {"node_count": 2, "edge_count": 1, "density": 0.5},
        "inputs": ["src/a.py"],
        "matched": [],
        "missing": [],
        "depth": 2,
        "direct_dependencies": {},
        "direct_dependents": {},
        "impact": {
            "impacted_files": [],
            "likely_tests": [f"src/t{i}.test.py" for i in range(35)],
            "missing": [],
        },
        "communities": {},
        "risk": [],
        "resolution_health": {"healthy": True},
        "inconclusive": False,
        "answer": {"grade": "decision_grade", "health": {}, "caveats": []},
    }

    text = format_context_markdown(ctx)

    assert "... 5 more" in text


def test_every_listing_lines_call_site_is_accounted_for():
    """Discovers every real ``listing_lines(...)`` call in ``src/graphite``
    via AST -- not a textual grep, which cannot reliably distinguish a call
    from a comment or docstring mention -- and reconciles the total against
    SURFACES. This is what makes the module docstring's claim ("a surface
    with no row is not covered by the contract") mechanically true for
    listing_lines()-based surfaces: without this test, nothing reconciled
    the table against the actual call sites, so a contributor could add a
    ninth call -- inside an existing surface's function or a brand new one
    -- and never touch SURFACES without breaking a single test.

    The per-surface expected count is >1 for two rows because
    ``cli.cmd_impact`` and ``cli._print_watch_impact`` each render two
    listing halves (impacted files, likely tests) from one function, so one
    SURFACES row covers two call sites there; ``context.py``'s two halves
    live in the same function (``format_context_markdown``) but get two
    separate SURFACES rows instead, since it has no per-half attribute to
    name. Raw call-site count and len(SURFACES) are therefore genuinely
    different numbers (8 calls, 7 rows) -- this test reconciles the two via
    the explicit mapping below rather than asserting a naive equality that
    would be wrong on day one.

    ``daemon_health._issue_lines`` does not call ``listing_lines`` at all --
    it hand-rolls its own marker (spec §7 row 8) -- so it is declared here
    with an expected count of zero. A brand new *hand-rolled* marker
    elsewhere in the codebase would not be caught by this test; only
    listing_lines()-based surfaces are mechanically discoverable this way.
    """
    import ast
    from pathlib import Path

    import graphite

    src_dir = Path(graphite.__file__).resolve().parent
    discovered = 0
    for path in src_dir.rglob("*.py"):
        if path.name == "listing.py":
            continue  # def listing_lines itself lives here, not a call site
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "listing_lines":
                discovered += 1

    expected_calls_per_surface = {
        "cli.cmd_impact": 2,  # impacted_files half + likely_tests half
        "cli.cmd_daemon_status": 1,
        "cli.cmd_validate": 1,
        "cli._print_watch_impact": 2,  # impacted_files half + likely_tests half
        "context.impacted_files": 1,
        "context.likely_tests": 1,
        "daemon_health._issue_lines": 0,  # hand-rolled -- see docstring above
    }

    assert set(expected_calls_per_surface) == {name for name, _ in SURFACES}, (
        "expected_calls_per_surface and SURFACES have drifted apart -- "
        "update both together"
    )
    expected_total = sum(expected_calls_per_surface.values())
    assert discovered == expected_total, (
        f"found {discovered} listing_lines(...) call sites in src/graphite "
        f"but SURFACES accounts for {expected_total} -- a listing surface "
        "was added or removed without updating SURFACES"
    )
