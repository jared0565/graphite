"""The listing contract: no bare headers, no silent drops."""

from graphite.listing import listing_lines


def test_empty_with_marker_prints_header_and_marker():
    assert listing_lines([], header="Likely tests:") == [
        "Likely tests:",
        "  - none found",
    ]


def test_empty_with_none_marker_drops_the_header_too():
    """empty=None means 'say nothing' — and R1 forbids a header over nothing."""
    assert listing_lines([], header="Likely tests:", empty=None) == []


def test_under_cap_has_no_truncation_line():
    assert listing_lines(["a", "b"], header="Files:", cap=5) == [
        "Files:",
        "  - a",
        "  - b",
    ]


def test_exactly_at_cap_has_no_truncation_line():
    """Off-by-one falsifier: 3 values, cap 3, nothing dropped."""
    assert listing_lines(["a", "b", "c"], cap=3) == ["  - a", "  - b", "  - c"]


def test_over_cap_names_the_number_dropped():
    assert listing_lines(["a", "b", "c", "d", "e"], cap=2) == [
        "  - a",
        "  - b",
        "  ... 3 more",
    ]


def test_truncation_line_carries_no_bullet():
    """R3: the marker must not be readable as a list entry."""
    lines = listing_lines(["a", "b", "c"], cap=1)
    assert lines[-1] == "  ... 2 more"
    assert not lines[-1].lstrip().startswith("- ")


def test_no_cap_shows_everything():
    assert listing_lines(["a", "b", "c"]) == ["  - a", "  - b", "  - c"]
    assert listing_lines(["a", "b", "c"], cap=None) == ["  - a", "  - b", "  - c"]


def test_cap_of_zero_or_less_is_treated_as_no_cap():
    """A body of nothing under a header would violate R1, so 0 means 'no cap'."""
    assert listing_lines(["a", "b"], cap=0) == ["  - a", "  - b"]
    assert listing_lines(["a", "b"], cap=-3) == ["  - a", "  - b"]


def test_render_callback_formats_each_item():
    assert listing_lines([1, 2], lambda v: f"n={v}") == ["  - n=1", "  - n=2"]


def test_custom_indent_applies_to_body_and_marker():
    lines = listing_lines(["a", "b"], cap=1, indent="")
    assert lines == ["- a", "... 1 more"]


def test_more_hint_is_appended_verbatim():
    lines = listing_lines(["a", "b"], cap=1, more_hint=" — use --json for the full list")
    assert lines[-1] == "  ... 1 more — use --json for the full list"


def test_header_omitted_when_none():
    assert listing_lines(["a"]) == ["  - a"]


def test_custom_empty_string():
    assert listing_lines([], header="Deps:", empty="none") == ["Deps:", "  - none"]
