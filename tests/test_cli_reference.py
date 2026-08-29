"""docs/reference/cli.md is generated from the parser; a stale copy fails."""
from __future__ import annotations

from pathlib import Path

from graphite.cli import build_parser
from scripts.gen_cli_reference import render_reference

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "reference" / "cli.md"


def test_cli_reference_is_current() -> None:
    expected = render_reference()
    actual = DOC.read_text(encoding="utf-8")
    assert actual == expected, "docs/reference/cli.md is stale: run python scripts/gen_cli_reference.py"


def test_cli_reference_names_every_subcommand() -> None:
    text = DOC.read_text(encoding="utf-8")
    parser = build_parser()
    import argparse

    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    listed: set[int] = set()
    for name, child in sub.choices.items():
        if id(child) in listed:
            assert f"`{name}`" in text, f"alias {name} is not mentioned"
            continue
        listed.add(id(child))
        assert f"### `graphite {name}`" in text, f"subcommand {name} has no section"


def test_build_parser_is_the_parser_main_uses(capsys) -> None:  # noqa: ANN001 - pytest fixture
    """`main()` must dispatch through the same parser the reference renders,
    or the document describes a parser nobody runs."""
    from graphite.cli import main

    parser = build_parser()
    assert parser.prog == "graphite"
    try:
        main(["capabilities", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "usage: graphite capabilities" in out
