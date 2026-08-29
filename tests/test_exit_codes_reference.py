"""Every subcommand the parser knows has a row in docs/reference/exit-codes.md."""
from __future__ import annotations

import argparse
from pathlib import Path

from graphite.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "reference" / "exit-codes.md"


def _top_level_subcommands() -> list[str]:
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    seen: set[int] = set()
    names: list[str] = []
    for name, child in sub.choices.items():
        if id(child) in seen:
            continue  # aliases share the parser; the first name is canonical
        seen.add(id(child))
        names.append(name)
    return names


def test_every_subcommand_has_a_row() -> None:
    text = DOC.read_text(encoding="utf-8")
    missing = [name for name in _top_level_subcommands() if f"`{name}" not in text]
    assert not missing, f"subcommands without an exit-code row: {missing}"


def test_the_two_universal_codes_are_stated() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "`2`" in text and "Usage error" in text
    assert "`1`" in text and "GRAPHITE_DEBUG" in text
    assert "`75`" in text  # the daemon child's lock refusal, the one code above 6
