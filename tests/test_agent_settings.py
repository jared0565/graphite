"""Tests for the idempotent Claude Code settings installer."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.agent_settings import (
    classify_hook_command,
    ensure_claude_settings,
    existing_mode,
    read_settings,
)


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


# --- read_settings: the tri-state is the contract -----------------------------
#
# `{}` absent and `None` malformed are DIFFERENT answers, and collapsing them is
# how a corrupt file comes to read as a clean one. `doctor.check_agent_hooks`
# turns `None` into a `degraded` finding precisely so that "no findings" can
# never mean "could not look" -- so these two return values are load-bearing,
# not an implementation detail.


def test_read_settings_returns_empty_dict_when_absent(tmp_path: Path) -> None:
    assert read_settings(tmp_path) == {}


def test_read_settings_returns_the_parsed_mapping(tmp_path: Path) -> None:
    ensure_claude_settings(tmp_path)

    settings = read_settings(tmp_path)

    assert isinstance(settings, dict)
    assert "hooks" in settings


def test_read_settings_returns_none_for_malformed_json(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")

    assert read_settings(tmp_path) is None


def test_read_settings_returns_none_for_valid_json_that_is_not_a_mapping(tmp_path: Path) -> None:
    """`[]` parses fine and would then blow up on `.get`. Distinguished from
    absent, because a settings file that is a list is corrupt, not empty."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("[1, 2]", encoding="utf-8")

    assert read_settings(tmp_path) is None


def test_read_settings_returns_none_when_the_path_cannot_be_read(tmp_path: Path) -> None:
    # A directory where the file should be: `exists()` is True, `read_text`
    # raises OSError. Unreadable must not be reported as absent.
    (tmp_path / ".claude" / "settings.json").mkdir(parents=True)

    assert read_settings(tmp_path) is None


# --- classify_hook_command: the branches the doctor tests do not reach --------


def test_classify_declines_to_guess_at_an_unparseable_command() -> None:
    """Unbalanced quotes. Reporting a command we could not lex would be a
    finding built on a guess."""
    assert classify_hook_command('graphite build "unterminated') is None


def test_classify_ignores_empty_and_non_string_commands() -> None:
    for command in ("", "   ", None, 42, ["graphite"]):
        assert classify_hook_command(command) is None, command


def test_classify_sees_through_an_absolute_interpreter_path() -> None:
    """The head is a path, not the bare name `python` -- the stem is what
    matters, or every hook written with a full interpreter path goes unchecked."""
    assert classify_hook_command(r"C:\Python314\python.exe -m graphite build .") == "python_m_without_P"


def test_classify_does_not_match_a_module_that_merely_starts_with_graphite() -> None:
    """`graphitex` is somebody else's package. Prefix-matching the module name
    would report a hook that has nothing to do with us."""
    assert classify_hook_command("python -m graphitex build .") is None


def test_classify_handles_a_dangling_dash_m() -> None:
    assert classify_hook_command("python -m") is None


def test_classify_accepts_the_windows_console_script_spelling() -> None:
    assert classify_hook_command("graphite.exe build .") == "console_script"


def test_classify_treats_a_graphite_submodule_invocation_as_ours_to_flag() -> None:
    """`-m graphite.hook_entry` is the trampoline's form (graphite#43). Shadowed
    identically, so the module's ROOT package is what decides."""
    assert classify_hook_command("python -m graphite.hook_entry") == "python_m_without_P"
