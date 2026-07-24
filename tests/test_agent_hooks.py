"""Tests for the Claude Code agent-hook handlers (fail-open by design)."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from graphite.agent_hooks import handle_pre_tool_use, handle_session_start, handle_stop
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
    code, out = _run_cli_hook(monkeypatch, capsys, ["agent-hook", "stop"], {})
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
