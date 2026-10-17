"""What a read command records about itself for the savings display.

`context`, `impact`, `query` and `search` each hand `_record_canonical_usage`
the clock reading they started at. The ledger tests pass `wall_ms` in
directly, and the end-to-end test only checks it is not negative, so nothing
held the conversion from seconds to milliseconds: a mutant that multiplied by
1001 survived the whole suite.
"""
from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from graphite import cli, usage_ledger


def test_the_elapsed_time_is_recorded_in_milliseconds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[Path, dict[str, Any]]] = []

    def capture(root: Path, **kwargs: Any) -> None:
        calls.append((root, kwargs))

    # A clock of cli's own: patching time.perf_counter itself would stop it for
    # every thread in the process. 12.0 - 10.0 is exact in binary.
    monkeypatch.setattr(cli, "time", types.SimpleNamespace(perf_counter=lambda: 12.0))
    monkeypatch.setattr(cli, "_cli_root", lambda: tmp_path)
    monkeypatch.setattr(usage_ledger, "record_usage", capture)

    cli._record_canonical_usage("query", {"r": 1}, 10.0)

    # Exactly one call: the function swallows every exception, so a value check
    # alone would pass if the call never happened.
    assert calls == [(tmp_path, {"cmd": "query", "wall_ms": 2000, "result": {"r": 1}})]
