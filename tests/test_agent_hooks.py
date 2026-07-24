"""Tests for the Claude Code agent-hook handlers (fail-open by design)."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.agent_hooks import handle_pre_tool_use, handle_session_start


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
