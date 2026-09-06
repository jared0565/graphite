"""Tests for `graphite.mcp_server`, the MCP tool surface over a project graph.

Named after the module so aramid's mutation stage 1 (`tests/test_<module>.py`)
has something to run; `-k mcp_server` collected one test before this file.
`test_channel_mcp.py` covers the channel tools and the deep probes cover the
real stdio server; this file pins the graph tools, the load/retry contract,
the tool table and the dispatch, on a hand-written bundle with no server.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from graphite import mcp_server  # noqa: E402
from graphite.mcp_server import (  # noqa: E402
    GraphiteMCPServer,
    _dispatch,
    _result,
    _tool_definitions,
    channel_tool_definitions,
)


def _bundle() -> dict[str, Any]:
    nodes = [
        {"id": "src_app", "kind": "file", "name": "app.py", "source_file": "src/app.py", "community": 0},
        {"id": "src_db", "kind": "file", "name": "db.py", "source_file": "src/db.py", "community": 0},
        {"id": "src_util", "kind": "file", "name": "util.py", "source_file": "src/util.py", "community": 1},
    ]
    edges = [
        {"source": "src_app", "target": "src_db", "relation": "imports"},
        {"source": "src_app", "target": "src_util", "relation": "imports"},
    ]
    return {
        "nodes": nodes,
        "edges": edges,
        "clusters": [{"id": 0, "members": ["src_app", "src_db"]}, {"id": 1, "members": ["src_util"]}],
        "analysis": {},
        "metadata": {"node_count": 3, "edge_count": 2, "community_count": 2},
    }


@pytest.fixture
def project(tmp_path: Path) -> Path:
    graph = tmp_path / "graph-out" / "graph.json"
    graph.parent.mkdir(parents=True)
    graph.write_text(json.dumps(_bundle()), encoding="utf-8")
    return tmp_path


def _text(content: list[Any]) -> dict[str, Any]:
    (item,) = content
    assert item.type == "text"
    return json.loads(item.text)


# --- loading ------------------------------------------------------------------------


def test_server_resolves_its_paths_under_the_project_root(project: Path) -> None:
    server = GraphiteMCPServer(project)
    assert server.project_root == project.resolve()
    assert server.graph_json == project.resolve() / "graph-out" / "graph.json"
    assert server._load() is True
    assert server._load_error is None


def test_load_reports_a_missing_graph_by_code_and_stops_retrying_after_three_attempts(tmp_path: Path) -> None:
    server = GraphiteMCPServer(tmp_path)
    for _ in range(3):
        assert server._load() is False
        assert server._load_error is not None and server._load_error.startswith("Graph unavailable: ")
        assert str(tmp_path) not in server._load_error
    assert server._load() is False
    assert server._load_error == "Graph unavailable: retry_limit"
    assert server.query_tool("stats") == {"error": "Graph unavailable: retry_limit"}


def test_load_is_cached_once_it_succeeds(project: Path) -> None:
    server = GraphiteMCPServer(project)
    assert server._load() is True
    graph = server._g
    (project / "graph-out" / "graph.json").write_text("{not json", encoding="utf-8")
    assert server._load() is True
    assert server._g is graph


# --- the graph tools -----------------------------------------------------------------------


def test_query_tool_answers_from_the_loaded_graph(project: Path) -> None:
    result = GraphiteMCPServer(project).query_tool("stats")
    assert "error" not in result
    assert (result["node_count"], result["edge_count"], result["community_count"]) == (3, 2, 2)
    assert result["edges_by_relation"] == {"imports": 2}
    json.dumps(result)  # serialisable, as the wire needs


def test_summary_tool_reports_counts_and_analysis_sections(project: Path) -> None:
    summary = GraphiteMCPServer(project).summary_tool()
    assert (summary["node_count"], summary["edge_count"]) == (3, 2)
    assert summary["density"] == pytest.approx(2 / 6)
    for key in ("god_nodes", "entry_points", "top_files", "surprising_connections"):
        assert isinstance(summary[key], list)
    assert len(summary["surprising_connections"]) <= 5


def test_community_tool_resolves_exact_fuzzy_and_named_lookups(project: Path) -> None:
    server = GraphiteMCPServer(project)
    exact = server.community_tool("src_app")
    assert exact == {"node": "src_app", "community": 0, "size": 2, "members": ["src_app", "src_db"]}
    assert server.community_tool("db") == {"node": "src_db", "community": 0, "size": 2, "members": ["src_app", "src_db"]}
    assert server.community_tool("UTIL.PY")["node"] == "src_util"
    assert server.community_tool("nothing-like-this") == {"error": "Node not found: nothing-like-this"}


def test_graph_tools_report_the_load_error_instead_of_raising(tmp_path: Path) -> None:
    server = GraphiteMCPServer(tmp_path)
    for result in (server.query_tool("stats"), server.summary_tool(), server.community_tool("x")):
        assert set(result) == {"error"}
        assert result["error"].startswith("Graph unavailable: ")


# --- refresh --------------------------------------------------------------------------------


def test_refresh_rebuilds_under_dash_p_then_reloads(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append((argv, kwargs))
        return type("Completed", (), {"returncode": 0, "stdout": "built\n", "stderr": ""})()

    monkeypatch.setattr(mcp_server.subprocess, "run", fake_run)
    server = GraphiteMCPServer(project)
    assert server._load() is True
    server._load_attempts = 2  # a refresh must reset the retry budget

    result = server.refresh()
    assert result == {"success": True, "error": None, "output": "built"}
    ((argv, kwargs),) = calls
    assert argv[1:] == ["-P", "-m", "graphite", "build", str(project.resolve())]
    assert kwargs["cwd"] == project.resolve()
    assert kwargs["encoding"] == "utf-8" and kwargs["check"] is False
    assert kwargs["stdin"] is mcp_server.subprocess.DEVNULL
    assert server._load_attempts == 0


def test_refresh_reports_a_failed_build_without_reloading(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_run(argv: list[str], **kwargs: Any) -> Any:
        return type("Completed", (), {"returncode": 2, "stdout": "", "stderr": "build exploded"})()

    monkeypatch.setattr(mcp_server.subprocess, "run", failing_run)
    server = GraphiteMCPServer(project)
    assert server.refresh() == {"success": False, "error": "build exploded"}

    def unstartable(argv: list[str], **kwargs: Any) -> Any:
        raise OSError("no interpreter")

    monkeypatch.setattr(mcp_server.subprocess, "run", unstartable)
    assert server.refresh() == {"success": False, "error": "Failed to run graphite build: no interpreter"}


# --- the tool table and dispatch ---------------------------------------------------------------


def test_tool_table_advertises_the_four_graph_tools_and_every_channel_tool() -> None:
    names = [tool.name for tool in _tool_definitions()]
    assert names[:4] == ["graphite_query", "graphite_community", "graphite_summary", "graphite_refresh"]
    assert names[4:] == [tool.name for tool in channel_tool_definitions()]
    assert len(names) == len(set(names))
    by_name = {tool.name: tool for tool in _tool_definitions()}
    # mcp 1.x names the field `inputSchema`, 2.x `input_schema`; either works here.
    schema = lambda tool: getattr(tool, "input_schema", None) or tool.inputSchema  # noqa: E731
    assert schema(by_name["graphite_query"])["required"] == ["query"]
    assert schema(by_name["graphite_community"])["required"] == ["node_id"]


def test_result_wraps_content_as_one_pretty_utf8_text_block() -> None:
    (item,) = _result({"k": "é"})
    assert item.type == "text"
    assert item.text == json.dumps({"k": "é"}, ensure_ascii=False, indent=2)


def test_dispatch_routes_each_graph_tool_and_rejects_unknown_names(project: Path) -> None:
    server = GraphiteMCPServer(project)
    assert _text(_dispatch(server, "graphite_summary", {}))["node_count"] == 3
    assert _text(_dispatch(server, "graphite_community", {"node_id": "src_util"}))["community"] == 1
    assert "error" not in _text(_dispatch(server, "graphite_query", {"query": "stats"}))
    # An absent node_id is the empty string, which is a substring of every id:
    # the fuzzy match lands on the first node rather than reporting not-found.
    assert _text(_dispatch(server, "graphite_community", {}))["node"] == "src_app"
    assert _text(_dispatch(server, "graphite_community", {"node_id": "zzz"})) == {"error": "Node not found: zzz"}
    assert _text(_dispatch(server, "no_such_tool", {})) == {"error": "Unknown tool: no_such_tool"}


def test_dispatch_routes_refresh_through_the_server(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = GraphiteMCPServer(project)
    monkeypatch.setattr(server, "refresh", lambda: {"success": True, "error": None, "output": "ok"})
    assert _text(_dispatch(server, "graphite_refresh", {})) == {"success": True, "error": None, "output": "ok"}
