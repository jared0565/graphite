"""Tests for the Claude Code agent-hook handlers (fail-open by design)."""
from __future__ import annotations

import io
import json
import os
import shutil
from pathlib import Path

import pytest

from graphite.agent_hooks import (
    _shell_path,
    handle_pre_tool_use,
    handle_session_start,
    handle_stop,
)
from graphite.usage_ledger import record_usage, set_savings_display


def _payload(root: Path) -> dict:
    return {"session_id": "s1", "cwd": str(root), "hook_event_name": "SessionStart"}


def test_session_start_missing_graph_reports_missing(tmp_path: Path) -> None:
    out = handle_session_start(_payload(tmp_path))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "graphite-first" in ctx
    assert "missing" in ctx
    assert "python -m graphite build ." in ctx


def test_session_start_stale_graph_warns(tmp_path: Path) -> None:
    (tmp_path / "graph-out").mkdir()
    (tmp_path / "graph-out" / "graph.json").write_text("{}", encoding="utf-8")
    # No manifest -> check_graph_freshness reports stale ("missing manifest").
    out = handle_session_start(_payload(tmp_path))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "STALE" in ctx


def test_session_start_fresh_graph(tmp_path: Path, monkeypatch) -> None:
    from graphite.cli import main

    (tmp_path / "alpha.py").write_text("def target_symbol():\n    return 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # cmd_build writes cfg.output_dir relative to CWD
    assert main(["build", "."]) == 0
    out = handle_session_start(_payload(tmp_path))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "fresh" in ctx


def test_session_start_bad_cwd_fails_open(tmp_path: Path) -> None:
    # Nonexistent cwd must not raise; missing graph messaging is acceptable.
    out = handle_session_start({"cwd": str(tmp_path / "nope")})
    assert out is None or "graphite-first" in out["hookSpecificOutput"]["additionalContext"]


def _grep_payload(root: Path, pattern: str, **tool_input) -> dict:
    return {
        "session_id": "s1",
        "cwd": str(root),
        "hook_event_name": "PreToolUse",
        "tool_name": "Grep",
        "tool_input": {"pattern": pattern, **tool_input},
    }


def _with_graph(tmp_path: Path) -> Path:
    (tmp_path / "graph-out").mkdir(exist_ok=True)
    (tmp_path / "graph-out" / "graph.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_remind_emits_context_when_graph_present(tmp_path: Path) -> None:
    _with_graph(tmp_path)
    out = handle_pre_tool_use(_grep_payload(tmp_path, "anything"), "remind")
    hook = out["hookSpecificOutput"]
    assert hook["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hook
    assert "graph-first" in hook["additionalContext"]


def test_remind_silent_without_graph(tmp_path: Path) -> None:
    assert handle_pre_tool_use(_grep_payload(tmp_path, "anything"), "remind") is None


def test_remind_ignores_other_tools(tmp_path: Path) -> None:
    _with_graph(tmp_path)
    payload = _grep_payload(tmp_path, "x")
    payload["tool_name"] = "Read"
    assert handle_pre_tool_use(payload, "remind") is None


def test_glob_gets_remind_never_deny_even_in_strict(tmp_path: Path) -> None:
    _with_graph(tmp_path)
    payload = _grep_payload(tmp_path, "target_symbol")
    payload["tool_name"] = "Glob"
    out = handle_pre_tool_use(payload, "strict")
    assert "permissionDecision" not in out["hookSpecificOutput"]


@pytest.fixture()
def built_repo(tmp_path: Path, monkeypatch) -> Path:
    from graphite.cli import main

    (tmp_path / "alpha.py").write_text(
        "def target_symbol():\n    return 1\n\n\ndef other():\n    return target_symbol()\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)  # cmd_build writes cfg.output_dir relative to CWD
    assert main(["build", "."]) == 0
    return tmp_path


def test_strict_denies_cross_file_grep_for_known_symbol(built_repo: Path) -> None:
    out = handle_pre_tool_use(_grep_payload(built_repo, "target_symbol"), "strict")
    hook = out["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"
    reason = hook["permissionDecisionReason"]
    assert "target_symbol" in reason
    assert 'query "callers target_symbol"' in reason
    assert "graphite" in reason


def test_strict_allows_unknown_tokens(built_repo: Path) -> None:
    out = handle_pre_tool_use(_grep_payload(built_repo, "no_such_symbol_here"), "strict")
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_strict_allows_single_file_scoped_grep(built_repo: Path) -> None:
    out = handle_pre_tool_use(
        _grep_payload(built_repo, "target_symbol", path=str(built_repo / "alpha.py")), "strict"
    )
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_strict_denies_directory_scoped_grep(built_repo: Path) -> None:
    # A directory-scoped search is still cross-file; only single-FILE scoping opts out.
    out = handle_pre_tool_use(
        _grep_payload(built_repo, "target_symbol", path=str(built_repo)), "strict"
    )
    assert out["hookSpecificOutput"].get("permissionDecision") == "deny"


def test_strict_allows_literal_patterns_without_identifiers(built_repo: Path) -> None:
    out = handle_pre_tool_use(_grep_payload(built_repo, "== 42"), "strict")
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_strict_falls_back_to_remind_on_oversized_graph(built_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr("graphite.agent_hooks.MAX_HOOK_GRAPH_BYTES", 1)
    out = handle_pre_tool_use(_grep_payload(built_repo, "target_symbol"), "strict")
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_remind_mode_never_denies_known_symbols(built_repo: Path) -> None:
    out = handle_pre_tool_use(_grep_payload(built_repo, "target_symbol"), "remind")
    assert "permissionDecision" not in out["hookSpecificOutput"]


def _write_analysis(root: Path, healthy: bool) -> None:
    (root / "graph-out").mkdir(exist_ok=True)
    (root / "graph-out" / ".graphite_analysis.json").write_text(
        json.dumps({"resolution_health": {"schema": 1, "healthy": healthy}}), encoding="utf-8"
    )


def test_strict_denial_preserved_when_graph_healthy(built_repo: Path) -> None:
    _write_analysis(built_repo, healthy=True)
    result = handle_pre_tool_use(_grep_payload(built_repo, "target_symbol"), mode="strict")
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_strict_denial_suspended_when_graph_unhealthy(built_repo: Path) -> None:
    _write_analysis(built_repo, healthy=False)
    result = handle_pre_tool_use(_grep_payload(built_repo, "target_symbol"), mode="strict")
    output = result["hookSpecificOutput"]
    assert "permissionDecision" not in output
    assert "strict denial suspended" in output["additionalContext"]


def test_strict_denial_suspended_when_analysis_missing(built_repo: Path) -> None:
    # built_repo's build step already persists a healthy analysis file; remove it
    # to genuinely simulate "no persisted analysis" rather than the healthy case.
    (built_repo / "graph-out" / ".graphite_analysis.json").unlink(missing_ok=True)
    result = handle_pre_tool_use(_grep_payload(built_repo, "target_symbol"), mode="strict")
    output = result["hookSpecificOutput"]
    assert "permissionDecision" not in output
    assert "strict denial suspended" in output["additionalContext"]


def test_strict_denial_suspended_when_analysis_malformed(built_repo: Path) -> None:
    (built_repo / "graph-out" / ".graphite_analysis.json").write_text("{bad", encoding="utf-8")
    result = handle_pre_tool_use(_grep_payload(built_repo, "target_symbol"), mode="strict")
    assert "strict denial suspended" in result["hookSpecificOutput"]["additionalContext"]


def test_strict_denial_suspended_when_healthy_is_not_bool(built_repo: Path) -> None:
    # _write_analysis hardcodes schema 1 + a real bool; this arrangement needs a
    # non-bool "healthy" under schema 2, so it writes the analysis file directly
    # using the same pattern (built_repo's build step already made graph-out/).
    (built_repo / "graph-out").mkdir(exist_ok=True)
    (built_repo / "graph-out" / ".graphite_analysis.json").write_text(
        json.dumps({"resolution_health": {"schema": 2, "healthy": "true"}}), encoding="utf-8"
    )
    result = handle_pre_tool_use(_grep_payload(built_repo, "target_symbol"), mode="strict")
    output = result["hookSpecificOutput"]
    assert "permissionDecision" not in output
    assert "strict denial suspended" in output["additionalContext"]


def _run_cli_hook(monkeypatch, capsys, argv: list[str], payload) -> tuple[int, str]:
    from graphite.cli import main

    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    code = main(argv)
    return code, capsys.readouterr().out


def test_cli_session_start_emits_hook_json(tmp_path, monkeypatch, capsys) -> None:
    code, out = _run_cli_hook(
        monkeypatch, capsys, ["agent-hook", "session-start"], {"cwd": str(tmp_path)}
    )
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_cli_pre_tool_use_strict_denies(built_repo, monkeypatch, capsys) -> None:
    code, out = _run_cli_hook(
        monkeypatch,
        capsys,
        ["agent-hook", "pre-tool-use", "--mode", "strict"],
        _grep_payload(built_repo, "target_symbol"),
    )
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_cli_malformed_stdin_fails_open(tmp_path, monkeypatch, capsys) -> None:
    code, out = _run_cli_hook(monkeypatch, capsys, ["agent-hook", "session-start"], "{not json")
    assert code == 0
    assert out == ""


def test_cli_agent_hook_rejects_llm_flags(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    from graphite.cli import main

    assert main(["--llm", "cloud", "agent-hook", "session-start"]) == 2


def test_cli_unknown_event_is_a_silent_noop(monkeypatch, capsys) -> None:
    code, out = _run_cli_hook(monkeypatch, capsys, ["agent-hook", "future-event"], {})
    assert code == 0
    assert out == ""


def test_capabilities_output_does_not_list_agent_hook(capsys) -> None:
    from graphite.cli import main

    assert main(["capabilities", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "agent-hook" not in payload["commands"]


def _stop_payload(root: Path, session: str = "s1") -> dict:
    return {"session_id": session, "cwd": str(root), "hook_event_name": "Stop"}


def _use_graphite(root: Path, cmd: str = "context") -> None:
    record_usage(root, cmd=cmd, wall_ms=50, result={"files": [{"path": "alpha.py"}]})


def test_stop_emits_summary_after_usage(built_repo: Path) -> None:
    _use_graphite(built_repo)
    out = handle_stop(_stop_payload(built_repo))
    message = out["systemMessage"]
    assert message.startswith("graphite: est. ")
    assert "saved this turn" in message
    assert "[estimates]" in message


def test_stop_silent_with_no_new_usage(built_repo: Path) -> None:
    _use_graphite(built_repo)
    assert handle_stop(_stop_payload(built_repo)) is not None  # consumes entries
    assert handle_stop(_stop_payload(built_repo)) is None  # nothing new this turn


def test_stop_session_totals_accumulate(built_repo: Path) -> None:
    _use_graphite(built_repo)
    first = handle_stop(_stop_payload(built_repo))["systemMessage"]
    _use_graphite(built_repo)
    second = handle_stop(_stop_payload(built_repo))["systemMessage"]
    assert "session:" in first and "session:" in second
    assert first.split("session:")[1] != second.split("session:")[1]  # totals grew


def test_stop_respects_toggle_but_cursor_still_advances(built_repo: Path) -> None:
    set_savings_display(built_repo, False)
    _use_graphite(built_repo)
    assert handle_stop(_stop_payload(built_repo)) is None
    set_savings_display(built_repo, True)
    assert handle_stop(_stop_payload(built_repo)) is None  # toggle-off turn already consumed


def test_stop_without_session_id_is_silent(built_repo: Path) -> None:
    _use_graphite(built_repo)
    assert handle_stop({"cwd": str(built_repo)}) is None


def test_stop_survives_ledger_rotation_between_turns(built_repo: Path, monkeypatch) -> None:
    from graphite import usage_ledger as ul

    _use_graphite(built_repo)
    assert handle_stop(_stop_payload(built_repo)) is not None
    monkeypatch.setattr(ul, "MAX_LEDGER_BYTES", 1)  # force rotation on the next record
    _use_graphite(built_repo)  # rotates the consumed generation away; fresh file, offset resync
    assert handle_stop(_stop_payload(built_repo)) is not None


def test_stop_prunes_cursor_to_max_sessions(built_repo: Path) -> None:
    from graphite.agent_hooks import MAX_CURSOR_SESSIONS
    from graphite.usage_ledger import read_cursor

    for i in range(MAX_CURSOR_SESSIONS + 5):
        _use_graphite(built_repo)
        handle_stop(_stop_payload(built_repo, session=f"s{i}"))

    sessions = read_cursor(built_repo)["sessions"]
    assert len(sessions) == MAX_CURSOR_SESSIONS
    assert "s0" not in sessions  # oldest pruned


def test_stop_self_heals_corrupted_cursor_tokens(built_repo: Path) -> None:
    from graphite.usage_ledger import read_cursor, write_cursor

    _use_graphite(built_repo)
    assert handle_stop(_stop_payload(built_repo)) is not None
    cursor = read_cursor(built_repo)
    state = cursor["sessions"]["s1"]
    old_offset = state["offset"]
    first_turn_tokens = state["tokens"]  # baseline: one identical turn's worth of tokens
    state["tokens"] = "not-a-number"  # simulate on-disk corruption
    write_cursor(built_repo, cursor)

    _use_graphite(built_repo)  # second, identical turn -> same tokens_saved as the first
    out = handle_stop(_stop_payload(built_repo))
    assert out is not None  # self-healed rather than permanently stalling

    healed = read_cursor(built_repo)["sessions"]["s1"]
    assert isinstance(healed["tokens"], int)
    assert healed["tokens"] == first_turn_tokens  # restarted from 0, not from the corrupt value
    assert healed["offset"] > old_offset  # cursor still advanced despite the corruption


def test_stop_missing_ino_key_falls_back_to_offset_check(built_repo: Path) -> None:
    from graphite.usage_ledger import read_cursor, write_cursor

    _use_graphite(built_repo)
    assert handle_stop(_stop_payload(built_repo)) is not None
    cursor = read_cursor(built_repo)
    del cursor["sessions"]["s1"]["ino"]  # simulate the older 3-key cursor schema
    write_cursor(built_repo, cursor)

    # No new usage since the last stop; a missing `ino` must not force a false
    # resync (which would re-read already-consumed entries and double-count).
    assert handle_stop(_stop_payload(built_repo)) is None


def test_cli_stop_event_emits_summary(built_repo, monkeypatch, capsys) -> None:
    _use_graphite(built_repo)
    code, out = _run_cli_hook(
        monkeypatch, capsys, ["agent-hook", "stop"], _stop_payload(built_repo)
    )
    assert code == 0
    assert json.loads(out)["systemMessage"].startswith("graphite: est. ")


def _graph_only(root: Path) -> None:
    """A graph file with no manifest, so the freshness path is reached."""
    (root / "graph-out").mkdir(exist_ok=True)
    (root / "graph-out" / "graph.json").write_text("{}", encoding="utf-8")


def test_session_start_distinguishes_timeout_from_inconclusive(tmp_path: Path, monkeypatch) -> None:
    """#24: a check that never finishes and a check that ran and could not tell
    both collapsed to the identical 'unknown' message. The timeout case is the
    one most likely to coincide with a real STALE -- a slow check suggests a
    big or cold repo -- so it must not read the same."""
    import graphite.agent_hooks as hooks

    monkeypatch.setattr(hooks, "_FRESHNESS_BUDGET_SECONDS", 0.05)

    def _never_returns(root, cfg):
        import time

        time.sleep(5.0)
        return {"stale": False}

    monkeypatch.setattr(hooks, "check_graph_freshness", _never_returns)
    _graph_only(tmp_path)

    ctx = handle_session_start(_payload(tmp_path))["hookSpecificOutput"]["additionalContext"]

    assert "did not finish" in ctx
    assert "0.05s" in ctx


def test_session_start_inconclusive_check_says_it_ran(tmp_path: Path, monkeypatch) -> None:
    """The other half of #24: a check that RAN and raised must say so, rather
    than borrowing the timeout wording."""
    import graphite.agent_hooks as hooks

    def _raises(root, cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(hooks, "check_graph_freshness", _raises)
    _graph_only(tmp_path)

    ctx = handle_session_start(_payload(tmp_path))["hookSpecificOutput"]["additionalContext"]

    assert "could not determine" in ctx
    assert "did not finish" not in ctx


def test_session_start_stale_message_carries_the_reason(tmp_path: Path, monkeypatch) -> None:
    """#24's second defect: only `stale` survived out of check_graph_freshness's
    status dict, so STALE could never say WHY -- forcing a second manual
    `graphite check .` just to learn it was an engine change."""
    import graphite.agent_hooks as hooks

    monkeypatch.setattr(
        hooks, "check_graph_freshness",
        lambda root, cfg: {"stale": True, "reason": "engine_changed"},
    )
    _graph_only(tmp_path)

    ctx = handle_session_start(_payload(tmp_path))["hookSpecificOutput"]["additionalContext"]

    assert "STALE" in ctx
    assert "engine_changed" in ctx


def test_session_start_stale_without_a_reason_still_reports_stale(tmp_path: Path, monkeypatch) -> None:
    """A reason is threaded when present and must never become load-bearing:
    absent one, STALE still has to be reported."""
    import graphite.agent_hooks as hooks

    monkeypatch.setattr(hooks, "check_graph_freshness", lambda root, cfg: {"stale": True})
    _graph_only(tmp_path)

    ctx = handle_session_start(_payload(tmp_path))["hookSpecificOutput"]["additionalContext"]

    assert "STALE" in ctx
    assert "python -m graphite build ." in ctx


# --------------------------------------------------------------- bash route --
# The reported bypass (2026-07-31): the PreToolUse matcher was "Grep|Glob", so
# `grep` run through the Bash tool never reached this hook at all. Two consumer
# agents independently reported using that route for cross-file searches.


def _bash_payload(root: Path, command: str) -> dict:
    return {"cwd": str(root), "tool_name": "Bash", "tool_input": {"command": command}}


@pytest.mark.parametrize(
    "command",
    [
        "grep -rn target_symbol .",
        "grep -rln 'target_symbol' src/",
        "rg target_symbol",
        "rg -n --hidden target_symbol .",
        "git grep target_symbol",
        "egrep -r target_symbol .",
        "cd subdir && grep -rn target_symbol .",
        "ls; grep -rn target_symbol .",
    ],
)
def test_strict_denies_cross_file_search_run_through_bash(built_repo: Path, command: str) -> None:
    out = handle_pre_tool_use(_bash_payload(built_repo, command), "strict")

    hook = out["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"
    assert "target_symbol" in hook["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        "ls | grep target_symbol",
        "cat alpha.py | grep -n target_symbol",
        'python -m graphite query "callers target_symbol" | grep name',
        "git log --oneline | grep target_symbol",
    ],
)
def test_search_downstream_of_a_pipe_is_filtering_not_searching(built_repo: Path, command: str) -> None:
    """`... | grep x` filters another command's output. Denying it would break
    the graph queries this hook exists to encourage -- the graphite commands in
    the denial message are themselves piped into grep constantly."""
    out = handle_pre_tool_use(_bash_payload(built_repo, command), "strict")

    assert out is None or "permissionDecision" not in out.get("hookSpecificOutput", {})


@pytest.mark.parametrize(
    ("command", "why"),
    [
        ("grep -n target_symbol alpha.py 2>/dev/null", "redirection operands are not paths"),
        ("grep -n target_symbol alpha.py >out.txt", "redirect target is not a search path"),
        ("grep -n target_symbol -A 5 alpha.py", "a separated flag VALUE is not a path"),
        ("grep -n target_symbol -m 1 alpha.py", "same, for -m"),
        ("grep -n target_symbol -A 5 alpha.py 2>/dev/null", "both at once"),
    ],
)
def test_strict_allows_a_single_file_search_despite_shell_noise(
    built_repo: Path, command: str, why: str
) -> None:
    """The single-file exemption has to survive ordinary shell syntax.

    `_bash_search_pattern` took every non-flag token as a path argument, so
    `2>/dev/null` contributed `2`, `>` and `/dev/null`, and `-A 5` contributed
    `5`. `_is_single_file_scope` requires EVERY named path to be an existing
    file, so one such token flipped it to False and a genuinely single-file
    search was denied -- while the denial text told the reader that "literal
    text searches scoped to a single file path are always allowed".

    Measured, not theorised: `grep -E "..." file 2>/dev/null | head` parsed to
    paths `['file', '2', '>', '/dev/null']`.
    """
    out = handle_pre_tool_use(_bash_payload(built_repo, command), "strict")

    decision = (out or {}).get("hookSpecificOutput", {}).get("permissionDecision")
    assert decision != "deny", f"{why}: {command}"


@pytest.mark.parametrize(
    "command",
    [
        "grep -rn target_symbol . 2>/dev/null",
        "grep -rn target_symbol src/ -A 5",
        "grep -rn target_symbol . >out.txt 2>&1",
    ],
)
def test_shell_noise_is_not_a_bypass_for_a_cross_file_search(
    built_repo: Path, command: str
) -> None:
    """The falsifiability half. Ignoring redirections and flag values must not
    become a way to launder a directory-scoped search past the gate -- if these
    stopped denying, the fix would have removed the rule rather than repaired
    it."""
    out = handle_pre_tool_use(_bash_payload(built_repo, command), "strict")

    assert out["hookSpecificOutput"].get("permissionDecision") == "deny", command


def test_uppercase_regex_mode_flag_is_not_read_as_the_pattern(built_repo: Path) -> None:
    """`-E` is extended-regex MODE and takes no value; `-e`'s value IS the
    pattern. The lookup lowercased every token, so `-E` matched `-e` and
    `grep -E -i PAT .` read `-i` as the pattern -- which matches no graph symbol,
    so the search was allowed through. That direction fails open, so it cost
    enforcement rather than causing false denials, and nothing caught it.
    """
    out = handle_pre_tool_use(
        _bash_payload(built_repo, "grep -E -i target_symbol ."), "strict"
    )

    assert out["hookSpecificOutput"].get("permissionDecision") == "deny"
    assert "target_symbol" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_strict_allows_a_search_outside_the_repository(
    built_repo: Path, tmp_path_factory
) -> None:
    """The graph describes THIS repo. A search rooted somewhere else is not a
    question the graph could answer, so denying it directs the reader to a tool
    that has nothing to say -- and the message names symbols from a repo the
    search never touched.

    Found by the hook firing on a scratchpad file, where the *pattern* happened
    to contain `START` (matching `daemon_start`). The matcher never considered
    that the target was outside the repository at all.
    """
    outside = tmp_path_factory.mktemp("outside-the-repo")
    (outside / "notes.txt").write_text("target_symbol appears here\n", encoding="utf-8")

    out = handle_pre_tool_use(
        _bash_payload(built_repo, f"grep -rn target_symbol {outside.as_posix()}"), "strict"
    )

    decision = (out or {}).get("hookSpecificOutput", {}).get("permissionDecision")
    assert decision != "deny"


@pytest.mark.parametrize(
    "command",
    ["git status", "pytest -q", "python -m graphite build .", "ls -la", "cat alpha.py"],
)
def test_ordinary_bash_passes_through_silently(built_repo: Path, command: str) -> None:
    """Not even a reminder. The hook fires on EVERY Bash call once the matcher
    covers Bash, so nagging non-searches is how a real warning gets ignored."""
    assert handle_pre_tool_use(_bash_payload(built_repo, command), "strict") is None


def test_bash_search_scoped_to_one_existing_file_is_allowed(built_repo: Path) -> None:
    out = handle_pre_tool_use(_bash_payload(built_repo, "grep -n target_symbol alpha.py"), "strict")

    assert out is None or "permissionDecision" not in out.get("hookSpecificOutput", {})


def test_bash_search_for_unknown_token_is_not_denied(built_repo: Path) -> None:
    out = handle_pre_tool_use(_bash_payload(built_repo, "grep -rn no_such_symbol_here ."), "strict")

    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_bash_route_is_silent_without_a_graph(tmp_path: Path) -> None:
    assert handle_pre_tool_use(_bash_payload(tmp_path, "grep -rn x ."), "strict") is None


def test_malformed_bash_command_fails_open(built_repo: Path) -> None:
    """An unbalanced quote must never break the user's shell."""
    assert handle_pre_tool_use(_bash_payload(built_repo, "grep -rn 'unclosed"), "strict") is None


# --------------------------------------------------- powershell / Select-String --
# Same bypass, second shell. The PowerShell tool is a separate tool name, so it
# needs the same matcher entry; Select-String is its grep.


def _ps_payload(root: Path, command: str) -> dict:
    return {"cwd": str(root), "tool_name": "PowerShell", "tool_input": {"command": command}}


@pytest.mark.parametrize(
    "command",
    [
        "Select-String -Pattern target_symbol -Path .",
        "Select-String target_symbol *.py",
        "sls target_symbol",
        "SELECT-STRING -Pattern target_symbol -Path .",
        "Get-Content x; Select-String -Pattern target_symbol -Path .",
    ],
)
def test_strict_denies_select_string_through_powershell(built_repo: Path, command: str) -> None:
    out = handle_pre_tool_use(_ps_payload(built_repo, command), "strict")

    hook = out["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"
    assert "target_symbol" in hook["permissionDecisionReason"]


def test_named_pattern_parameter_is_read_even_when_path_comes_first(built_repo: Path) -> None:
    """`-Path . -Pattern x` puts a non-flag token (the path) before the pattern.
    Taking the first positional would read the path and silently allow the
    search, so -Pattern is honoured explicitly."""
    out = handle_pre_tool_use(
        _ps_payload(built_repo, "Select-String -Path . -Pattern target_symbol"), "strict"
    )

    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_grep_dash_e_pattern_is_read_explicitly(built_repo: Path) -> None:
    """Same hazard on the bash side: `grep -e PAT path`."""
    out = handle_pre_tool_use(_bash_payload(built_repo, "grep -r -e target_symbol ."), "strict")

    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "Get-ChildItem -Recurse | Select-String target_symbol",
        "git log | sls target_symbol",
    ],
)
def test_select_string_downstream_of_a_pipe_is_filtering(built_repo: Path, command: str) -> None:
    out = handle_pre_tool_use(_ps_payload(built_repo, command), "strict")

    assert out is None or "permissionDecision" not in out.get("hookSpecificOutput", {})


@pytest.mark.parametrize("command", ["Get-ChildItem", "git status", "pytest -q"])
def test_ordinary_powershell_passes_through_silently(built_repo: Path, command: str) -> None:
    assert handle_pre_tool_use(_ps_payload(built_repo, command), "strict") is None


def test_select_string_scoped_to_one_file_is_allowed(built_repo: Path) -> None:
    out = handle_pre_tool_use(
        _ps_payload(built_repo, "Select-String -Pattern target_symbol -Path alpha.py"), "strict"
    )

    assert out is None or "permissionDecision" not in out.get("hookSpecificOutput", {})


def test_filename_lookups_are_never_denied(built_repo: Path) -> None:
    """The contract explicitly allows filename lookups -- `find -name` is not a
    cross-file content search and must not be treated as one."""
    out = handle_pre_tool_use(_bash_payload(built_repo, "find . -name 'target_symbol*'"), "strict")

    assert out is None or "permissionDecision" not in out.get("hookSpecificOutput", {})


# ------------------------------------------- literal searches the gate refused (#72) --
# The strict gate refused searches for directory names, filenames and plain words
# because one fragment of the pattern matched a node in the graph. Each mechanism
# below has its own arm and its own deny control: an arm that starts to allow by
# removing the rule, instead of repairing it, turns a control red.


def _decision(out: dict | None) -> str:
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision", "allow")


@pytest.fixture(autouse=True)
def _no_ambient_search_options(monkeypatch) -> None:
    """The gate reads these from its environment. A developer's own ripgrep
    config must not decide what the arms below assert."""
    for name in ("RIPGREP_CONFIG_PATH", "GREP_OPTIONS", "CDPATH"):
        monkeypatch.delenv(name, raising=False)


_LITERAL_REPO_FILES = {
    "src/pkg/__init__.py": "",
    "src/pkg/core.py": (
        "import json\n\n\n"
        "def dependents(x):\n    return json.dumps(x)\n\n\n"
        "def check():\n    return dependents(1)\n\n\n"
        "def run():\n    return check()\n\n\n"
        "def done():\n    return run()\n\n\n"
        "def out():\n    return done()\n\n\n"
        "def graph():\n    return out()\n"
    ),
    "src/pkg/channel.py": "class Ledger:\n    pass\n\n\ndef round(x):\n    return x\n",
    "tests/test_consume.py": "class Out:\n    pass\n\n\ndef test_x():\n    return Out()\n",
    "examples/demo.py": "VALUE = 1\n",
    "docs/schemas/round.schema.json": '{"title": "round"}\n',
    "docs/notes.md": "A round is one message.\n",
}


@pytest.fixture(scope="module")
def literal_repo(tmp_path_factory) -> Path:
    """Symbols named like ordinary words, a test-local class, docs with no code.

    Built once: the arms below only read it, and there are several dozen. A
    module fixture is set up before the per-test state isolation in conftest,
    so the build gets its own state directory here.
    """
    from graphite import activation
    from graphite.cli import main

    root = tmp_path_factory.mktemp("literal-repo")
    for rel, text in _LITERAL_REPO_FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(activation.ENV_STATE_DIR, str(tmp_path_factory.mktemp("literal-repo-state")))
        patch.chdir(root)  # cmd_build writes cfg.output_dir relative to CWD
        assert main(["build", "."]) == 0
    return root


def test_a_real_function_is_still_refused_over_a_source_directory(literal_repo: Path) -> None:
    """The deny control every arm below is read against (round 312's `dependents`)."""
    out = handle_pre_tool_use(_grep_payload(literal_repo, "dependents", path="src/"), "strict")

    assert _decision(out) == "deny"
    assert "src_pkg_core_py_dependents" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_an_import_placeholder_is_not_a_symbol_of_this_repo(literal_repo: Path) -> None:
    """`json` is in the graph only as the target of `import json`: a placeholder
    with no kind and no source file. Nothing in this repository defines it, so
    the graph has no callers to offer and the search is for a plain word."""
    out = handle_pre_tool_use(_grep_payload(literal_repo, "json", path="src/"), "strict")

    assert _decision(out) == "allow"


def test_an_unresolved_call_placeholder_is_not_a_symbol_either(literal_repo: Path) -> None:
    """`json.dumps(x)` leaves a second placeholder, named by its own id."""
    out = handle_pre_tool_use(
        _grep_payload(literal_repo, "src_pkg_core_py_json_dumps", path="src/"), "strict"
    )

    assert _decision(out) == "allow"


def test_a_directory_with_no_code_is_searched_as_text(literal_repo: Path) -> None:
    """`round` is a function in `src/`. `docs/` holds JSON and Markdown only: no
    definition and no call site lives there, so the graph has nothing to say
    about a word found in it."""
    out = handle_pre_tool_use(_grep_payload(literal_repo, "round", path="docs/"), "strict")

    assert _decision(out) == "allow"


@pytest.mark.parametrize("path", ["src/", "examples/", ".", None])
def test_the_same_word_is_still_refused_where_code_lives(literal_repo: Path, path: str | None) -> None:
    """`examples/` holds one Python file that defines nothing: still code the
    graph models. No path at all is the whole repository."""
    tool_input = {} if path is None else {"path": path}
    out = handle_pre_tool_use(_grep_payload(literal_repo, "round", **tool_input), "strict")

    assert _decision(out) == "deny", path


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("grep -rn round docs/", "allow"),
        ("grep -rn round docs/schemas docs/notes.md", "allow"),
        ("grep -rn round docs/ src/", "deny"),
        ("grep -rn round docs/ examples/demo.py src/pkg", "deny"),
    ],
)
def test_every_directory_searched_must_be_free_of_code(
    literal_repo: Path, command: str, expected: str
) -> None:
    out = handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")

    assert _decision(out) == expected, command


@pytest.mark.parametrize(
    "command",
    ["grep -rn round src/*.py", "grep -rn round $DOCS", "grep -rn round docs/*.md", "grep -rn round nosuchdir/"],
)
def test_a_path_that_does_not_exist_gets_no_exemption(literal_repo: Path, command: str) -> None:
    """A glob or a shell variable reaches the hook unexpanded, so what it will
    match is unknown. Reading "no code under a path that is not there" as "no
    code" would let `src/*.py` through."""
    out = handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")

    assert _decision(out) == "deny", command


def test_a_directory_outside_the_repo_holds_none_of_its_code(
    literal_repo: Path, tmp_path_factory
) -> None:
    """Searched together with `docs/`, a scratch directory adds no code."""
    outside = tmp_path_factory.mktemp("outside-the-repo")
    (outside / "logs").mkdir()

    command = f"grep -rn round {(outside / 'logs').as_posix()} docs/"
    out = handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")

    assert _decision(out) == "allow"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # a directory the repository lies under holds all of its code
        ("grep -rn round docs/ ..", "deny"),
        ("grep -rn round docs/ ../..", "deny"),
        ("grep -rn round docs/ {parent}", "deny"),
        ("grep -rn round ..", "deny"),
        ("grep -rn round {parent}", "deny"),
        ("rg round ../..", "deny"),
        # a directory beside the repository holds none of it
        ("grep -rn round docs/ ../{beside}", "allow"),
        ("grep -rn round ../{beside}", "allow"),
        ("grep -rn round {parent}/{beside}", "allow"),
    ],
)
def test_a_directory_above_the_repository_is_not_outside_it(
    literal_repo: Path, tmp_path_factory, command: str, expected: str
) -> None:
    """`..` holds the whole repository, so a search rooted there is a
    whole-repo search. Read as "outside", it let a symbol search through, alone
    and beside a directory with no code in it."""
    beside = literal_repo.parent / "beside-the-repo"
    beside.mkdir(exist_ok=True)
    command = command.format(parent=literal_repo.parent.as_posix(), beside=beside.name)

    out = handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")

    assert _decision(out) == expected, command


def test_the_grep_tool_cannot_search_from_above_the_repository_either(literal_repo: Path) -> None:
    out = handle_pre_tool_use(_grep_payload(literal_repo, "round", path=".."), "strict")

    assert _decision(out) == "deny"


def test_a_path_is_placed_against_the_repository_case_folded(tmp_path: Path) -> None:
    """A case-insensitive filesystem opens `/USERS/ME/REPO/src` as the repo's
    own `src`. Compared exactly, that spelling read as a directory elsewhere.
    The repository here does not exist, so no filesystem corrects the case and
    the arm means the same on every platform."""
    from graphite.agent_hooks import _repo_scope

    repo = tmp_path.resolve() / "nope" / "repo"
    other_case = Path(str(repo).swapcase())

    assert _repo_scope(repo, other_case / "SRC" / "Pkg") == "src/pkg"
    assert _repo_scope(repo, other_case) == "."
    assert _repo_scope(repo, other_case.parent) == "."
    assert _repo_scope(repo, other_case.parent / "other") is None
    assert _repo_scope(repo, other_case.parent / "repository") is None  # shares a prefix, not a parent
    assert _repo_scope(repo, other_case.parent / "re") is None  # a prefix of the name, not a directory above


def test_a_definition_in_a_file_of_no_known_language_still_counts_as_code(tmp_path: Path) -> None:
    """Which files hold code is read from the extension, and a document has a
    file node and nothing else. If the graph ever holds a function in a file
    whose extension is not listed, the directory is not free of code."""
    from graphite.agent_hooks import _holds_no_code
    from graphite.graph import build_graph

    (tmp_path / "bin").mkdir()
    (tmp_path / "docs").mkdir()
    graph = build_graph(
        [
            {"id": "bin_tool", "kind": "file", "name": "tool", "source_file": "bin/tool"},
            {"id": "bin_tool_main", "kind": "function", "name": "main", "source_file": "bin/tool"},
            {"id": "docs_notes_md", "kind": "file", "name": "notes.md", "source_file": "docs/notes.md"},
        ],
        [],
    )

    assert _holds_no_code(tmp_path, ["bin"], graph) is False
    assert _holds_no_code(tmp_path, ["docs"], graph) is True


def test_a_code_directory_named_in_another_case_still_counts_its_code(
    literal_repo: Path, tmp_path: Path
) -> None:
    """A case-insensitive filesystem opens `SRC/` as `src/`. The graph records
    `src/pkg/...`, so comparing the two spellings exactly would find "no code"
    under a directory full of it. The directory is renamed, in a copy, so the
    arm means the same thing on a case-sensitive filesystem."""
    repo = tmp_path / "repo"
    shutil.copytree(literal_repo, repo)
    (repo / "src").rename(repo / "SRC")

    out = handle_pre_tool_use(_bash_payload(repo, "grep -rn round SRC/"), "strict")

    assert _decision(out) == "deny"


def test_a_word_in_another_case_is_not_the_symbol(literal_repo: Path) -> None:
    """`DONE` in a log line is not the function `done`. The Grep tool matches
    case exactly unless told otherwise, so the search cannot even find the
    function: it is a search for a word."""
    out = handle_pre_tool_use(_grep_payload(literal_repo, "DONE", path="src/"), "strict")

    assert _decision(out) == "allow"


def test_a_grep_that_ignores_case_matches_the_symbol_in_any_case(literal_repo: Path) -> None:
    payload = _grep_payload(literal_repo, "DONE", path="src/", **{"-i": True})

    out = handle_pre_tool_use(payload, "strict")

    assert _decision(out) == "deny"
    assert "src_pkg_core_py_done" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        # the pattern itself turns folding on, so the search finds `done`
        ("(?i)DONE", "deny"),
        ("(?si)DONE", "deny"),
        ("(?i:DONE)", "deny"),
        # a flag group that does not: `s` only, or `i` switched off
        ("(?s)DONE", "allow"),
        ("(?-i)DONE", "allow"),
    ],
)
def test_an_inline_flag_folds_case_for_the_grep_tool_as_well(
    literal_repo: Path, pattern: str, expected: str
) -> None:
    """Measured on the Grep tool: `ledger` does not find `class Ledger:`, and
    `(?i)ledger` does. The inline flag was read on the shell route only, so the
    same search passed here and was refused there."""
    out = handle_pre_tool_use(_grep_payload(literal_repo, pattern, path="src/"), "strict")

    assert _decision(out) == expected, pattern


@pytest.mark.parametrize(("flag", "expected"), [("true", "deny"), (1, "deny"), (False, "allow"), (None, "allow")])
def test_a_case_flag_the_gate_cannot_read_is_taken_as_folding(
    literal_repo: Path, flag: object, expected: str
) -> None:
    """`-i` is a boolean. Any other value that is not plainly "off" folds:
    unsure refuses more, never less."""
    payload = _grep_payload(literal_repo, "DONE", path="src/", **{"-i": flag})

    out = handle_pre_tool_use(payload, "strict")

    assert _decision(out) == expected, flag


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # the search matches case exactly, and so does the symbol check
        ("grep -rn DONE src/", "allow"),
        ("grep -rn -E 'group\\]Run .*finished' src/", "allow"),
        ("rg DONE src/", "allow"),
        ("git grep DONE", "allow"),
        ("grep -rn -- DONE src/", "allow"),  # `--` ends the options; it abbreviates none
        ("grep -rn ledger src/", "allow"),  # the class is `Ledger`
        ("grep -rn done src/", "deny"),
        ("grep -rn Ledger src/", "deny"),
        ("grep -rn 'Done\\|done' src/", "deny"),
        # the search folds case, so a symbol in any case is what it would find
        ("grep -rni DONE src/", "deny"),
        ("grep -rn -i DONE src/", "deny"),
        ("grep -rn --ignore-case DONE src/", "deny"),
        ("grep -rn --ignore DONE src/", "deny"),
        ("grep -rny DONE src/", "deny"),
        ("grep -rnP '(?i)DONE' src/", "deny"),
        ("git grep -i DONE", "deny"),
        ("rg -i DONE src/", "deny"),
        ("rg --smart-case DONE src/", "deny"),
        ("rg -S DONE src/", "deny"),
        # tools whose case rules are not modelled fold, as every tool did before
        ("ag DONE src/", "deny"),
        ("ack DONE src/", "deny"),
    ],
)
def test_the_symbol_check_folds_case_only_when_the_search_does(
    literal_repo: Path, command: str, expected: str
) -> None:
    """Folding is a property of the search, not of the gate. Where it is not
    known, the gate folds: that refuses more, never less."""
    out = handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")

    assert _decision(out) == expected, command


@pytest.mark.parametrize(
    "command",
    [
        "Select-String -Pattern DONE -Path src",
        "sls DONE src",
        "Select-String -Pattern DONE -Path src -CaseSensitive",
    ],
)
def test_select_string_is_treated_as_folding_case(literal_repo: Path, command: str) -> None:
    """`Select-String` ignores case unless told otherwise. `-CaseSensitive` is
    not modelled, so that search keeps the refusal it had."""
    out = handle_pre_tool_use(_ps_payload(literal_repo, command), "strict")

    assert _decision(out) == "deny", command


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        # each bare word is a function in `src/`, so each is refused on its own
        ("graph", "deny"),
        ("out", "deny"),
        ("check", "deny"),
        # kebab-case names and command-line options
        ("graph-out", "allow"),
        ("--check", "allow"),
        ("pre-check-run", "allow"),
        # a path
        ("src/pkg/check", "allow"),
        ("graph/", "allow"),
        ("check\\/run", "allow"),
        # a file's name, with the dot escaped or not
        ("graph\\.json", "allow"),
        ("graph.json", "allow"),
        ("check\\.py", "allow"),
        ("zzqx|graph\\.json", "allow"),
        ("dependents|graph\\.json", "deny"),
        # not a literal: the hyphen is optional or part of an arrow, so the text
        # this finds still includes the bare name
        ("check-?", "deny"),
        ("check->", "deny"),
        ("->check", "deny"),
        # member access, not a file's name. `attr` is no symbol, so only the
        # stem `dependents` can refuse the first of these
        ("dependents\\.attr", "deny"),
        ("dependents\\.run", "deny"),
        ("self\\.check\\(", "deny"),
        ("graph\\.jsonify", "deny"),
    ],
)
def test_a_fragment_of_a_literal_is_not_judged_alone(
    literal_repo: Path, pattern: str, expected: str
) -> None:
    """`graph-out` is a directory and `graph\\.json` a file. Neither is the
    function `graph`, though each contains its name. A fragment is skipped only
    where the text matched must carry the joining character, so the search
    cannot be finding the bare symbol."""
    out = handle_pre_tool_use(_grep_payload(literal_repo, pattern, path="src/"), "strict")

    assert _decision(out) == expected, pattern


