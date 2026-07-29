"""A failing MCP probe must leave enough behind to diagnose it (#29).

`probe_mcp` holds `result.stdout`, `result.stderr` and `result.returncode`, and
then the `except` that produces `invalid_response` discards all three. The
resulting `{'code': 'invalid_response'}` says only that something went wrong.

That opacity is the actual cost. An intermittent CI failure in this family
consumed six CI runs and four proposed mechanisms -- flake, a `sys.path`
reshape, a fail-closed refusal, manifest-cache interference -- and the reason
none could be checked cheaply is that every occurrence reported the same
four words.

The diagnostics go to the repo incident ledger rather than into `DoctorCheck`:
the ledger is local and bounded, whereas a doctor report may be shared, and
subprocess stderr is exactly the sort of thing that should not travel.
"""
from __future__ import annotations

from pathlib import Path

from graphite.incident_ledger import read_incident_entries, repo_ledger_dir


def _doctor_entries(root: Path) -> list[dict]:
    # The ledger serialises the class under "class"; `klass` is only the
    # keyword argument name. Filtering on "klass" here silently matched
    # nothing and made the bounded-diagnostics test below pass vacuously.
    entries, _ = read_incident_entries(repo_ledger_dir(root))
    return [e for e in entries if e.get("class") == "doctor"]


def test_ledger_accepts_the_doctor_class() -> None:
    """Guard the whitelist: `record_incident` silently drops an unknown class,
    so a typo here would make every assertion below vacuously pass."""
    from graphite.incident_ledger import _CLASSES

    assert "doctor" in _CLASSES


def test_invalid_response_records_the_subprocess_evidence(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes

    check = probes.probe_mcp(
        tmp_path,
        _runner=lambda *a, **k: probes.ProbeProcessResult(3, b"not json at all", b"Traceback: boom", 0.01),
    )

    assert check.details == {"code": "invalid_response"}, "the public contract must not change"

    entries = _doctor_entries(tmp_path)
    assert entries, "a failing probe recorded nothing to diagnose it with"
    blob = " ".join(str(e.get("detail", "")) for e in entries)
    assert "boom" in blob, "the subprocess stderr was discarded again"
    assert "3" in blob, "the returncode was discarded"


def test_diagnostics_do_not_change_the_public_check(tmp_path: Path) -> None:
    """Existing callers assert exact equality on `details`. Widening it would
    break them and would put stderr into a shareable report."""
    import graphite.doctor_probes as probes

    check = probes.probe_mcp(
        tmp_path,
        _runner=lambda *a, **k: probes.ProbeProcessResult(0, b"{}", b"", 0.01),
    )

    assert set(check.details) == {"code"}
    assert check.status == "degraded"


def test_a_broken_ledger_never_breaks_the_probe(tmp_path: Path, monkeypatch) -> None:
    """Diagnostics are a convenience. A probe that fails because its own
    logging failed would be worse than the opacity being fixed."""
    import graphite.doctor_probes as probes

    def _boom(*args, **kwargs):
        raise OSError("ledger unwritable")

    monkeypatch.setattr(probes, "record_incident", _boom)

    check = probes.probe_mcp(
        tmp_path,
        _runner=lambda *a, **k: probes.ProbeProcessResult(1, b"garbage", b"err", 0.01),
    )

    assert check.status == "degraded"
    assert check.details == {"code": "invalid_response"}


def test_recorded_diagnostics_are_bounded(tmp_path: Path) -> None:
    """A probe can emit megabytes. The ledger caps `detail` at 2048 chars, but
    the probe must not rely on that -- an unbounded string still gets built,
    hashed and passed around first."""
    import graphite.doctor_probes as probes

    check = probes.probe_mcp(
        tmp_path,
        _runner=lambda *a, **k: probes.ProbeProcessResult(1, b"x" * 500_000, b"y" * 500_000, 0.01),
    )

    assert check.details == {"code": "invalid_response"}
    entries = _doctor_entries(tmp_path)
    # Asserted non-empty first: iterating an empty list would pass this test
    # without checking anything, which is exactly how it passed while the
    # helper was filtering on the wrong field name.
    assert entries, "nothing recorded, so the bound below was never tested"
    for entry in entries:
        assert len(str(entry.get("detail", ""))) <= 2048
