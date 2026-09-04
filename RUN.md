# RUN.md — 5 分钟跑起来

本文件是给评测方/面试官的快速上手手册。三种方式任选其一，都能在 5 分钟内
看到一份完整的「矿权日报」Markdown。

---

## 方式 A：Docker 一键（推荐，最快）

```bash
cd mining-daily-agent

# 可选：把 key 写入 .env 让 agent 用 LLM 规划（没有 key 也能跑，走确定性规划）
echo "DEEPSEEK_API_KEY=sk-xxxx" > .env

# 起 3 个 MCP server + 生成一次日报，自动写出 outputs/briefing.md
docker compose up agent

# 看结果
cat outputs/briefing.md
```

首次构建需拉镜像 + 装依赖（约 2-4 分钟），之后每次跑 < 1 分钟。

## 方式 B：本地 Python（Windows / macOS / Linux）

```bash
cd mining-daily-agent

# 1) 建环境（只需一次）
python -m venv .venv
# Windows:
.venv/Scripts/python.exe -m pip install -r requirements.txt
# macOS / Linux:
# .venv/bin/python -m pip install -r requirements.txt

# 2) 配置 DeepSeek key（可选；不配则 agent 用确定性规划，日报仍可生成）
#    Windows: set DEEPSEEK_API_KEY=sk-xxxx
#    macOS/Linux: export DEEPSEEK_API_KEY=sk-xxxx
#    或放到 ~/Desktop/.env

# 3) 生成日报
python main.py
# 或自定义请求：
python main.py "Newmont 金矿最近 30 天有什么变化？"

# 4) 跑冒烟测试
python -m pytest tests/ -q
```

## 方式 C：交互式问答

```bash
python main.py --interactive
# > 给我生成一份关于 Pilbara 锂矿的今日简报
# > 铜价最近走势如何？
# > exit
```

---

## 预期输出（2026-09-04 真实运行节选，完整样例见 docs/sample_briefing_pilbara.md）

```markdown
# 矿权日报 · Pilbara Minerals（lithium）

## 一、新闻摘要
1. **Albemarle taps BHP veteran Udd as next CEO**
   来源：https://www.mining.com/feed/
   链接：https://www.mining.com/albemarle-taps-bhp-veteran-udd-as-next-ceo/
2. **China's biggest lithium mine loses licence**（CATL / 真实稿）
   ...

## 二、NI 43-101 储量数据
| 类别 | 矿石量 (Mt) | 品位 | 金属量 |
|---|---|---|---|
| Indicated | 324.6 | pct_li2o=1.13 | 3668.0 kt Li2O |
| Inferred | 87.2 | pct_li2o=1.05 | 916.0 kt Li2O |
数据说明：来自内置样例报告库（确定性离线演示数据，非实时抓取）。

## 三、价格走势
- 最新价（2026-09-04）：150,500.0 CNY/t（GFEX Lithium Carbonate）— 新浪实时行情
- 近 22 个数据点：143,220 → 149,880 （上涨 4.65%）

## 四、风险提示 / ## 五、引用来源
```

---

## 三个 MCP server 单独验证

```bash
# server 是 stdio 协议，可直接被任何 MCP client 拉起：
python servers/mining_news_server.py      # mining-news-mcp
python servers/mineral_pdf_server.py      # mineral-pdf-mcp
python servers/lme_price_server.py        # lme-price-mcp
```

用 `mcp-config.json` 导入 Claude Desktop / Cursor 后，可以直接在对话里调用：
- `search("Pilbara", 30)` → 新闻
- `extract_resources("<NI 43-101 pdf url>")` → 储量
- `get_trend("copper", 30)` → 价格趋势

---

## 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 无 DEEPSEEK_API_KEY 报错 | agent 想用 LLM 规划但没 key | 配置 key，或忽略——agent 自动回退确定性规划 |
| 价格带 ⚠️ 样例标注 | 无网络/数据源不可达 | 正常降级行为，数据源恢复后自动切 live |
| 储量显示"待人工审核" | PDF 无法可靠解析 | 设计如此：**不臆造数字**，宁可 abstain |
| docker 拉镜像慢 | 网络 | 换本地 Python 方式（方式 B） |