# ------------------------------------------- options the command line does not show --
# Measured: `GREP_OPTIONS=-i grep ledger` finds `class Ledger:` with the grep
# Git Bash ships, and `rg ledger` finds it once `RIPGREP_CONFIG_PATH` names a
# file holding `--ignore-case`. Neither shows in the command the hook reads.


@pytest.mark.parametrize(
    ("variable", "value", "command", "expected"),
    [
        ("RIPGREP_CONFIG_PATH", "ripgreprc", "rg DONE src/", "deny"),
        ("RIPGREP_CONFIG_PATH", "ripgreprc", "grep -rn DONE src/", "allow"),
        ("RIPGREP_CONFIG_PATH", "", "rg DONE src/", "allow"),  # ripgrep reads no config for an empty value
        ("GREP_OPTIONS", "-i", "grep -rn DONE src/", "deny"),
        ("GREP_OPTIONS", "-i", "egrep -rn DONE src/", "deny"),
        ("GREP_OPTIONS", "-i", "fgrep -rn DONE src/", "deny"),
        ("GREP_OPTIONS", "-i", "rg DONE src/", "allow"),
        ("GREP_OPTIONS", "", "grep -rn DONE src/", "allow"),
    ],
)
def test_a_search_tool_configured_from_the_environment_is_taken_as_folding(
    literal_repo: Path, monkeypatch, variable: str, value: str, command: str, expected: str
) -> None:
    """What the configuration says is not read. That it exists is enough to be
    unsure, and unsure folds."""
    monkeypatch.setenv(variable, value)

    out = handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")

    assert _decision(out) == expected, (variable, command)


