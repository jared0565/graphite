"""Suite-wide safety rails.

The activation registry lives in a *user-scoped* directory, not under the repo,
so a test that marks a repository active would otherwise write into the real
`%LOCALAPPDATA%\\graphite\\active\\` and the live daemon would start supervising
pytest temp directories. The suite has polluted live state before (the incident
ledger, fixed in 36e2528); this fixture makes that class of bug impossible for
activation rather than relying on every test file to remember.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from graphite import activation


@pytest.fixture(autouse=True)
def _isolate_activation_state(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every test's activation registry at a throwaway directory."""
    state = tmp_path_factory.mktemp("graphite-state")
    monkeypatch.setenv(activation.ENV_STATE_DIR, str(state))
    monkeypatch.delenv(activation.ENV_DAEMON_CHILD, raising=False)
    return state


@pytest.fixture
def assert_json_omits() -> Callable[[object, object], None]:
    """Assert a needle appears nowhere in a JSON payload, escaped spelling included.

    `needle not in json.dumps(payload)` is **vacuous whenever the needle is a
    Windows path**: `json.dumps` doubles every backslash, so a raw path can
    never match its own serialised form. The assertion passes while the leak sits
    in plain sight.

    That is not hypothetical. `daemon_health` embedded an absolute status path in
    its report and `test_daemon_health.py` waved it through for as long as
    windows-latest was the only platform CI ran; the portability matrix caught it
    on macOS the first time it executed, because POSIX paths have no backslashes
    to escape.

    Comparing the escaped spelling too makes the check bite on every host.
    """

    def _check(needle: object, payload: object) -> None:
        blob = payload if isinstance(payload, str) else json.dumps(payload)
        text = str(needle)
        escaped = json.dumps(text)[1:-1]
        assert text not in blob, f"{text!r} leaked into the payload"
        assert escaped not in blob, f"{text!r} leaked, escaped as {escaped!r}"

    return _check
