"""Declared debt, with the interval that makes it actionable.

The target for this project was never "zero known issues". That bar punishes the
sessions that find real defects, and it is satisfiable by not looking. What
matters is the LATENCY: how long a blind spot stays known-but-undeclared, and
then how long a declared one stays open.

`CAVEAT_REGISTRY` already records both endpoints -- `since` is the day a
blindspot class was confirmed, `retired_by` the day it stopped being emitted,
and its process rule requires the entry on the day of confirmation, decoupled
from the fix. So the series existed and nothing read it.

Deliberately offline and deterministic, like every other canonical surface here:
`as_of` is a parameter rather than a call to `date.today()`, so the output can
be asserted on and compared across runs. GitHub issues are NOT consulted --
that would put a network call and a non-reproducible answer inside a diagnostic
whose whole value is being comparable.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .answer_contract import CAVEAT_REGISTRY

DEBT_SCHEMA = 1


def _parse(stamp: str) -> date:
    return date.fromisoformat(stamp)


def debt_report(*, as_of: date) -> dict[str, Any]:
    """Declared blind spots, open and retired, as of a given day."""
    open_items: list[dict[str, Any]] = []
    retired_items: list[dict[str, Any]] = []

    for entry in CAVEAT_REGISTRY:
        since = _parse(entry["since"])
        retired_by = entry.get("retired_by")
        if retired_by:
            retired_items.append(
                {
                    "code": entry["code"],
                    "summary": entry["summary"],
                    "since": entry["since"],
                    "retired_by": retired_by,
                    # Declaration -> fix. Zero is a real and good value: the
                    # process rule allows an entry and its fix on the same day.
                    "latency_days": (_parse(retired_by) - since).days,
                }
            )
            continue
        open_items.append(
            {
                "code": entry["code"],
                "summary": entry["summary"],
                "since": entry["since"],
                # Clamped at zero: `as_of` is operator input and can precede a
                # declaration. A negative age would read as a fresh item and
                # sort itself to the bottom of the list, which is the one place
                # nobody looks.
                "age_days": max(0, (as_of - since).days),
            }
        )

    # Oldest first: the list is read top-down, and the top is where attention
    # goes. Ties break on code so the ordering is total and the report is
    # comparable across runs.
    open_items.sort(key=lambda item: (-item["age_days"], item["code"]))
    retired_items.sort(key=lambda item: (item["retired_by"], item["code"]))

    latencies = sorted(item["latency_days"] for item in retired_items)
    summary = {
        "open_count": len(open_items),
        "retired_count": len(retired_items),
        "oldest_open_days": open_items[0]["age_days"] if open_items else 0,
        "oldest_open_code": open_items[0]["code"] if open_items else None,
        "median_retire_latency_days": (
            latencies[len(latencies) // 2] if latencies else None
        ),
    }

    return {
        "schema_version": DEBT_SCHEMA,
        "as_of": as_of.isoformat(),
        "open": open_items,
        "retired": retired_items,
        "summary": summary,
    }


def render_debt(report: dict[str, Any]) -> str:
    """Human rendering. The ages are the point, so they lead each line."""
    lines = [f"[graphite] declared debt as of {report['as_of']}"]
    summary = report["summary"]

    if not report["open"]:
        lines.append("  no open blind spots declared")
    else:
        lines.append(f"  OPEN ({summary['open_count']}):")
        for item in report["open"]:
            lines.append(f"    {item['age_days']:>5}d  {item['code']}")
            lines.append(f"           {item['summary']}")

    if report["retired"]:
        lines.append(f"  RETIRED ({summary['retired_count']}), declaration -> fix:")
        for item in report["retired"]:
            lines.append(f"    {item['latency_days']:>5}d  {item['code']}")

    median = summary["median_retire_latency_days"]
    if median is not None:
        lines.append(f"  median time to retire: {median}d")
    lines.append(
        "  note: counts are not the target -- an unfixed blind spot that is "
        "DECLARED is working as designed; an undeclared one is the failure."
    )
    return "\n".join(lines)
