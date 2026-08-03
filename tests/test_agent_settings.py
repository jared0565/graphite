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
    assert _commands(settings, "PreToolUse") == ["python -P -m graphite agent-hook pre-tool-use --mode remind"]
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Grep|Glob|Bash|PowerShell"
    assert _commands(settings, "SessionStart") == ["python -P -m graphite agent-hook session-start"]


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
    assert any(c.startswith("python -P -m graphite agent-hook pre-tool-use") for c in _commands(settings, "PreToolUse"))
    assert "echo bye" in _commands(settings, "Stop")
    assert "python -P -m graphite agent-hook stop" in _commands(settings, "Stop")


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
    assert commands == ["python -P -m graphite agent-hook pre-tool-use --mode remind"]
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
    assert _commands(settings, "Stop") == ["python -P -m graphite agent-hook stop"]


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
    assert _commands(settings, "Stop") == ["python -P -m graphite agent-hook stop"]
    assert "--mode strict" in _commands(settings, "PreToolUse")[0]


# --- graphite#49: bare `python -m` lets a local graphite.py shadow the package -


def test_hook_commands_omit_the_working_directory_from_sys_path(tmp_path: Path) -> None:
    """Every generated hook must run the interpreter with `-P`.

    `python -m X` puts the CWD at `sys.path[0]`, so a `graphite.py` in a repo
    root wins over the installed package and the hook executes it instead. The
    pre-tool-use hook fires on every tool use, so such a file never has to be
    invoked by anyone -- it runs on the next agent action after it lands. And
    `.claude/settings.json` is tracked, so it would arrive through an ordinary
    reviewed commit.

    Reproduced before fixing: with a `graphite.py` in the CWD, bare
    `python -m graphite --version` printed the shadow file's output, while
    `python -P -m graphite --version` printed graphite's usage.

    Reported by aramid-agent, channel round 49.
    """
    ensure_claude_settings(tmp_path, mode="strict")
    settings = _settings(tmp_path)

    for event in ("PreToolUse", "SessionStart", "Stop"):
        for command in _commands(settings, event):
            assert command.startswith("python -P -m graphite agent-hook"), (event, command)


def test_install_replaces_a_legacy_bare_hook_rather_than_duplicating_it(tmp_path: Path) -> None:
    """The old command must still be recognised as graphite-owned, and stripped.

    The command prefix is both what we WRITE and what we MATCH on to find our
    own hooks. Changing it without teaching the matcher the old spelling would
    leave every already-onboarded repo with the legacy hook unstripped and the
    new one appended beside it -- two hooks, the vulnerable one still firing.
    """
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Grep|Glob|Bash|PowerShell",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python -m graphite agent-hook pre-tool-use --mode strict",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    ensure_claude_settings(tmp_path)

    commands = _commands(_settings(tmp_path), "PreToolUse")
    assert commands == ["python -P -m graphite agent-hook pre-tool-use --mode strict"]


def test_legacy_bare_hook_still_reports_its_mode(tmp_path: Path) -> None:
    """A strict repo must not be silently downgraded to remind by the migration.

    `ensure_claude_settings` resolves the mode as
    `mode or existing_mode(root) or DEFAULT_MODE`, and DEFAULT_MODE is "remind".
    So if the new prefix stopped matching the legacy command, `existing_mode`
    would return None and every already-strict repo would quietly drop to
    remind on the next `graphite init` -- a security downgrade produced by a
    security fix, and invisible in the diff.
    """
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python -m graphite agent-hook pre-tool-use --mode strict",
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert existing_mode(tmp_path) == "strict"
    assert ensure_claude_settings(tmp_path)["mode"] == "strict"
