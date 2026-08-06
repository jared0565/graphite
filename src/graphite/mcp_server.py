"""MCP server exposing Graphite graph queries as tools for Claude Code."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError:
    print(
        "[graphite-mcp] MCP package not installed. Run: pip install -e 'tools/graphite[mcp]'",
        file=sys.stderr,
    )
    raise

import networkx as nx

from .analyze import analyze
from .graph_io import GraphReadError, load_validated_graph_bundle
from .query import query


class GraphiteMCPServer:
    """In-memory Graphite graph + optional rebuild for MCP tools."""

    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = (project_root or Path.cwd()).resolve()
        self.graph_json = self.project_root / "graph-out" / "graph.json"
        self.report_md = self.project_root / "graph-out" / "GRAPH_REPORT.md"
        self._g: nx.DiGraph | None = None
        self._load_error: str | None = None
        self._load_attempts = 0

    def _load(self) -> bool:
        if self._g is not None:
            return True
        if self._load_attempts >= 3:
            self._load_error = "Graph unavailable: retry_limit"
            return False
        self._load_attempts += 1
        try:
            _, self._g = load_validated_graph_bundle(
                self.graph_json,
                root=self.project_root,
            )
            self._load_error = None
            self._load_attempts = 0
            return True
        except GraphReadError as exc:
            self._load_error = f"Graph unavailable: {exc.code}"
            return False

    def refresh(self) -> dict[str, Any]:
        """Rebuild the graph and reload it."""
        try:
            result = subprocess.run(
                [sys.executable, "-P", "-m", "graphite", "build", str(self.project_root)],
                cwd=self.project_root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                # graphite forces UTF-8 on redirected output (#17), so decoding
                # its child with the locale codec is wrong on both sides.
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=300,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to run graphite build: {e}"}
        if result.returncode != 0:
            return {"success": False, "error": result.stderr or result.stdout}
        self._g = None
        self._load_attempts = 0
        loaded = self._load()
        return {
            "success": loaded,
            "error": self._load_error,
            "output": result.stdout.strip(),
        }

    def query_tool(self, q: str) -> dict[str, Any]:
        if not self._load():
            return {"error": self._load_error}
        assert self._g is not None
        return query(self._g, q)

    def community_tool(self, node_id: str) -> dict[str, Any]:
        if not self._load():
            return {"error": self._load_error}
        assert self._g is not None
        g = self._g
        if node_id not in g:
            # Try fuzzy match.
            for n in g.nodes():
                if node_id in n or g.nodes[n].get("name", "").lower() == node_id.lower():
                    node_id = n
                    break
            else:
                return {"error": f"Node not found: {node_id}"}
        comm = g.nodes[node_id].get("community")
        members = [n for n in g.nodes() if g.nodes[n].get("community") == comm]
        return {
            "node": node_id,
            "community": comm,
            "size": len(members),
            "members": sorted(members)[:50],
        }

    def summary_tool(self) -> dict[str, Any]:
        if not self._load():
            return {"error": self._load_error}
        assert self._g is not None
        analysis = analyze(self._g, top_n=10)
        return {
            "node_count": self._g.number_of_nodes(),
            "edge_count": self._g.number_of_edges(),
            "density": nx.density(self._g),
            "god_nodes": analysis.get("god_nodes", []),
            "entry_points": analysis.get("entry_points", []),
            "top_files": analysis.get("top_files_by_links", []),
            "surprising_connections": analysis.get("surprising_connections", [])[:5],
        }


    # --- agent channel -----------------------------------------------------
    #
    # The only tools here that write outside the selected project. They exist
    # because some agents are sandboxed to their own workspace and cannot reach
    # the shared channel at all -- so access is mediated rather than granted.
    #
    # None of them take an author. Identity is derived from `self.project_root`,
    # the repo this server was launched in, because every agent commits under
    # the operator's git identity and the trailer is therefore the only answer
    # to "who wrote this".

    def _channel_call(self, fn, *args, **kwargs) -> dict[str, Any]:
        from .channel import ChannelError, require_channel

        try:
            root = require_channel()
            return fn(root, *args, **kwargs)
        except ChannelError as exc:
            return {"error": exc.code, "message": str(exc)}

    def channel_post_tool(
        self,
        *,
        title: str,
        body: str,
        to: list[str] | None = None,
        supersedes: int | None = None,
    ) -> dict[str, Any]:
        from .channel import post_round

        return self._channel_call(
            lambda root: post_round(
                root,
                self.project_root,
                title=title,
                body=body,
                to=to or [],
                supersedes=supersedes,
            )
        )

    def channel_inbox_tool(self) -> dict[str, Any]:
        from .channel import inbox

        return self._channel_call(
            lambda root: {
                "ok": True,
                "rounds": [
                    {
                        "round": entry.number,
                        "title": entry.title,
                        "author": entry.author,
                        "posted": entry.posted,
                        "body": entry.body,
                    }
                    for entry in inbox(root, self.project_root)
                ],
            }
        )

    def channel_status_tool(
        self, *, number: int, status: str, reason: str | None = None
    ) -> dict[str, Any]:
        from .channel import record_status

        return self._channel_call(
            lambda root: record_status(root, self.project_root, number, status, reason=reason)
        )

    def channel_list_tool(self) -> dict[str, Any]:
        from .channel import current_status, list_rounds

        def _run(root):
            entries = list_rounds(root)
            return {
                "ok": True,
                "rounds": [
                    {
                        "round": entry.number,
                        "title": entry.title,
                        "author": entry.author,
                        "to": entry.to,
                        "posted": entry.posted,
                        "legacy": entry.legacy,
                        "status": (
                            (current_status(root, entry.number) or {}).get("status")
                            if entry.number is not None
                            else None
                        ),
                    }
                    for entry in entries
                ],
            }

        return self._channel_call(_run)

    def channel_read_tool(self, *, number: int) -> dict[str, Any]:
        from .channel import current_status, read_round

        def _run(root):
            entry = read_round(root, number)
            return {
                "ok": True,
                "round": entry.number,
                "title": entry.title,
                "author": entry.author,
                "to": entry.to,
                "posted": entry.posted,
                "body": entry.body,
                "status": (current_status(root, entry.number) or {}).get("status"),
            }

        return self._channel_call(_run)


def channel_tool_definitions() -> list[Tool]:
    """Advertised so agents can discover them; a tool nobody can find is the
    problem this was built to solve."""
    return [
        Tool(
            name="graphite_channel_inbox",
            description=(
                "Messages addressed to you that you have not been handed yet. Call this at "
                "the start of a session. Delivery is recorded as they are returned."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="graphite_channel_post",
            description=(
                "Post a new round to the shared agent channel. You are identified by the "
                "repository this server runs in; you cannot post as another agent. Rounds are "
                "immutable -- correct one by posting another with `supersedes`."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "One-line subject"},
                    "body": {"type": "string", "description": "Markdown body"},
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Recipient agent ids, e.g. ['aramid-agent']",
                    },
                    "supersedes": {"type": "integer", "description": "Round this replaces"},
                },
                "required": ["title", "body"],
            },
        ),
        Tool(
            name="graphite_channel_status",
            description=(
                "Update a message's status: acknowledged, blocked (give a reason), done, or "
                "withdrawn if you wrote it. Only the recipient may acknowledge/block/complete."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "enum": ["acknowledged", "blocked", "done", "withdrawn"],
                    },
                    "reason": {"type": "string"},
                },
                "required": ["number", "status"],
            },
        ),
        Tool(
            name="graphite_channel_list",
            description="List every round in the channel with its author and current status.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="graphite_channel_read",
            description="Read one round by number.",
            inputSchema={
                "type": "object",
                "properties": {"number": {"type": "integer"}},
                "required": ["number"],
            },
        ),
    ]


def _result(content: dict[str, Any]) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(content, ensure_ascii=False, indent=2))]


def _tool_definitions() -> list[Tool]:
    return [
        Tool(
            name="graphite_query",
            description="Query the Graphite knowledge graph. Supported queries: depends-on <node>, imported-by <node>, path <a> -> <b>, stats.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Graphite query string, e.g. 'depends-on db.ts'",
                    }
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="graphite_community",
            description="Describe the community/cluster a node belongs to, including fellow members.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "Node id, name, or file path fragment, e.g. 'db.ts'",
                    }
                },
                "required": ["node_id"],
            },
        ),
        Tool(
            name="graphite_summary",
            description="Return high-level graph stats, god nodes, entry points, and top files.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="graphite_refresh",
            description="Rebuild graph-out/graph.json and reload it.",
            inputSchema={"type": "object", "properties": {}},
        ),
        *channel_tool_definitions(),
    ]


def _dispatch(
    graphite: GraphiteMCPServer, name: str, arguments: dict[str, Any]
) -> list[TextContent]:
    if name == "graphite_channel_inbox":
        return _result(graphite.channel_inbox_tool())
    if name == "graphite_channel_post":
        return _result(graphite.channel_post_tool(
            title=arguments.get("title", ""),
            body=arguments.get("body", ""),
            to=arguments.get("to") or [],
            supersedes=arguments.get("supersedes"),
        ))
    if name == "graphite_channel_status":
        return _result(graphite.channel_status_tool(
            number=arguments.get("number", 0),
            status=arguments.get("status", ""),
            reason=arguments.get("reason"),
        ))
    if name == "graphite_channel_list":
        return _result(graphite.channel_list_tool())
    if name == "graphite_channel_read":
        return _result(graphite.channel_read_tool(number=arguments.get("number", 0)))
    if name == "graphite_query":
        return _result(graphite.query_tool(arguments.get("query", "")))
    if name == "graphite_community":
        return _result(graphite.community_tool(arguments.get("node_id", "")))
    if name == "graphite_summary":
        return _result(graphite.summary_tool())
    if name == "graphite_refresh":
        return _result(graphite.refresh())
    return _result({"error": f"Unknown tool: {name}"})


def _register_tool_handlers(server: Any, graphite: GraphiteMCPServer) -> None:
    """Wire tools/list and tools/call across both supported mcp lines.

    mcp 2.x removed the `@server.list_tools()` / `@server.call_tool()`
    decorators in favour of explicit `add_request_handler` registration. Both
    paths serve the same tool definitions and the same dispatch, so the wire
    contract is identical either way.
    """
    if hasattr(server, "list_tools"):  # mcp 1.x decorator API
        @server.list_tools()
        async def list_tools() -> list[Tool]:
            return _tool_definitions()

        @server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
            return _dispatch(graphite, name, arguments)

        return

    from mcp.types import (  # mcp 2.x handler-registration API
        CallToolRequestParams,
        CallToolResult,
        ListToolsResult,
        PaginatedRequestParams,
    )

    async def handle_list_tools(ctx: Any, params: Any) -> ListToolsResult:
        return ListToolsResult(tools=_tool_definitions())

    async def handle_call_tool(ctx: Any, params: Any) -> CallToolResult:
        return CallToolResult(
            content=_dispatch(graphite, params.name, params.arguments or {})
        )

    server.add_request_handler("tools/list", PaginatedRequestParams, handle_list_tools)
    server.add_request_handler("tools/call", CallToolRequestParams, handle_call_tool)


def main() -> int:
    server = Server("graphite")
    graphite = GraphiteMCPServer()
    _register_tool_handlers(server, graphite)

    async def run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    import asyncio
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
