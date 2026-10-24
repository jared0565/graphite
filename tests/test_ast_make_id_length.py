"""`_make_id` keeps an id of exactly `_MAX_ID_LEN` characters as written.

The length bound was held only from above (`len(id) <= _MAX_ID_LEN`), and an id
that is shortened and given a discriminator also fits under it. So reading the
bound as `<` passed the whole suite (aramid 25d393fc) while it renamed every
canonical id of exactly the maximum length, and an id is what `graph.json` and
every edge are keyed on. These pin the edge from both sides.
"""
from __future__ import annotations

from graphite.extract.ast import _MAX_ID_LEN, _make_id


def test_an_id_of_exactly_the_maximum_length_is_kept_as_written() -> None:
    name = "a" * _MAX_ID_LEN

    assert _make_id(name) == name


def test_an_id_split_across_parts_at_the_maximum_length_is_kept_as_written() -> None:
    file_id = "src_graphite_config"
    symbol = "b" * (_MAX_ID_LEN - len(file_id) - 1)

    assert _make_id(file_id, symbol) == f"{file_id}_{symbol}"


def test_an_id_one_over_the_maximum_is_shortened_and_discriminated() -> None:
    name = "a" * (_MAX_ID_LEN + 1)

    made = _make_id(name)

    assert len(made) == _MAX_ID_LEN
    assert not made.startswith("a" * (_MAX_ID_LEN - 1))
