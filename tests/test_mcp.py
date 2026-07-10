"""Smoke tests for the Graphite MCP server tools."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path


def _with_local_graphite_src() -> dict[str, str]:
    env = os.environ.copy()
    src = Path(__file__).resolve().parents[1] / "src"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(src) if not existing else f"{src}{os.pathsep}{existing}"
    return env


def _call_tools(cwd: Path) -> list[dict]:
    proc = subprocess.Popen(
        [sys.executable, "-B", "-m", "graphite.mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd),
        env=_with_local_graphite_src(),
    )

    messages: "queue.Queue[dict]" = queue.Queue()
    pending: dict[int, dict] = {}

    def read_stdout() -> None:
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                messages.put({"error": "mcp server closed stdout"})
                return
            messages.put(json.loads(line))

    threading.Thread(target=read_stdout, daemon=True).start()

    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv(expected_id: int, timeout: float = 10.0) -> dict:
        if expected_id in pending:
            return pending.pop(expected_id)
        try:
            while True:
                msg = messages.get(timeout=timeout)
                msg_id = msg.get("id")
                if msg_id == expected_id:
                    return msg
                if isinstance(msg_id, int):
                    pending[msg_id] = msg
        except queue.Empty:
            proc.terminate()
            try:
                _, stderr = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, stderr = proc.communicate(timeout=3)
            raise AssertionError(f"timed out waiting for MCP response id={expected_id}; stderr={stderr}")

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
        )
        init = recv(1)
        assert init["result"]["serverInfo"]["name"] == "graphite"

        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = recv(2)
        tool_names = {t["name"] for t in tools["result"]["tools"]}
        assert tool_names >= {"graphite_query", "graphite_summary", "graphite_community", "graphite_refresh"}

        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "graphite_refresh", "arguments": {}}})
        refresh_result = recv(3, timeout=30.0)
        refresh_text = refresh_result["result"]["content"][0]["text"]
        refresh_data = json.loads(refresh_text)
        assert refresh_data["success"] is True, refresh_data

        send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "graphite_query", "arguments": {"query": "stats"}},
            }
        )
        query_result = recv(4)

        send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "graphite_summary", "arguments": {}}})
        summary_result = recv(5)

        return [query_result, summary_result]
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_mcp_tools(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "math.ts").write_text(
        "export function add(a: number, b: number): number {\n  return a + b;\n}\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "app.ts").write_text(
        "import { add } from './math';\n\nexport const total = add(1, 2);\n",
        encoding="utf-8",
    )

    results = _call_tools(tmp_path)
    query_text = results[0]["result"]["content"][0]["text"]
    summary_text = results[1]["result"]["content"][0]["text"]
    query_data = json.loads(query_text)
    summary_data = json.loads(summary_text)

    assert "node_count" in query_data
    assert query_data["node_count"] > 0
    assert "node_count" in summary_data
    assert "edge_count" in summary_data
    assert "god_nodes" in summary_data


