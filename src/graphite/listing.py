"""Render a bounded list so that emptiness and truncation are always visible.

Every human-output listing goes through :func:`listing_lines`. Two guarantees
hold by construction: a header is never printed over an empty body, and a
capped list always names how many items it dropped.

See docs/superpowers/specs/2026-07-26-listing-marker-contract-design.md.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

__all__ = ["listing_lines"]


def listing_lines(
    values: Sequence[Any],
    render: Callable[[Any], str] = str,
    *,
    header: str | None = None,
    cap: int | None = None,
    indent: str = "  ",
    empty: str | None = "none found",
    more_hint: str | None = None,
) -> list[str]:
    """Lines for one bounded listing: header, body, and a truncation marker.

    ``cap`` of None (or <= 0) means no cap. ``empty=None`` means the whole
    listing is omitted when there is nothing to show -- header included, since
    a header over an empty body is the defect this module exists to prevent.
    ``more_hint`` is appended verbatim; callers supply their own separator.
    """
    if not values and empty is None:
        return []

    lines: list[str] = []
    if header is not None:
        lines.append(header)

    if not values:
        lines.append(f"{indent}- {empty}")
        return lines

    shown = list(values) if cap is None or cap <= 0 else list(values[:cap])
    lines.extend(f"{indent}- {render(value)}" for value in shown)
    dropped = len(values) - len(shown)
    if dropped > 0:
        lines.append(f"{indent}... {dropped} more{more_hint or ''}")
    return lines