@pytest.mark.parametrize(("variable", "expected"), [("RIPGREP_CONFIG_PATH", "deny"), ("GREP_OPTIONS", "allow")])
def test_the_grep_tool_is_taken_as_folding_while_ripgrep_is_configured(
    literal_repo: Path, monkeypatch, variable: str, expected: str
) -> None:
    """The Grep tool is built on ripgrep. Whether it reads ripgrep's config
    file was not measured, so while the variable is set it is not known to
    match case exactly, and unsure folds."""
    monkeypatch.setenv(variable, "set")

    out = handle_pre_tool_use(_grep_payload(literal_repo, "DONE", path="src/"), "strict")

    assert _decision(out) == expected, variable


def test_a_cd_is_not_followed_while_cdpath_is_set(literal_repo: Path, monkeypatch) -> None:
    """With `CDPATH` set, `cd docs` may land in a `docs` under one of its
    entries instead of `./docs`."""
    command = "cd docs && grep -rn round ."
    assert _decision(handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")) == "allow"

    monkeypatch.setenv("CDPATH", str(literal_repo / "src"))

    assert _decision(handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")) == "deny"


# ------------------------------------------------ more than one search in a command --
# Only the first search of a command line was judged. Once a first search could
# earn an exemption, any search could ride through behind it.


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("grep -rn round docs/ ; grep -rn round src/", "deny"),
        ("grep -rn round docs/ && grep -rn round src/", "deny"),
        ("grep -rn round docs/ || rg round", "deny"),
        ("grep zzqx docs/notes.md ; grep -rn dependents src/", "deny"),
        ("grep -e zzqx docs/notes.md ; grep -rn dependents src/", "deny"),
        ("grep -rn zzqx src/ ; grep -rn zzqy src/ ; grep -rn dependents src/", "deny"),
        ("grep -rn dependents src/ ; grep zzqx docs/notes.md", "deny"),
        ("cd docs && grep -rn round . && grep -rn round ../src", "deny"),
        # a first command that is no search to judge: no pattern, an empty one, no word in it
        ("grep -c ; grep -rn dependents src/", "deny"),
        ("grep -rn '' src/ ; grep -rn dependents src/", "deny"),
        ("grep -rn '[0-9]+' src/ ; grep -rn dependents src/", "deny"),
        # every search passes on its own
        ("grep -rn round docs/ ; grep -rn round docs/schemas", "allow"),
        ("grep -n round docs/notes.md ; grep -rn zzqx src/", "allow"),
        ("cd docs && grep -rn round . && rg round schemas", "allow"),
    ],
)
def test_every_search_in_a_command_is_judged(literal_repo: Path, command: str, expected: str) -> None:
    out = handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")

    assert _decision(out) == expected, command


