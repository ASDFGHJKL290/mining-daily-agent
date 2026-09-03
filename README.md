# ⛏️ mining-daily-agent — 矿权日报 Agent

> 24h 工程题：基于 MCP (Model Context Protocol) 的矿权日报 Agent。
> 3 个 MCP server + 1 个自写 ReAct 编排 client，输入一句自然语言，输出一份 Markdown 日报
> （新闻摘要 + NI 43-101 储量数据 + 价格走势 + 风险提示 + 引用源链接）。

## 架构总览

```
                 ┌──────────────────────────────────────────────┐
  用户请求        │               agent/  (ReAct orchestrator)    │
 "Pilbara锂矿    │  ┌──────────┐   ┌──────────┐   ┌──────────┐  │
  今日简报" ───▶ │  │ PLAN     │──▶│ ACT      │──▶│ OBSERVE  │  │
                 │  │ LLM 规划  │   │ 调 MCP   │   │ 存证输出  │  │
                 │  └──────────┘   └────┬─────┘   └────┬─────┘  │
                 └──────────────────────┼──────────────┼────────┘
                                        │ stdio(MCP)   │
        ┌───────────────┬───────────────┼──────────────┼───────────────┐
        ▼               ▼               ▼              ▼               ▼
 ┌──────────────┐ ┌──────────────┐ ┌──────────────┐          ┌──────────────┐
 │mining-news-  │ │mineral-pdf-  │ │lme-price-    │          │  outputs/    │
 │mcp           │ │mcp           │ │mcp           │          │ briefing.md  │
 │search        │ │extract_      │ │get_price     │          │ (Markdown)   │
 │fetch_article │ │resources     │ │get_trend     │          └──────────────┘
 └──────────────┘ └──────────────┘ └──────────────┘
     新闻聚合           NI 43-101 PDF           LME/SHFE 价格
     (RSS+全文)         储量抽取 (Mt/g/t/oz)      (行情+趋势)
```

## 目录结构

```
mining-daily-agent/
├── agent/
│   ├── cli.py               # CLI 入口（单次/交互）
│   ├── mcp_client.py        # MCP stdio client 管理器（起 3 server + 统一调用）
│   ├── react_agent.py       # ReAct 编排：PLAN(LLM) → ACT → OBSERVE → RENDER
│   └── llm.py               # DeepSeek client（懒加载 key，支持无 key 离线回退）
├── servers/
│   ├── mining_news_server.py   # MCP server 1：search / fetch_article / list_sources
│   ├── mineral_pdf_server.py   # MCP server 2：extract_resources / list_samples
│   └── lme_price_server.py     # MCP server 3：get_price / get_trend / list_commodities
├── data/                      # 内置样例语料（离线降级，保证 demo 恒可跑）
│   ├── news_samples.json
│   ├── pdf_samples.json
│   └── price_samples.json
├── tests/test_smoke.py        # 冒烟测试（8 用例，docker build 时自动跑）
├── mcp-config.json            # 可直接导入 Claude Desktop / Cursor
├── docker-compose.yml         # 一键起 3 server + agent demo
├── Dockerfile
├── requirements.txt
├── RUN.md                     # 5 分钟跑起来
└── README.md
```

## 关键设计

### 1. 三个 MCP server（Python / FastMCP）
| Server | 工具 | 数据策略 |
|---|---|---|
| mining-news-mcp | `search(query, days)` / `fetch_article(url)` | mining.com / S&P RSS → 抓不到自动降级到内置样例 |
| mineral-pdf-mcp | `extract_resources(pdf_url)` | 真实 PDF 启发式扫表（pypdf）→ 失败明确 `needs_human_review`，**绝不编造数字** |
| lme-price-mcp | `get_price(commodity, date)` / `get_trend(commodity, days)` | LME 公开 CSV → 降级样例序列并显式打 `is_sample` 标记 |

**降级透明原则**：每个 server 都有"真实源 → 样例兜底"两档，且**永远在返回里声明数据是 live 还是 sample**。演示/评测环境无网也能完整跑通。

### 2. ReAct 编排（不依赖 LangGraph）
- **PLAN**：DeepSeek 把自然语言请求解析成结构化计划 `{company, commodity, days, need_news, need_pdf, need_price}`；LLM 不可用时自动回退确定性解析。
- **ACT / OBSERVE**：代码按计划调用 3 个 MCP server，观测结果原样存证——**模型不允许发明数字**。
- **RENDER**：Markdown 日报含五段（新闻/储量/价格/风险/引用），样例数据用 ⚠️ 显式标注。

### 3. 工程化
- 类型标注、中文注释、模块化、统一降级策略
- pytest 冒烟测试 8 例；Docker build 时自动跑测试
- `mcp-config.json` 开箱即用接 Claude Desktop / Cursor

## 快速开始

```bash
# 本地（需要 python 3.10+）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt         # macOS/Linux
export DEEPSEEK_API_KEY=sk-xxx     # 或放 ~/Desktop/.env
python demo.py                     # 生成 Pilbara 锂矿今日简报
```

> 没有 DeepSeek key？agent 会自动走确定性规划，日报仍可生成（数据为样例，已标注）。

## Docker 一键跑

```bash
# 生成日报（agent-demo 跑完即出 outputs/briefing.md）
DEEPSEEK_API_KEY=sk-xxx docker compose up agent-demo

# 只起 3 个 MCP server
docker compose up mining-news-mcp mineral-pdf-mcp lme-price-mcp

# 冒烟测试
docker compose --profile test up agent-test
```

详细步骤见 **[RUN.md](RUN.md)**。

## 接入 Claude Desktop / Cursor

把 `mcp-config.json` 中的三个 server 合并进你的 `claude_desktop_config.json` /
Cursor MCP 配置即可直接对话调用（路径已按本机 venv 绝对路径写死）。
