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


def test_cli_reference_carries_no_machine_path(monkeypatch) -> None:  # noqa: ANN001 - pytest fixture
    """The page is committed, so a default that came from the environment
    would publish one machine's layout as if it were the tool's. Measured:
    the first cut rendered `F:\\Projects` and failed on every CI leg. The
    rendering must not change with GRAPHITE_PROJECTS_ROOT, and must never
    contain an absolute path."""
    import os
    import re

    text = DOC.read_text(encoding="utf-8")
    for needle in (str(ROOT), str(ROOT.parent), str(Path.home()), os.environ.get("GRAPHITE_PROJECTS_ROOT") or "\x00"):
        assert needle not in text, f"machine path {needle!r} leaked into cli.md"
    assert not re.search(r"default: `(?:[A-Za-z]:\\|/)", text), "an absolute-path default leaked into cli.md"
    monkeypatch.setenv("GRAPHITE_PROJECTS_ROOT", "Z:\\somewhere-else")
    with_env = render_reference()
    monkeypatch.delenv("GRAPHITE_PROJECTS_ROOT")
    without_env = render_reference()
    assert with_env == without_env == text


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