def test_a_search_outside_the_repository_does_not_end_the_judging(
    literal_repo: Path, scratch: Path
) -> None:
    command = f"grep -rn check {scratch.as_posix()} ; grep -rn check src/"

    out = handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")

    assert _decision(out) == "deny"


def test_the_refusal_names_the_search_that_earned_it(literal_repo: Path) -> None:
    command = "grep -rn round docs/ ; grep -rn dependents src/"

    out = handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")

    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "src_pkg_core_py_dependents" in reason
    assert "'round'" not in reason


def test_the_graph_is_loaded_once_for_a_command_of_several_searches(
    literal_repo: Path, monkeypatch
) -> None:
    """Each search here reaches the graph: `round` is a symbol, and only the
    graph can say that `docs/` holds no code."""
    from graphite import agent_hooks

    loads: list[Path] = []
    real = agent_hooks._hook_graph

    def counting(root: Path):
        loads.append(root)
        return real(root)

    monkeypatch.setattr(agent_hooks, "_hook_graph", counting)
    command = "grep -rn round docs/ ; grep -rn round docs/schemas ; grep -rn round docs/"

    out = handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")

    assert _decision(out) == "allow"
    assert len(loads) == 1


# --------------------------------------------- a search after `cd` (channel round 318) --
# `cd <scratch> && grep check ci.log` reads one file outside the repository. The
# hook resolved `ci.log` against the repository root, found no such file there,
# and refused the search for a symbol named `check`.


