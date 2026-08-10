"""Known debt, with the interval that actually matters attached.

The standing target for this project was never "zero known issues" -- that bar
would have failed the most productive sessions it has had, because finding a
real defect makes the count go up. What matters is how long something stays
KNOWN-BUT-UNDECLARED, and after that, how long a declared blind spot stays open.

`CAVEAT_REGISTRY` already carries both endpoints: `since` is the day a blindspot
class was confirmed, `retired_by` the day it stopped being emitted. Nothing read
them as a series, so the interval existed and was never looked at.

Determinism is the design constraint. A report whose output changes with the
wall clock cannot be asserted on, so `as_of` is a parameter rather than a call
to `date.today()` -- the same reason workflow scripts here are forbidden
`Date.now()`.
"""
from __future__ import annotations

from datetime import date

import pytest

from graphite.debt import debt_report


AS_OF = date(2026, 8, 10)


def test_an_open_blindspot_reports_how_long_it_has_been_open() -> None:
    report = debt_report(as_of=AS_OF)

    dynamic = next(c for c in report["open"] if c["code"] == "python-dynamic-dispatch")

    assert dynamic["since"] == "2026-07-26"
    assert dynamic["age_days"] == 15, "2026-07-26 to 2026-08-10 is 15 days"
    assert dynamic["summary"], "a code alone does not tell a reader what is unmodelled"


def test_a_retired_blindspot_reports_declaration_to_fix_latency() -> None:
    """The historical series. This is the number that says whether declaring a
    blind spot leads anywhere, as opposed to being a place to file and forget."""
    report = debt_report(as_of=AS_OF)

    retired = {c["code"]: c for c in report["retired"]}

    assert retired["ts-external-calls-unclassified"]["latency_days"] == 1
    assert retired["calls-unattributable-receiver-false-external"]["latency_days"] == 0


def test_a_retired_blindspot_is_never_also_open() -> None:
    """Falsifiability guard. Without it every assertion above is satisfied by a
    report that lists the whole registry twice, and the open count -- the one
    number a reader acts on -- would never fall."""
    report = debt_report(as_of=AS_OF)

    open_codes = {c["code"] for c in report["open"]}
    retired_codes = {c["code"] for c in report["retired"]}

    assert not (open_codes & retired_codes)
    assert retired_codes, "fixture registry must contain retired entries to discriminate"


def test_the_summary_names_the_oldest_open_item() -> None:
    """A count says how much; the oldest says what to look at."""
    report = debt_report(as_of=AS_OF)

    summary = report["summary"]

    assert summary["open_count"] == len(report["open"])
    assert summary["oldest_open_days"] == max(c["age_days"] for c in report["open"])
    assert summary["oldest_open_code"] == "python-dynamic-dispatch"


def test_the_report_is_deterministic_for_a_given_as_of() -> None:
    """It is compared across runs and across repos, so the same inputs have to
    render identically -- including ordering."""
    assert debt_report(as_of=AS_OF) == debt_report(as_of=AS_OF)


def test_open_items_are_ordered_oldest_first() -> None:
    ages = [c["age_days"] for c in debt_report(as_of=AS_OF)["open"]]

    assert ages == sorted(ages, reverse=True)


def test_a_future_as_of_does_not_produce_negative_ages() -> None:
    """`as_of` is operator input, so it can be wrong. A negative age would read
    as a fresh item and quietly sort to the bottom."""
    report = debt_report(as_of=date(2020, 1, 1))

    assert all(c["age_days"] >= 0 for c in report["open"])


def test_the_registry_is_the_only_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins that this reads the live registry rather than a copied list.

    A hand-maintained duplicate would drift the day someone adds a caveat and
    forgets this file -- and a debt report that under-reports is worse than
    none, because it is read as reassurance.
    """
    import graphite.debt as debt_module

    fake = (
        {"code": "invented", "summary": "s", "since": "2026-01-01", "relations": (), "languages": ()},
    )
    monkeypatch.setattr(debt_module, "CAVEAT_REGISTRY", fake)

    report = debt_report(as_of=date(2026, 1, 11))

    assert [c["code"] for c in report["open"]] == ["invented"]
    assert report["open"][0]["age_days"] == 10


def test_the_cli_renders_the_report(capsys) -> None:
    from graphite.cli import main

    assert main(["debt", "--as-of", "2026-08-10"]) == 0
    out = capsys.readouterr().out

    assert "python-dynamic-dispatch" in out
    assert "15d" in out, "the age is the reason this command exists"
    assert "declaration -> fix" in out, "the retired series must be visible too"


def test_the_cli_does_not_mark_the_repository_active(monkeypatch, capsys) -> None:
    """Asking how much debt exists must not enrol the directory you asked from.

    Same failure as a version survey that activates eight repos: the
    measurement changes the thing it measures, and the daemon starts
    supervising whatever the operator happened to `cd` into.
    """
    from graphite import activation
    from graphite.cli import main

    calls: list[object] = []
    monkeypatch.setattr(activation, "mark_active", lambda *a, **k: calls.append(a))

    assert main(["debt", "--as-of", "2026-08-10"]) == 0
    capsys.readouterr()

    assert calls == [], "debt marked a repository active"


def test_as_of_defaults_to_today_without_crashing(capsys) -> None:
    """The flag exists for reproducibility, not because the command needs it."""
    from graphite.cli import main

    assert main(["debt"]) == 0
    assert "declared debt as of" in capsys.readouterr().out
