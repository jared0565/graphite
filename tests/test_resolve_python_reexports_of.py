"""Which names one module binds only by a top-level `from X import name` (#87, #107).

What `python_reexports_of` returns is what a call of `module.name` is followed
through, so a name the module binds any other way as well must not be in it.
"""
from __future__ import annotations

from graphite.resolve import python_reexports_of


def _names(source: str) -> dict[str, tuple[str, int, str]]:
    return {local: hop for (_, local), hop in python_reexports_of("pkg/front.py", source.encode()).items()}


def test_a_plain_from_import_is_recorded() -> None:
    assert _names("from pkg.impl import target\n") == {"target": ("pkg.impl", 0, "target")}


def test_a_relative_and_aliased_import_is_recorded_as_written() -> None:
    assert _names("from .impl import target as aim\n") == {"aim": ("impl", 1, "target")}


def test_a_name_a_function_rebinds_under_global_is_not_recorded() -> None:
    source = "from pkg.impl import target\n\n\ndef swap(new):\n    global target\n    target = new\n"

    assert _names(source) == {}


def test_a_name_a_method_rebinds_under_global_is_not_recorded() -> None:
    source = (
        "from pkg.impl import target, other\n\n\n"
        "class Switch:\n    def flip(self, new):\n        global target\n        target = new\n"
    )

    assert _names(source) == {"other": ("pkg.impl", 0, "other")}


def test_a_name_bound_twice_is_not_recorded() -> None:
    source = "from pkg.impl import target\n\ntry:\n    from pkg.fast import target\nexcept ImportError:\n    pass\n"

    assert _names(source) == {}


def test_a_name_the_module_also_assigns_or_defines_is_not_recorded() -> None:
    assert _names("from pkg.impl import target\n\ntarget = wrap(target)\n") == {}
    assert _names("from pkg.impl import target\n\n\ndef target():\n    return 0\n") == {}


def test_an_import_under_a_condition_is_not_recorded() -> None:
    assert _names("import typing\n\nif typing.TYPE_CHECKING:\n    from pkg.impl import Target\n") == {}


def test_a_star_import_records_nothing_of_its_own() -> None:
    assert _names("from pkg.impl import *\nfrom pkg.util import helper\n") == {"helper": ("pkg.util", 0, "helper")}


def test_a_module_that_does_not_parse_records_nothing() -> None:
    assert _names("from pkg.impl import target\ndef broken(:\n") == {}