@pytest.fixture()
def scratch(tmp_path_factory) -> Path:
    outside = tmp_path_factory.mktemp("outside-the-repo")
    (outside / "ci.log").write_text("##[group]Run pkg check --all\n", encoding="utf-8")
    return outside


@pytest.mark.parametrize(
    "command",
    [
        "cd {out} && grep -n check ci.log",
        "cd {out} && grep -n check ./ci.log",
        'cd "{out}" && grep -n check ci.log',
        "cd {out} && grep -rn check .",
        "cd {out} && rg check",
        "pushd {out} && grep -n check ci.log",
        "cd src && grep -n check pkg/core.py",
        "cd docs && grep -rn round schemas",
        "cd docs && cd schemas && grep -rn round .",
        "cd docs && rg round",
        # an assignment and a search leave the shell where it was
        "cd docs && LC_ALL=C && grep -rn round .",
        "cd docs && grep -c zzqx notes.md && grep -rn round .",
    ],
)
def test_a_path_after_cd_is_found_where_the_shell_will_find_it(
    literal_repo: Path, scratch: Path, command: str
) -> None:
    command = command.format(out=scratch.as_posix())

    out = handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")

    assert _decision(out) == "allow", command


@pytest.mark.parametrize(
    "command",
    [
        # the directory moved into holds code, or the path leads back into the repo
        "cd src && grep -rn check .",
        "cd src && rg check",
        "cd {out} && grep -rn check {repo}/src",
        "cd {out} && cd {repo} && grep -rn check src",
        # the search runs whether or not the `cd` worked
        "cd {out} ; grep -n check ci.log",
        "cd {out} || grep -n check ci.log",
        "cd {out} & grep -n check ci.log",
        "cd {out} | cat && grep -n check ci.log",
        "cd {out} && ls ; grep -n check ci.log",
        # where the `cd` leads is not known to the hook
        "cd $OUT && grep -rn check .",
        "cd ~ && grep -rn check .",
        "cd nosuchdir && grep -rn check .",
        "cd {out}/nosuchdir && grep -n check ci.log",
        "cd -P {out} && grep -rn check .",
        "cd {out} extra && grep -rn check .",
        "cd {out} && cd - && grep -rn check .",
        # a command the hook does not model may have moved the shell again
        "pushd docs && popd && grep -rn round .",
        "cd docs && builtin cd .. && grep -rn round .",
        "cd docs && source env.sh && grep -rn round .",
        "cd docs && ls && grep -rn round .",
        "cd {out} && ls && grep -n check ci.log",
        "cd {out} && ls | sort && grep -n check ci.log",
    ],
)
def test_a_cd_that_cannot_be_trusted_moves_nothing(
    literal_repo: Path, scratch: Path, command: str
) -> None:
    """Following a `cd` must not become a way out of the gate. Wherever the
    hook cannot be sure the search runs in the new directory, paths resolve
    against the repository root, as they did before."""
    command = command.format(out=scratch.as_posix(), repo=literal_repo.as_posix())

    out = handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")

    assert _decision(out) == "deny", command


