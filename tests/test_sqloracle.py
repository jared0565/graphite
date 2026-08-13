"""Known-answer tests for `scripts/sqloracle.py`.

Why this file exists, and why it is not optional. The oracle's figures are
published in immutable agent-channel rounds -- round 69 ("5 hits, not 7") and
round 86 ("the oracle can see 2 of 7 of these shapes"). Another agent is
choosing what rule work to do partly on those numbers. An instrument whose
results are cited as evidence but which nobody can re-run produces leads
dressed as measurements, so the instrument and its known answers live here
together or the numbers should not be quoted at all.

The fixtures carry a `.py.txt` extension deliberately. They contain real SQL
injection by construction -- that is their whole job -- and as `.py` files they
would be scanned by this repo's own gate and permanently enlarge its finding
population with hazards that exist only to be found. The extension keeps them
readable and diffable (they are published artifacts, so a silent edit matters)
while keeping them out of ruff and semgrep.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data" / "sqloracle"
ORACLE_PATH = Path(__file__).parents[1] / "scripts" / "sqloracle.py"


def _load_oracle():
    """Import the oracle from its path.

    `scripts/` is not a package and deliberately is not one -- the oracle is a
    standalone grading tool, not part of the shipped library, and the sdist
    allowlist in pyproject.toml excludes `/scripts` for that reason. Loading it
    by path keeps that boundary visible instead of quietly making it importable.
    """
    spec = importlib.util.spec_from_file_location("_sqloracle", ORACLE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def oracle():
    return _load_oracle()


def _scan_source(oracle, tmp_path: Path, source: str) -> list[dict]:
    path = tmp_path / "probe.py"
    path.write_text(source, encoding="utf-8")
    return oracle.scan_file(path)


def _scan_fixture(oracle, tmp_path: Path, name: str) -> list[dict]:
    return _scan_source(oracle, tmp_path, (DATA / name).read_text(encoding="utf-8"))


def _claims(sites: list[dict]) -> list[dict]:
    """The sites the oracle actually ASSERTS are hazards.

    Everything else it emits is a record of what it looked at, not a finding.
    Gating on this helper rather than on any single kind string is the point:
    the defect this module pins is that a caller who tested `kind ==
    "unattributed"` was reading one bucket that held two opposite meanings.
    """
    return [s for s in sites if s["kind"] in _load_oracle_claim_kinds()]


def _load_oracle_claim_kinds() -> frozenset[str]:
    return _CLAIM_KINDS


_CLAIM_KINDS = frozenset({"assigned", "inline"})


# --- pins on the published numbers -----------------------------------------


def test_the_round69_fixture_still_reports_its_seven_known_sites(oracle, tmp_path) -> None:
    """Round 69 published 7 sites for this fixture. If this moves, that round's
    figures moved with it and the channel needs a correction, not a re-baseline."""
    sites = _scan_fixture(oracle, tmp_path, "round69.py.txt")
    assert len(_claims(sites)) == 7
    assert {s["scope"] for s in _claims(sites)} == {
        "positive_concat",
        "positive_fstring",
        "positive_percent",
        "positive_format",
        "positive_in_loop",
        "gap_join",
        "gap_multi_hop",
    }


def test_no_claim_is_made_inside_any_safe_form_of_the_round69_fixture(oracle, tmp_path) -> None:
    sites = _scan_fixture(oracle, tmp_path, "round69.py.txt")
    assert [s for s in _claims(sites) if s["scope"].startswith("safe_")] == []


def test_the_blindspot_fixture_claims_exactly_the_hazards_it_can_reach(oracle, tmp_path) -> None:
    """Round 86 published "of 7 real hazards this oracle claims 2".

    It is now three: tracking augmented assignment was forced by the regression
    documented in `test_an_augmented_build_is_not_reported_as_clean`, and once
    `q += f"..."` taints, the accumulate-in-a-loop hazard is reachable. Round 86
    needs a correction, which is the reason this assertion names scopes rather
    than a count -- a number can drift quietly, a name cannot.
    """
    sites = _scan_fixture(oracle, tmp_path, "blindspots.py.txt")
    assert {s["scope"] for s in _claims(sites)} == {
        "bs_one_branch_only",
        "bs_tuple_unpacking_tainted",
        "bs_augmented_in_loop",
    }


def test_an_augmented_build_is_not_reported_as_clean(oracle, tmp_path) -> None:
    """A regression introduced by the `cleared` split itself, caught by the
    fixture within minutes of the split landing.

    `q` is first bound to a plain constant, which marks it cleared. The hazard
    then accumulates through `+=`, which the walk did not track at all -- so the
    name stayed cleared and the oracle reported a genuine injection as ANALYSED
    AND SAFE. That is strictly worse than the bucket it replaced: `unattributed`
    at least admitted ignorance. A positive safety claim about a hazard is the
    one direction this instrument may not fail in.
    """
    sites = _scan_source(
        oracle,
        tmp_path,
        "import sqlite3\n"
        "cur = sqlite3.connect(':memory:').cursor()\n"
        "values = ['a', 'b']\n"
        "def f():\n"
        "    q = 'SELECT * FROM t WHERE 1=0'\n"
        "    for v in values:\n"
        "        q += f\" OR x = '{v}'\"\n"
        "    cur.execute(q)\n",
    )
    (site,) = [s for s in sites if s["scope"] == "f"]
    assert site["kind"] != "cleared"
    assert site["kind"] in _CLAIM_KINDS


def test_appending_a_constant_keeps_a_tainted_name_tainted(oracle, tmp_path) -> None:
    """The other direction of the same edit. `q += " AND 1=1"` adds nothing
    hazardous, but it must not launder a name that is already tainted -- a
    rule that cleared taint on any augmented assignment would be trivially
    defeated by appending a harmless suffix."""
    sites = _scan_source(
        oracle,
        tmp_path,
        "import sqlite3\n"
        "cur = sqlite3.connect(':memory:').cursor()\n"
        "x = 'untrusted'\n"
        "def f():\n"
        "    q = f\"SELECT * FROM t WHERE x = '{x}'\"\n"
        "    q += ' AND 1=1'\n"
        "    cur.execute(q)\n",
    )
    (site,) = [s for s in sites if s["scope"] == "f"]
    assert site["kind"] in _CLAIM_KINDS


def test_the_blindspot_fixture_produces_no_false_claim_on_any_safe_twin(oracle, tmp_path) -> None:
    """Every hazard in that fixture is paired with a look-alike safe form. A
    claim on one of those is a false positive, which is the failure mode the
    twins exist to catch."""
    sites = _scan_fixture(oracle, tmp_path, "blindspots.py.txt")
    safe_scopes = {
        "bs_cross_function_safe",
        "bs_container_iterated_safe",
        "bs_augmented_in_loop_safe",
        "bs_dead_interpolation",
        "bs_tuple_unpacking_safe",
        "bs_probe_unrelated_execute",
    }
    assert {s["scope"] for s in _claims(sites)} & safe_scopes == set()


# --- the defect: one bucket held two opposite meanings ----------------------


def test_a_taint_cleared_by_rebinding_is_reported_as_cleared_not_as_unseeable(
    oracle, tmp_path
) -> None:
    """The defect reported to aramid in round 86.

    The oracle analyses this correctly -- it taints `q` on the f-string and
    clears the taint when `q` is rebound to a plain constant. It then reported
    the execute as `unattributed`, which is also what it emits for shapes it
    cannot parse at all. "I checked this and it is clean" and "I cannot see
    this" were the same token, so no consumer could tell a verified-safe result
    from a blind spot.
    """
    sites = _scan_source(
        oracle,
        tmp_path,
        "import sqlite3\n"
        "cur = sqlite3.connect(':memory:').cursor()\n"
        "x = 'untrusted'\n"
        "def f():\n"
        "    q = f\"SELECT * FROM t WHERE x = '{x}'\"\n"
        "    q = 'SELECT * FROM t WHERE x = ?'\n"
        "    cur.execute(q, (x,))\n",
    )
    (site,) = [s for s in sites if s["scope"] == "f"]
    assert site["kind"] == "cleared"
    assert site["kind"] not in _CLAIM_KINDS


def test_an_unresolved_name_is_distinguished_from_an_unattributable_expression(
    oracle, tmp_path
) -> None:
    """The other half of the same bucket.

    A NAME the walk could not evaluate is a possible cross-scope flow -- the
    oracle's largest published blind spot and the candidate for future work. An
    attribute or subscript is structurally out of reach of a name-based walk and
    always will be. Collapsing them loses the only signal saying which shapes
    are worth pursuing.

    `unresolved` deliberately covers both "never bound in this scope" (`f`) and
    "bound from something opaque" (`h`). Calling the second one clean would be a
    false negative in the one direction a security instrument may not fail, and
    `q = _build_it()` is precisely the cross-function shape round 86 measured as
    missed by both instruments.
    """
    sites = _scan_source(
        oracle,
        tmp_path,
        "import sqlite3\n"
        "cur = sqlite3.connect(':memory:').cursor()\n"
        "def _build_it():\n"
        "    return 'SELECT 1'\n"
        "def f(q):\n"
        "    cur.execute(q)\n"
        "def g(o):\n"
        "    cur.execute(o.q)\n"
        "def h():\n"
        "    q = _build_it()\n"
        "    cur.execute(q)\n",
    )
    kinds = {s["scope"]: s["kind"] for s in sites}
    assert kinds["f"] == "unresolved_name"
    assert kinds["g"] == "unattributable"
    assert kinds["h"] == "unresolved_name"
    assert set(kinds.values()) & _CLAIM_KINDS == set()


def test_a_name_bound_to_a_plain_string_constant_reads_as_cleared(oracle, tmp_path) -> None:
    """`cleared` must mean "this walk determined the value is not built", not
    merely "a taint was withdrawn". A constant that was never tainted in the
    first place is equally clean, and reporting it as unresolved would put a
    verified-safe result back in the bucket this change exists to empty."""
    sites = _scan_source(
        oracle,
        tmp_path,
        "import sqlite3\n"
        "cur = sqlite3.connect(':memory:').cursor()\n"
        "def f():\n"
        "    q = 'SELECT 1'\n"
        "    cur.execute(q)\n",
    )
    (site,) = [s for s in sites if s["scope"] == "f"]
    assert site["kind"] == "cleared"


def test_claim_kinds_are_exported_so_a_consumer_need_not_hardcode_them(oracle) -> None:
    """A caller asking "is this a finding?" must not have to enumerate kind
    strings, because that enumeration is exactly what went stale when the
    bucket split. The oracle names the answer."""
    assert oracle.CLAIM_KINDS == _CLAIM_KINDS
