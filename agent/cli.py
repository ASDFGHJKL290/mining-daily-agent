"""
Mining Daily Agent - CLI entry point.

Usage:
    python -m agent.cli "给我生成一份关于 Pilbara 锂矿的今日简报"
    python -m agent.cli --query "Newmont 金矿 30天简报"
    python -m agent.cli --interactive
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.llm import DeepSeekClient
from agent.mcp_client import MCPClientManager
from agent.react_agent import MiningDailyAgent


async def _run_once(query: str) -> str:
    async with MCPClientManager() as mcp:
        llm = DeepSeekClient()
        agent = MiningDailyAgent(llm=llm, mcp=mcp)
        result = await agent.run(query)
    return result["report_markdown"]


async def _interactive() -> None:
    print("矿权日报 Agent (interactive) — Ctrl+C / 'exit' 退出")
    async with MCPClientManager() as mcp:
        llm = DeepSeekClient()
        agent = MiningDailyAgent(llm=llm, mcp=mcp)
        while True:
            try:
                query = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                break
            query = query.strip()
            if not query or query.lower() in ("exit", "quit"):
                break
            try:
                result = await agent.run(query)
                print("\n" + result["report_markdown"])
            except Exception as exc:  # noqa: BLE001
                print(f"[error] {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mining Daily Agent")
    parser.add_argument("query", nargs="?", help="briefing request, e.g. 'Pilbara 锂矿今日简报'")
    parser.add_argument("--interactive", "-i", action="store_true", help="interactive REPL")
    parser.add_argument("--out", "-o", default=None, help="write markdown to a file")
    args = parser.parse_args()

    if args.interactive:
        asyncio.run(_interactive())
        return

    query = args.query or "给我生成一份关于 Pilbara 锂矿的今日简报"
    report = asyncio.run(_run_once(query))
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"\n[saved] {args.out}")


if __name__ == "__main__":
    main()