@pytest.mark.parametrize("name", ["-", "$OUT", "~"])
def test_a_directory_named_like_shell_syntax_is_not_the_cd_target(
    literal_repo: Path, tmp_path: Path, name: str
) -> None:
    """`cd -` goes back, `cd $OUT` and `cd ~` go wherever the shell expands them
    to. A directory in the repository that happens to carry such a name, with no
    code in it, is not where the search runs."""
    repo = tmp_path / "repo"
    shutil.copytree(literal_repo, repo)
    (repo / name).mkdir()

    out = handle_pre_tool_use(_bash_payload(repo, f"cd {name} && grep -rn check ."), "strict")

    assert _decision(out) == "deny"


def test_a_parent_path_after_cd_no_longer_reads_as_outside_the_repository(literal_repo: Path) -> None:
    """The same mistake in the other direction. From `docs/`, `..` is the
    repository root. Resolved against the root itself it was the directory
    above, so a whole-repo search for a symbol read as a search elsewhere and
    passed."""
    out = handle_pre_tool_use(_bash_payload(literal_repo, "cd docs && grep -rn round .."), "strict")

    assert _decision(out) == "deny"


@pytest.mark.parametrize(
    ("text", "msys", "expected"),
    [
        ("/c/Users/x", True, "C:/Users/x"),
        ("/F/Projects", True, "F:/Projects"),
        ("/c", True, "C:/"),
        ("/cache/x", True, "/cache/x"),  # a directory named `cache`, not drive C
        ("src/pkg", True, "src/pkg"),
        ("/c/Users/x", False, "/c/Users/x"),  # PowerShell, or any shell off Windows
    ],
)
def test_a_git_bash_drive_path_is_read_as_the_drive(text: str, msys: bool, expected: str) -> None:
    assert _shell_path(text, msys) == Path(expected)


