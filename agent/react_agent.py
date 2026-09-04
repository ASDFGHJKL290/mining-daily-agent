"""
Self-written ReAct orchestrator for the "矿权日报" (Mining Rights Daily) agent.

Pipeline (deliberately framework-light, no LangGraph dependency):
  1. PLAN   - an LLM (DeepSeek) turns the user query into a structured plan:
              {company, commodity, days, need_news, need_pdf, need_price}
  2. ACT    - the code executes the plan by calling the three MCP servers
              (news search / NI 43-101 extraction / price+trend).
  3. OBSERVE- every observation is stored verbatim from tool output; the model
              is never allowed to invent numbers.
  4. RENDER - a Markdown briefing (news + resources + prices + risks + sources)
              is assembled from the grounded observations.

The LLM decides *what to gather*; the code decides *how to call tools* and
*how to render*, which keeps the output deterministic and auditable.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from agent.llm import DeepSeekClient
from agent.mcp_client import MCPClientManager

PLAN_SYSTEM_PROMPT = """You are the planner of a mining-daily-briefing agent.
Given a user request about a mining company or commodity, return ONE JSON object
with exactly these keys:
{
  "company": "<company name or null>",
  "commodity": "<one of: copper, zinc, nickel, lithium, iron-ore, gold, silver, tantalum, or null>",
  "days": <int, look-back window for news, default 30>,
  "need_news": true,
  "need_pdf": true,
  "need_price": true
}
Rules:
- Infer the company from the request (e.g. Pilbara, Newmont, Barrick, Rio Tinto...).
- Infer the commodity; map to the allowed list above (lithium carbonate -> lithium).
- If nothing specific is given, default company=Pilbara Minerals,
  commodity=lithium, days=30.
