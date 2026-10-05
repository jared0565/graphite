"""Python scope identity (#70, #71): one node per definition, bare names bound the
way CPython resolves them.

Spec: docs/superpowers/specs/2026-10-04-python-scope-identity-design.md.
"""
from __future__ import annotations

from graphite.extract.ast import ExtractionResult, _edge, _merge


def test_merge_keeps_the_numerically_first_location():
    """As strings `L12` sorts before `L9`, so the surviving edge named a later site (§4.5)."""
    late = _edge("m_py_f", "m_py_g", "calls", "m.py", 12)
    early = _edge("m_py_f", "m_py_g", "calls", "m.py", 9)
    (edge,) = _merge([ExtractionResult(edges=[late, early])]).edges
    assert edge["source_location"] == "L9"
    assert edge["weight"] == 2.0


def test_merge_sorts_a_missing_location_after_every_line():
    unplaced = _edge("m_py_f", "m_py_g", "calls", "m.py")
    placed = _edge("m_py_f", "m_py_g", "calls", "m.py", 3)
    (edge,) = _merge([ExtractionResult(edges=[unplaced, placed])]).edges
    assert edge["source_location"] == "L3"
