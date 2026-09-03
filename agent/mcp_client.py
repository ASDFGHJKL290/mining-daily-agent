"""
MCP client manager: spawns the three mining MCP servers over stdio and exposes
a unified tool-invocation surface for the ReAct agent.

Server registry (mirrors ../mcp-config.json so the same servers run under
Claude Desktop / Cursor and under our own agent):
  - mining-news-mcp : servers/mining_news_server.py
  - mineral-pdf-mcp : servers/mineral_pdf_server.py
  - lme-price-mcp   : servers/lme_price_server.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_BIN = sys.executable  # the venv python that is running this agent

# server_name -> script path (relative to project root)
SERVER_SCRIPTS = {
    "mining-news-mcp": "servers/mining_news_server.py",
    "mineral-pdf-mcp": "servers/mineral_pdf_server.py",
    "lme-price-mcp": "servers/lme_price_server.py",
}


class MCPClientManager:
    """Owns stdio subprocesses for each MCP server and their client sessions."""

    def __init__(self, server_scripts: dict[str, str] | None = None):
        self.server_scripts = server_scripts or SERVER_SCRIPTS
        self._exit_stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tools: dict[str, dict[str, Any]] = {}  # tool_name -> metadata
        self._server_of_tool: dict[str, str] = {}  # tool_name -> server_name

    async def __aenter__(self) -> "MCPClientManager":
        await self.connect_all()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._exit_stack.aclose()

    async def connect_all(self) -> None:
        """Start every server script and cache their tool lists."""
        for server_name, script in self.server_scripts.items():
            script_path = os.path.join(PROJECT_ROOT, script)
            params = StdioServerParameters(
                command=PYTHON_BIN,
                args=[script_path],
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            stdio_transport = await self._exit_stack.enter_async_context(
                stdio_client(params)
            )
            read_stream, write_stream = stdio_transport
            session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            self._sessions[server_name] = session

            tools_response = await session.list_tools()
            for tool in tools_response.tools:
                name = tool.name
                self._tools[name] = {
                    "name": name,
                    "description": tool.description or "",
                    "server": server_name,
                    # OpenAI-style schema for the LLM prompt
                    "input_schema": getattr(tool, "inputSchema", None) or {},
                }
                self._server_of_tool[name] = server_name

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the aggregated tool manifest (for the ReAct system prompt)."""
        return [
            {
                "name": meta["name"],
                "description": meta["description"],
                "input_schema": meta["input_schema"],
            }
            for meta in self._tools.values()
        ]

    def tool_servers(self) -> dict[str, str]:
        return dict(self._server_of_tool)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Invoke a tool on whichever server owns it; returns JSON string."""
        server_name = self._server_of_tool.get(tool_name)
        if server_name is None:
            raise KeyError(f"Unknown tool: {tool_name}")
        session = self._sessions[server_name]
        result = await session.call_tool(tool_name, arguments)
        # result.content is a list of ContentBlock; text blocks carry the payload
        texts: list[str] = []
        for block in getattr(result, "content", []):
            if getattr(block, "type", "") == "text":
                texts.append(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        if not texts:
            return json.dumps({"tool": tool_name, "note": "empty result"})
        return "\n".join(texts)