def _git_bash(path: Path) -> str:
    """`C:\\Users\\x` the way Git Bash spells it: `/c/Users/x`."""
    return f"/{path.drive[0].lower()}{path.as_posix()[2:]}"


@pytest.mark.skipif(os.name != "nt", reason="Git Bash drive paths exist only on Windows")
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd {out} && grep -n check ci.log", "allow"),
        ("grep -n check {out}/ci.log", "allow"),
        ("grep -rn check {out}", "allow"),
        # read as a path on the current drive, this was "outside the repository"
        ("grep -rn check {repo}/src", "deny"),
        ("cd {repo}/src && grep -rn check .", "deny"),
    ],
)
def test_git_bash_drive_paths_are_located_on_windows(
    literal_repo: Path, scratch: Path, command: str, expected: str
) -> None:
    command = command.format(out=_git_bash(scratch), repo=_git_bash(literal_repo))

    out = handle_pre_tool_use(_bash_payload(literal_repo, command), "strict")

    assert _decision(out) == expected, command


@pytest.mark.skipif(os.name != "nt", reason="Git Bash drive paths exist only on Windows")
def test_powershell_does_not_read_a_git_bash_drive_path(literal_repo: Path) -> None:
    """To PowerShell `/f/x` is a directory `f` at the root of the current
    drive. The search keeps the reading it had."""
    command = f"Select-String -Pattern check -Path {_git_bash(literal_repo)}/src"

    out = handle_pre_tool_use(_ps_payload(literal_repo, command), "strict")

    assert _decision(out) == "allow"
