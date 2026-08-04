"""Doctor check: foreign hooks that invoke graphite in a shadowable form.

graphite#42. `graphite init` deliberately never rewrites a hook it does not own
(`agent_settings._OWNED_COMMAND_PREFIXES`) -- silently rewriting someone else's
hook would be worse than the bug. So a consumer's own hook calling
`python -m graphite ...`, or the bare console script, keeps the exact defect
`67aafb2` and #43 removed from graphite's own hooks, and nothing says so.

This check REPORTS; it never repairs. Two rules it must not break:

* **Details carry no absolute path.** Same rule as `check_hooks`, and the
  commands here are mostly path, so the classification is reported instead of
  the command text.
* **"No findings" must never mean "could not look."** An unreadable settings
  file is `degraded`, not `ready`.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from graphite.doctor import check_agent_hooks

OWNED = "python -P -m graphite agent-hook pre-tool-use --mode strict"
LEGACY_OWNED = "python -m graphite agent-hook session-start"
PRIVATE = r"C:\Users\someone\private\checkout"


def _repo(tmp_path: Path, *, onboarded: bool = True) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607
    if onboarded:
        (tmp_path / "GRAPHITE.md").write_text("x", encoding="utf-8")
    return tmp_path


def _settings(root: Path, hooks: dict) -> None:
    directory = root / ".claude"
    directory.mkdir(exist_ok=True)
    (directory / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


def _event(command: str) -> list:
    return [{"hooks": [{"type": "command", "command": command}]}]


def _forms(check) -> list[str]:
    return [f["form"] for f in check.details["findings"]]


def test_un_onboarded_repo_is_not_a_finding(tmp_path: Path) -> None:
    """Nagging every repo that never opted in is how a real warning gets
    ignored -- the severity rule `check_hooks` already follows."""
    root = _repo(tmp_path, onboarded=False)
    _settings(root, {"Stop": _event("graphite build .")})

    assert check_agent_hooks(root).status == "optional"


def test_graphites_own_hooks_are_never_reported(tmp_path: Path) -> None:
    """The false-positive guard. Without it the check fires on every onboarded
    repo and is worth nothing."""
    root = _repo(tmp_path)
    _settings(root, {"PreToolUse": _event(OWNED)})

    check = check_agent_hooks(root)

    assert check.status == "ready"
    assert check.details["foreign_graphite_hooks"] == 0


def test_the_legacy_owned_prefix_is_also_ours_not_a_finding(tmp_path: Path) -> None:
    """A repo mid-migration still has the pre-`67aafb2` spelling. It is
    vulnerable, but it is OURS -- `graphite init` fixes it, so reporting it here
    would send the operator to hand-edit a generated file."""
    root = _repo(tmp_path)
    _settings(root, {"SessionStart": _event(LEGACY_OWNED)})

    assert check_agent_hooks(root).details["foreign_graphite_hooks"] == 0


def test_a_bare_console_script_hook_is_reported(tmp_path: Path) -> None:
    """Measured: `graphite ...` is shadowed exactly like `python -m graphite`,
    and it cannot express the fix -- there is no `-P` to add."""
    root = _repo(tmp_path)
    _settings(root, {"Stop": _event(f'graphite build "{PRIVATE}"')})

    check = check_agent_hooks(root)

    assert check.status == "degraded"
    assert _forms(check) == ["console_script"]


def test_a_python_m_hook_without_dash_P_is_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _settings(root, {"Stop": _event(f"python -m graphite build {PRIVATE}")})

    check = check_agent_hooks(root)

    assert check.status == "degraded"
    assert _forms(check) == ["python_m_without_P"]


def test_a_foreign_hook_that_already_passes_dash_P_is_fine(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _settings(root, {"Stop": _event(f"python -P -m graphite build {PRIVATE}")})

    check = check_agent_hooks(root)

    assert check.status == "ready"
    assert check.details["foreign_graphite_hooks"] == 0


def test_a_hook_that_does_not_invoke_graphite_is_ignored(tmp_path: Path) -> None:
    """Not our business. A command merely MENTIONING graphite -- a path
    argument, an echo -- must not be classified."""
    root = _repo(tmp_path)
    _settings(root, {"Stop": _event(f'echo "rebuilding graphite graph in {PRIVATE}"')})

    assert check_agent_hooks(root).details["foreign_graphite_hooks"] == 0


def test_an_unreadable_settings_file_never_reads_as_clean(tmp_path: Path) -> None:
    """`_load_settings` returns None for malformed, distinctly from {} for
    absent. If corruption reported `ready`, absence of a finding would
    masquerade as safety -- the exact failure `answer.grade` exists to stop."""
    root = _repo(tmp_path)
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")

    check = check_agent_hooks(root)

    assert check.status == "degraded"
    assert check.details["settings_readable"] is False


def test_an_absent_settings_file_is_ready_not_degraded(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    check = check_agent_hooks(root)

    assert check.status == "ready"
    assert check.details["settings_readable"] is True


def test_an_unknown_event_key_is_bucketed_not_echoed(tmp_path: Path) -> None:
    """The event is a key read out of the file, so it is attacker-controllable.
    Bucketing unknown keys is what makes "details carry no path" a property of
    the code rather than a property of well-behaved input."""
    root = _repo(tmp_path)
    _settings(root, {PRIVATE: _event("graphite build .")})

    check = check_agent_hooks(root)

    assert [f["event"] for f in check.details["findings"]] == ["other"]


def test_details_carry_no_absolute_path_and_a_fixed_key_set(tmp_path: Path) -> None:
    """NOTE: asserting over detail VALUES, never `str(p) in json.dumps(...)` --
    `test_details_do_not_leak_the_absolute_hooks_path` records that the
    json.dumps form is vacuous on Windows, because it doubles backslashes so a
    raw path never matches its own escaped form."""
    root = _repo(tmp_path)
    # A path in BOTH places a path can reach details: the command, and the
    # event KEY. Mutation testing found this test blind to the second -- with a
    # known event like "Stop" it passes even when the raw key is echoed.
    _settings(root, {PRIVATE: _event(f'graphite build "{PRIVATE}"')})

    check = check_agent_hooks(root)

    flat = [str(v) for v in check.details.values()]
    flat += [str(v) for f in check.details["findings"] for v in f.values()]
    assert not any(PRIVATE in value for value in flat)
    assert not any(str(tmp_path) in value for value in flat)
    assert set(check.details) == {
        "onboarded",
        "settings_readable",
        "foreign_graphite_hooks",
        "findings",
    }


def test_every_finding_is_counted(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _settings(
        root,
        {
            "Stop": _event("graphite build ."),
            "PreToolUse": _event(OWNED),
            "SessionStart": _event("python -m graphite build ."),
        },
    )

    check = check_agent_hooks(root)

    assert check.details["foreign_graphite_hooks"] == 2
    assert sorted(_forms(check)) == ["console_script", "python_m_without_P"]
