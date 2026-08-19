"""Distinct definitions must get distinct node ids (#57, #58).

`_make_id` normalised away information that distinguishes real symbols, and
`_file_node_id` discarded the file extension. Both produced the same silent
failure: two definitions collapse to one node, the loser vanishes from the graph
entirely, and every edge naming either lands on whichever survived. Nothing in
the answer signals it -- a contaminated `callers` list grades exactly like a
clean one, and an empty one for the vanished name reads as a trustworthy absence.

Measured on graphite's own sources before the fix:

  * 5 same-file symbol collisions, 6 definitions absent. `routing/storage.py`
    defines `initialize` at L950 and `_initialize` at L3965; the graph kept
    `initialize`, and `self._initialize()` was recorded as a call to it.
  * 3 file-id collisions, the worst merging `src/graphite/init.py` (791 lines)
    into `src/graphite/__init__.py` (41 lines) -- so the larger module had no
    file node at all and its 21 symbols hung off the smaller one's id.

The two mechanisms are independent and both are pinned here, because fixing
either alone leaves the other's collisions in place.
"""
from __future__ import annotations

import pytest

from graphite.extract.ast import _file_node_id, _make_id


# --- symbol segment: sanitisation must not merge distinct names ---------------


@pytest.mark.parametrize(
    "a,b",
    [
        ("path", "_path"),          # leading underscore, stripped
        ("path", "Path"),           # case, folded
        ("_Service", "_service"),   # both, and neither is canonical
        ("initialize", "_initialize"),
        ("digest", "_digest"),
        ("a_b", "a__b"),            # interior run, collapsed by `_+ -> _`
        ("x", "__x__"),             # dunder
    ],
)
def test_two_distinct_names_in_one_file_get_distinct_ids(a: str, b: str) -> None:
    """The five lossy operations in `_make_id`, one pair each.

    `strip("_.")` per part, `[^\\w]+ -> _`, `_+ -> _`, `casefold()`, and
    truncation. Note that removing the strip does NOT separate `path`/`_path`:
    the `_+` collapse two lines below puts them back together. Only a
    discriminator made of lowercase hex survives both that collapse and the
    casefold.
    """
    file_id = "src_graphite_config"
    assert _make_id(file_id, a) != _make_id(file_id, b)


def test_a_canonical_name_keeps_its_plain_id() -> None:
    """The discriminator is for ambiguity, not decoration.

    Without this the fix could pass every collision test by hashing everything,
    at the cost of ids no human can read and a far larger migration.
    """
    assert _make_id("src_graphite_config", "resolve") == "src_graphite_config_resolve"
    assert _make_id("src_app_index_ts", "handler") == "src_app_index_ts_handler"


def test_the_discriminator_survives_the_operations_that_caused_the_collisions() -> None:
    """A marker made of anything but lowercase hex would be eaten by the pipeline.

    `_+ -> _` collapses underscore runs and `casefold()` flattens case, so a
    marker containing either would be re-merged by the very code it exists to
    defeat. This asserts the property rather than the implementation: whatever
    distinguishes two colliding ids must itself be stable under both.
    """
    file_id = "src_graphite_config"
    a, b = _make_id(file_id, "path"), _make_id(file_id, "_path")
    for value in (a, b):
        import re

        assert re.sub(r"_+", "_", value) == value, "id contains a collapsible underscore run"
        assert value.casefold() == value, "id is not casefold-stable"


# --- file segment: the extension is part of a file's identity -----------------


@pytest.mark.parametrize(
    "a,b",
    [
        ("src/app/index.ts", "src/app/index.js"),      # the TypeScript case
        ("src/ui/Button.tsx", "src/ui/Button.css"),    # component + its stylesheet
        ("src/m.py", "src/m.pyi"),
        (".githooks/post-commit", ".githooks/post-commit.local"),
        ("ARAMID.md", "aramid.toml"),                  # extension AND case
        ("src/graphite/init.py", "src/graphite/__init__.py"),
    ],
)
def test_two_files_get_distinct_ids(a: str, b: str) -> None:
    """`Path.stem` discarded the suffix, so a stylesheet could claim a
    component's id. The last pair is the one `.name` alone does NOT fix --
    `__init__` still loses its underscores to the strip and the collapse, so it
    needs the symbol-segment discriminator too. Both mechanisms, one test.
    """
    assert _file_node_id(a) != _file_node_id(b)


def test_a_files_symbols_are_namespaced_under_that_file() -> None:
    """The regression guard for the whole point of a file-id prefix.

    `src/graphite/init.py` had 21 symbols hanging off `__init__.py`'s node. If
    file ids ever stop being distinct, this is what silently stops being true.
    """
    init_py = _file_node_id("src/graphite/init.py")
    dunder = _file_node_id("src/graphite/__init__.py")
    assert _make_id(init_py, "write_docs") != _make_id(dunder, "write_docs")


def test_ids_stay_within_the_length_bound() -> None:
    """A discriminator must fit inside the budget, not push the id past it."""
    from graphite.extract.ast import _MAX_ID_LEN

    long_name = "_" + "a" * 400
    assert len(_make_id("src_graphite_config", long_name)) <= _MAX_ID_LEN
    assert len(_make_id("x" * 400)) <= _MAX_ID_LEN


def test_two_long_names_that_truncate_alike_stay_distinct() -> None:
    """Truncation is the fifth lossy operation and collides silently."""
    file_id = "src_graphite_config"
    base = "handler_for_the_extremely_long_and_descriptive_operation_name_number_"
    a = _make_id(file_id, base + "one" + "x" * 200)
    b = _make_id(file_id, base + "two" + "x" * 200)
    assert a != b


def test_ids_are_deterministic_across_calls() -> None:
    """Ids are written into graph.json and compared across builds."""
    assert _make_id("src_graphite_config", "_path") == _make_id("src_graphite_config", "_path")
    assert _file_node_id("src/app/index.ts") == _file_node_id("src/app/index.ts")
