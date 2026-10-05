"""Python scope identity (#70, #71): one node per definition, bare names bound the
way CPython resolves them.

Spec: docs/superpowers/specs/2026-10-04-python-scope-identity-design.md.
"""
from __future__ import annotations

from graphite.extract.ast import _MAX_ID_LEN, ExtractionResult, _edge, _make_id, _merge, _scoped_id


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


def test_a_scoped_id_always_carries_a_discriminator():
    assert _scoped_id("m_py", "Worker.run") == "m_py_worker_run_7c6b1a"


def test_a_nested_definition_never_shares_a_module_definitions_id():
    """`_make_id` folds `.` into `_` as lossless: the trap `_scoped_id` exists to avoid."""
    assert _make_id("m_py", "outer.inner") == _make_id("m_py", "outer_inner") == "m_py_outer_inner"
    assert _scoped_id("m_py", "outer.inner") == "m_py_outer_inner_b0dec9"


def test_scoped_ids_differ_by_file_and_by_qualname():
    assert _scoped_id("a_py", "K.run") != _scoped_id("b_py", "K.run")
    assert _scoped_id("m_py", "test_one.fake_build") == "m_py_test_one_fake_build_4e414e"
    assert _scoped_id("m_py", "test_two.fake_build") == "m_py_test_two_fake_build_1dc280"


def test_a_scoped_id_is_capped_at_the_id_length_limit():
    longest = _scoped_id("m_py", "a" * 300)
    assert len(longest) == _MAX_ID_LEN
    assert longest != _scoped_id("m_py", "a" * 299)


def test_the_local_phantom_is_not_a_definitions_id():
    """`<` is no identifier character, so no definition's qualname hashes to this marker."""
    assert _scoped_id("m_py", "<local>.generate") == "m_py_local_generate_1c9caf"
    assert _scoped_id("m_py", "<local>.generate") != _scoped_id("m_py", "local.generate")