- Respond with ONLY the JSON object, no prose, no markdown fence."""


class MiningDailyAgent:
    def __init__(
        self,
        llm: DeepSeekClient | None = None,
        mcp: MCPClientManager | None = None,
    ):
        self.llm = llm or DeepSeekClient()
        self.mcp = mcp

    # ------------------------------------------------------------------
    # PLAN: let the LLM structure the request
    # ------------------------------------------------------------------

    async def _plan(self, query: str) -> dict[str, Any]:
        """Ask the LLM to turn the raw query into a structured plan."""
        messages = [
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": f"User request: {query}\nReturn the JSON plan."},
        ]
        try:
            response = self.llm.chat(messages, temperature=0.0, max_tokens=512)
            content = response.get("content", "").strip()
            # tolerate markdown-fenced json
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
            # locate first {...}
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                plan = json.loads(match.group(0))
            else:
                plan = json.loads(content)
            return self._normalise_plan(plan)
        except Exception:  # noqa: BLE001 - fall back to deterministic defaults
            return self._fallback_plan(query)

    def _normalise_plan(self, raw: dict[str, Any]) -> dict[str, Any]:
        plan = {
            "company": raw.get("company") or "Pilbara Minerals",
            "commodity": raw.get("commodity") or "lithium",
            "days": int(raw.get("days") or 30),
            "need_news": bool(raw.get("need_news", True)),
            "need_pdf": bool(raw.get("need_pdf", True)),
            "need_price": bool(raw.get("need_price", True)),
        }
        plan["days"] = max(1, min(plan["days"], 365))
        # commodity alias normalisation
        alias = {
            "lithium-carbonate": "lithium",
            "lithium carbonate": "lithium",
            "iron ore": "iron-ore",
            "iron_ore": "iron-ore",
            "铁矿石": "iron-ore",
            "锂": "lithium",
            "铜": "copper",
            "锌": "zinc",
            "镍": "nickel",
            "金": "gold",
        }
        commodity = str(plan["commodity"]).lower().strip()
        plan["commodity"] = alias.get(commodity, commodity)
        return plan

    def _fallback_plan(self, query: str) -> dict[str, Any]:
        """Deterministic plan when the LLM is unreachable (offline demo)."""
        q = query.lower()
        company = "Pilbara Minerals"
        commodity = "lithium"
        for key, name in {
            "pilbara": "Pilbara Minerals",
            "pilgangoora": "Pilbara Minerals",
            "newmont": "Newmont",
            "barrick": "Barrick",
        }.items():
            if key in q:
                company = name
                break
        for key, name in {
            "copper": "copper",
            "铜": "copper",
            "zinc": "zinc",
            "nickel": "nickel",
            "iron ore": "iron-ore",
            "gold": "gold",
        }.items():
            if key in q:
                commodity = name
                break
        return {
            "company": company,
            "commodity": commodity,
            "days": 30,
            "need_news": True,
            "need_pdf": True,
            "need_price": True,
        }

    # ------------------------------------------------------------------
    # ACT + OBSERVE: gather grounded evidence via MCP servers
    # ------------------------------------------------------------------

    async def _gather(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Call the three MCP servers per plan; store tool output verbatim."""
        if self.mcp is None:
            raise RuntimeError("MCPClientManager not attached (call agent.run first)")

        company = plan["company"]
        commodity = plan["commodity"]
        days = plan["days"]
        gathered: dict[str, Any] = {
            "news": [],
            "resource": None,
            "price": None,
            "trend": None,
            "citations": [],
            "notes": "",
        }

        # --- news -----------------------------------------------------
        # 三级回退：公司+商品 -> 仅公司 -> 仅商品(赛道级新闻)。
        # 真实新闻源不一定天天覆盖某家具体公司(如 Pilbara)，但商品赛道
        # (lithium/copper...) 基本总有稿件，保证日报新闻栏始终有据可依。
        if plan.get("need_news", True):
            try:
                search_terms = [f"{company} {commodity}", company]
                commodity_term = _news_keyword(commodity)
                if commodity_term and commodity_term not in search_terms:
                    search_terms.append(commodity_term)
                articles: list[dict[str, Any]] = []
                for term in search_terms:
                    if articles:
                        break
                    raw = await self.mcp.call_tool(
                        "search", {"query": term, "days": days}
                    )
                    articles = (_safe_json(raw).get("articles") or [])[:8]
                gathered["news"] = articles
                for article in articles:
                    gathered["citations"].append(
                        {
                            "type": "news",
                            "title": article.get("title", ""),
                            "url": article.get("url", ""),
                            "source": article.get("source", ""),
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                gathered["notes"] += f"[news] {exc}; "

        # --- NI 43-101 resource PDF ------------------------------------
        if plan.get("need_pdf", True):
            pdf_url = _pick_pdf_url(company)
            try:
                raw = await self.mcp.call_tool(
                    "extract_resources", {"pdf_url": pdf_url}
                )
                data = _safe_json(raw)
                if isinstance(data, dict) and (data.get("resources") or data.get("raw")):
                    gathered["resource"] = data
                    if data.get("report_url"):
                        gathered["citations"].append(
                            {
                                "type": "report",
                                "title": data.get("report_title", ""),
                                "url": data.get("report_url", ""),
                            }
                        )
            except Exception as exc:  # noqa: BLE001
                gathered["notes"] += f"[pdf] {exc}; "

        # --- price + trend ---------------------------------------------
        if plan.get("need_price", True):
            price_symbol = commodity
            if commodity in ("lithium", "锂"):
                price_symbol = "lithium-carbonate"
            try:
                raw_price = await self.mcp.call_tool(
                    "get_price", {"commodity": price_symbol}
                )
                raw_trend = await self.mcp.call_tool(
                    "get_trend", {"commodity": price_symbol, "days": days}
                )
                gathered["price"] = _safe_json(raw_price)
                gathered["trend"] = _safe_json(raw_trend)
            except Exception as exc:  # noqa: BLE001
                gathered["notes"] += f"[price] {exc}; "

        return gathered

    # ------------------------------------------------------------------
    # RENDER: build the Markdown briefing
    # ------------------------------------------------------------------

    def _render_markdown(
        self,
        query: str,
        plan: dict[str, Any],
        gathered: dict[str, Any],
    ) -> str:
        company = plan["company"]
        commodity = plan["commodity"]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        lines: list[str] = []
        lines.append(f"# 矿权日报 · {company}（{commodity}）")
        lines.append("")
        lines.append(f"> 生成时间：{today} UTC  ")
        lines.append(f"> 原始需求：{query}")
        lines.append("")

        # --- 一、新闻摘要 ---
        lines.append("## 一、新闻摘要")
        lines.append("")
        articles = gathered.get("news") or []
        if articles:
            for idx, article in enumerate(articles[:6], start=1):
                title = article.get("title", "（无标题）")
                source = article.get("source", "unknown")
                url = article.get("url", "")
                summary = (article.get("summary") or "")[:240]
                lines.append(f"{idx}. **{title}**  ")
                lines.append(f"   来源：{source}  ")
                lines.append(f"   摘要：{summary}  ")
                lines.append(f"   链接：{url}")
                lines.append("")
        else:
            lines.append("（未检索到相关新闻）")
            lines.append("")

        # --- 二、NI 43-101 储量数据 ---
        lines.append("## 二、NI 43-101 储量数据")
        lines.append("")
        resource = gathered.get("resource") or {}
        resources = resource.get("resources") or []
        if resources:
            lines.append("| 类别 | 矿石量 (Mt) | 品位 | 金属量 |")
            lines.append("|---|---|---|---|")
            for item in resources:
                grade = item.get("grade") or {}
                grade_str = ", ".join(
                    f"{key}={value}" for key, value in grade.items()
                )
                lines.append(
                    f"| {item.get('category', '?')} | {item.get('tonnage_mt', '?')} "
                    f"| {grade_str} | {item.get('contained_metal', '?')} "
                    f"{item.get('metal_unit', '')} |"
                )
            lines.append("")
            if resource.get("report_title"):
                lines.append(f"报告：{resource.get('report_title')}  ")
            if resource.get("report_url"):
                lines.append(f"来源：{resource.get('report_url')}")
            resource_notes = resource.get("notes") or ""
            if "sample" in resource_notes.lower() or "样例" in resource_notes:
                lines.append(f"数据说明：{resource_notes}")
            lines.append("")
        else:
            lines.append(
                "（未能从报告可靠抽取储量数据——本系统**不臆造数字**，该栏待人工审核。）"
            )
            lines.append("")

        # --- 三、价格走势 ---
        lines.append("## 三、价格走势")
        lines.append("")
        price = gathered.get("price") or {}
        trend = gathered.get("trend") or {}
        if price and price.get("price") is not None:
            display_name = price.get("display_name", commodity)
            unit = price.get("unit", "")
            date_str = price.get("date", "?")
            lines.append(f"- **最新价**（{date_str}）：**{price.get('price'):,} {unit}**（{display_name}）")
        else:
            lines.append("- 价格数据暂不可用。")
        if trend and trend.get("points"):
            first = trend.get("first_price")
            last = trend.get("last_price")
            change = trend.get("change_pct")
            if first is not None and last is not None and change is not None:
                direction = "上涨" if change >= 0 else "下跌"
                lines.append(
                    f"- 近 {len(trend.get('points', []))} 个数据点：{first:,.0f} → {last:,.0f} "
                    f"（{direction} {abs(change):.2f}%）"
                )
            if trend.get("min") is not None and trend.get("max") is not None:
                unit = (price or {}).get("unit", "")
                lines.append(
                    f"- 区间：{trend.get('min'):,.0f} ~ {trend.get('max'):,.0f} {unit}"
                )
        if (price or {}).get("is_sample") or (trend or {}).get("is_sample"):
            lines.append(
                "- ⚠️ **数据说明**：当前价格来自内置样例序列（离线演示用），非实时行情。"
            )
        lines.append("")

        # --- 四、风险提示 ---
        lines.append("## 四、风险提示")
        lines.append("")
        risks: list[str] = []
        if commodity in ("lithium", "nickel"):
            risks.append(
                "锂/镍价格受电动车与储能需求、以及新增产能投放节奏影响，波动较大。"
            )
        if commodity == "copper":
            risks.append("铜价受宏观情绪、矿端扰动（罢工/港口/TC 低位）影响显著。")
        if (price or {}).get("is_sample"):
            risks.append(
                "本简报价格数据为样例序列，不构成投资依据，请以交易所实时行情为准。"
            )
        if (resource or {}).get("needs_human_review"):
            risks.append("储量数据未经人工复核，使用前请对照原文 NI 43-101 报告。")
        if not risks:
            risks.append("无特别提示。")
        for risk in risks:
            lines.append(f"- {risk}")
        lines.append("")

        # --- 五、引用来源 ---
        lines.append("## 五、引用来源")
        lines.append("")
        citations = gathered.get("citations") or []
        if citations:
            seen: set[str] = set()
            for idx, cite in enumerate(citations, start=1):
                title = cite.get("title") or cite.get("type", "source")
                url = cite.get("url", "")
                if url in seen:
                    continue
                seen.add(url)
                lines.append(f"{idx}. {title}")
                lines.append(f"   {url}")
            lines.append("")
        else:
            lines.append("（无外部引用）")
            lines.append("")

        lines.append("---")
        lines.append(
            "*由 mining-daily-agent 自动生成：ReAct 编排（DeepSeek 规划）→ "
            "mining-news-mcp / mineral-pdf-mcp / lme-price-mcp 三路 MCP 数据聚合。*"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------

    async def run(self, query: str) -> dict[str, Any]:
        """Full pipeline: PLAN -> ACT/OBSERVE -> RENDER."""
        plan = await self._plan(query)
        gathered = await self._gather(plan)
        report = self._render_markdown(query, plan, gathered)
        return {
            "query": query,
            "plan": plan,
            "report_markdown": report,
            "citations": gathered.get("citations", []),
            "notes": gathered.get("notes", ""),
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _safe_json(raw: str) -> dict[str, Any]:
    """Parse JSON tool output; tolerate fences and stray text."""
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"data": data}
    except json.JSONDecodeError:
        return {"raw": raw}


def _news_keyword(commodity: str) -> str | None:
    """把商品标识映射成新闻检索词（server 做子串匹配，iron-ore 需展开）。"""
    mapping = {
        "lithium": "lithium",
        "copper": "copper",
        "zinc": "zinc",
        "nickel": "nickel",
        "iron-ore": "iron ore",
        "iron_ore": "iron ore",
        "gold": "gold",
        "silver": "silver",
        "tantalum": "tantalum",
    }
    return mapping.get(str(commodity).lower().strip())


def _pick_pdf_url(company: str | None) -> str | None:
    """Map a company to its bundled NI 43-101 sample report URL."""
    mapping = [
        ("pilbara", "https://www.pilbaraminerals.com.au/reports/pilgangoora-ni43-101.pdf"),
        ("newmont", "https://www.newmont.com/reports/nevada-ni43-101.pdf"),
        ("barrick", "https://www.barrick.com/reports/pueblo-viejo-ni43-101.pdf"),
    ]
    if company:
        lowered = company.lower()
        for key, url in mapping:
            if key in lowered:
                return url
    return mapping[0][1]
