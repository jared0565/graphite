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


def _nested_groups() -> dict[str, dict[str, object]]:
    """{group name: {nested name: nested parser}} for every top-level
    subcommand that is itself a group (route, lifecycle, channel, incidents,
    overlay), aliases collapsed onto their first name."""
    import argparse

    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    groups: dict[str, dict[str, object]] = {}
    seen_top: set[int] = set()
    for name, child in sub.choices.items():
        if id(child) in seen_top:
            continue
        seen_top.add(id(child))
        nested = next((a for a in child._actions if isinstance(a, argparse._SubParsersAction)), None)
        if nested is None:
            continue
        seen_nested: set[int] = set()
        for nested_name, nested_child in nested.choices.items():
            if id(nested_child) in seen_nested:
                continue
            seen_nested.add(id(nested_child))
            groups.setdefault(name, {})[nested_name] = nested_child
    return groups


def test_cli_reference_renders_nested_subcommands() -> None:
    """A group's real commands live one level down (`graphite route
    reconcile`, `graphite incidents ack`). The first cut of this page walked
    only the top level, so twenty-four commands and every option they own --
    `route reconcile --attempt-id` among them -- were absent, and the
    lockstep test could not see them drift. Every nested command gets its
    own section under its group, with its options."""
    import argparse

    text = DOC.read_text(encoding="utf-8")
    groups = _nested_groups()
    assert {"route", "lifecycle", "incidents", "overlay"} <= set(groups), sorted(groups)
    for group, nested in groups.items():
        for name, child in nested.items():
            heading = f"#### `graphite {group} {name}`"
            assert heading in text, f"{heading} has no section"
            section = text.split(heading, 1)[1].split("\n### ", 1)[0].split("\n#### ", 1)[0]
            for action in child._actions:  # type: ignore[attr-defined]
                if isinstance(action, argparse._SubParsersAction) or action.help == argparse.SUPPRESS:
                    continue
                for opt in action.option_strings:
                    assert f"`{opt}`" in section, f"{group} {name}: option {opt} missing from its section"
    assert "`--attempt-id`" in text


def test_cli_reference_renders_argument_choices() -> None:
    """`graphite channel` dispatches on a positional with `choices`, not a
    subparser, so recursion cannot reach `report`/`list`/`show`/`register`;
    every argument that restricts its values must list them, or the page
    documents an argument whose accepted values are unknowable from it."""
    import argparse

    text = DOC.read_text(encoding="utf-8")
    parser = build_parser()

    def walk(node: argparse.ArgumentParser, prefix: str) -> None:
        for action in node._actions:
            if isinstance(action, argparse._SubParsersAction):
                seen: set[int] = set()
                for name, child in action.choices.items():
                    if id(child) in seen:
                        continue
                    seen.add(id(child))
                    walk(child, f"{prefix}{name} ")
            elif action.choices and action.help != argparse.SUPPRESS:
                if prefix:
                    heading = f"`graphite {prefix.rstrip()}`"
                    assert heading in text, f"{heading} has no section"
                    section = text.split(heading, 1)[1].split("\n### ", 1)[0].split("\n#### ", 1)[0]
                else:
                    section = text.split("## Global options", 1)[1].split("\n## ", 1)[0]
                for choice in action.choices:
                    assert f"`{choice}`" in section, f"graphite {prefix}{action.dest}: choice {choice!r} not listed"

    walk(parser, "")
    channel = text.split("### `graphite channel`", 1)[1].split("\n### ", 1)[0]
    for choice in ("report", "list", "show", "register"):
        assert f"`{choice}`" in channel


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
