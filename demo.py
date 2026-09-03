"""
One-shot demo: 生成 Pilbara 锂矿今日简报（完整链路）。
Run:  python demo.py   (从项目根目录)
Output: 打印 Markdown 并保存到 outputs/briefing.md
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.llm import DeepSeekClient
from agent.mcp_client import MCPClientManager
from agent.react_agent import MiningDailyAgent

DEFAULT_QUERY = "给我生成一份关于 Pilbara 锂矿的今日简报"


async def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY
    print(f"[1/3] 连接 MCP servers ...")
    async with MCPClientManager() as mcp:
        tools = mcp.list_tools()
        print(f"      发现 {len(tools)} 个工具: {[t['name'] for t in tools]}")
        print(f"[2/3] LLM 规划 + 数据聚合 ...")
        llm = DeepSeekClient()
        agent = MiningDailyAgent(llm=llm, mcp=mcp)
        result = await agent.run(query)
        print(f"[3/3] 生成简报")
        print("=" * 70)
        print(result["report_markdown"])
        print("=" * 70)

        os.makedirs("outputs", exist_ok=True)
        out_path = os.path.join("outputs", "briefing.md")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(result["report_markdown"])
        print(f"\n[ok] 简报已保存: {os.path.abspath(out_path)}")


if __name__ == "__main__":
    asyncio.run(main())
