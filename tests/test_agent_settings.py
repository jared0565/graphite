"""Tests for the idempotent Claude Code settings installer."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.agent_settings import ensure_claude_settings, existing_mode


def _settings(root: Path) -> dict:
    return json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _commands(settings: dict, event: str) -> list[str]:
    return [
        h["command"]
        for entry in settings.get("hooks", {}).get(event, [])
        for h in entry.get("hooks", [])
    ]


def test_fresh_install_writes_both_events_remind_mode(tmp_path: Path) -> None:
    result = ensure_claude_settings(tmp_path)
    settings = _settings(tmp_path)

    assert result["changed"] is True
    assert result["action"] == "created"
    assert result["mode"] == "remind"
    assert _commands(settings, "PreToolUse") == ["python -m graphite agent-hook pre-tool-use --mode remind"]
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Grep|Glob|Bash|PowerShell"
    assert _commands(settings, "SessionStart") == ["python -m graphite agent-hook session-start"]


def test_reinstall_is_idempotent(tmp_path: Path) -> None:
    ensure_claude_settings(tmp_path)
    second = ensure_claude_settings(tmp_path)
    assert second["changed"] is False
    assert second["action"] == "already current"


def test_preserves_foreign_settings_and_hooks(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo custom"}]}
                    ],
                    "Stop": [{"hooks": [{"type": "command", "command": "echo bye"}]}],
                },
            }
        ),
        encoding="utf-8",
    )

    ensure_claude_settings(tmp_path)
    settings = _settings(tmp_path)

    assert settings["model"] == "opus"
    assert "echo custom" in _commands(settings, "PreToolUse")
    assert any(c.startswith("python -m graphite agent-hook pre-tool-use") for c in _commands(settings, "PreToolUse"))
    assert "echo bye" in _commands(settings, "Stop")
    assert "python -m graphite agent-hook stop" in _commands(settings, "Stop")


def test_replaces_stale_graphite_entries_on_reinit(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Grep",
                            "hooks": [{"type": "command", "command": "python -m graphite agent-hook pre-tool-use --old-flag"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = ensure_claude_settings(tmp_path)
    settings = _settings(tmp_path)

    assert result["changed"] is True
    commands = _commands(settings, "PreToolUse")
    assert commands == ["python -m graphite agent-hook pre-tool-use --mode remind"]
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Grep|Glob|Bash|PowerShell"


def test_mode_preserved_without_explicit_request(tmp_path: Path) -> None:
    ensure_claude_settings(tmp_path, mode="strict")
    assert existing_mode(tmp_path) == "strict"
    result = ensure_claude_settings(tmp_path)  # routine re-init, no explicit mode
    assert result["mode"] == "strict"
    assert "--mode strict" in _commands(_settings(tmp_path), "PreToolUse")[0]


def test_explicit_mode_overrides(tmp_path: Path) -> None:
    ensure_claude_settings(tmp_path, mode="strict")
    result = ensure_claude_settings(tmp_path, mode="remind")
    assert result["mode"] == "remind"
    assert "--mode remind" in _commands(_settings(tmp_path), "PreToolUse")[0]


def test_malformed_json_is_never_touched(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    result = ensure_claude_settings(tmp_path)

    assert result["changed"] is False
    assert result["action"] == "malformed settings"
    assert path.read_text(encoding="utf-8") == "{not json"


def test_non_dict_hooks_value_is_a_noop(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    original_bytes = json.dumps({"hooks": ["not", "a", "dict"], "other": "preserved"}).encode("utf-8")
    path.write_bytes(original_bytes)

    result = ensure_claude_settings(tmp_path)

    assert result["action"] == "malformed settings"
    assert result["changed"] is False
    assert path.read_bytes() == original_bytes


def test_fresh_install_wires_stop_hook(tmp_path: Path) -> None:
    ensure_claude_settings(tmp_path)
    settings = _settings(tmp_path)
    assert _commands(settings, "Stop") == ["python -m graphite agent-hook stop"]


def test_spec_a_shaped_settings_gain_stop_on_reinit(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Grep|Glob",
                            "hooks": [{"type": "command", "command": "python -m graphite agent-hook pre-tool-use --mode strict"}],
                        }
                    ],
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "python -m graphite agent-hook session-start"}]}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    result = ensure_claude_settings(tmp_path)
    settings = _settings(tmp_path)

    assert result["changed"] is True
    assert result["mode"] == "strict"  # preserved across the upgrade
    assert _commands(settings, "Stop") == ["python -m graphite agent-hook stop"]
    assert "--mode strict" in _commands(settings, "PreToolUse")[0]
