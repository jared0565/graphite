"""Suite-wide safety rails.

The activation registry lives in a *user-scoped* directory, not under the repo,
so a test that marks a repository active would otherwise write into the real
`%LOCALAPPDATA%\\graphite\\active\\` and the live daemon would start supervising
pytest temp directories. The suite has polluted live state before (the incident
ledger, fixed in 36e2528); this fixture makes that class of bug impossible for
activation rather than relying on every test file to remember.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite import activation


@pytest.fixture(autouse=True)
def _isolate_activation_state(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every test's activation registry at a throwaway directory."""
    state = tmp_path_factory.mktemp("graphite-state")
    monkeypatch.setenv(activation.ENV_STATE_DIR, str(state))
    monkeypatch.delenv(activation.ENV_DAEMON_CHILD, raising=False)
    return state
