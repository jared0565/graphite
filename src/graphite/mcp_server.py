"""MCP server exposing Graphite graph queries as tools for Claude Code."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError as e:
    print(
        "[graphite-mcp] MCP package not installed. Run: pip install -e 'tools/graphite[mcp]'",
        file=sys.stderr,
    )
    raise

import networkx as nx

from .analyze import analyze
from .graph import graph_from_json
from .query import query


class GraphiteMCPServer:
    """In-memory Graphite graph + optional rebuild for MCP tools."""

    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = (project_root or Path.cwd()).resolve()
        self.graph_json = self.project_root / "graph-out" / "graph.json"
        self.report_md = self.project_root / "graph-out" / "GRAPH_REPORT.md"
        self._g: nx.DiGraph | None = None
        self._load_error: str | None = None

    def _load(self) -> bool:
        if self._g is not None:
            return True
        if not self.graph_json.exists():
            self._load_error = (
                f"No graph found at {self.graph_json}. "
                "Run `python -m graphite build .` or call `graphite_refresh`."
            )
            return False
        try:
            with open(self.graph_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._g = graph_from_json(data)
            self._load_error = None
            return True
        except Exception as e:
            self._load_error = f"Failed to load graph.json: {e}"
            return False

    def refresh(self) -> dict[str, Any]:
        """Rebuild the graph and reload it."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "graphite", "build", str(self.project_root)],
                cwd=self.project_root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to run graphite build: {e}"}
        if result.returncode != 0:
            return {"success": False, "error": result.stderr or result.stdout}
        self._g = None
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


def _result(content: dict[str, Any]) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(content, ensure_ascii=False, indent=2))]


def main() -> int:
    server = Server("graphite")
    graphite = GraphiteMCPServer()

    @server.list_tools()
    async def list_tools() -> list[Tool]:
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
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        if name == "graphite_query":
            return _result(graphite.query_tool(arguments.get("query", "")))
        if name == "graphite_community":
            return _result(graphite.community_tool(arguments.get("node_id", "")))
        if name == "graphite_summary":
            return _result(graphite.summary_tool())
        if name == "graphite_refresh":
            return _result(graphite.refresh())
        return _result({"error": f"Unknown tool: {name}"})

    async def run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    import asyncio
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

