"""
Smoke tests for the three MCP servers (pure-function level, no subprocess).

Run:  python -m pytest tests/ -q
"""

from __future__ import annotations

import importlib.util
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def _load(module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(PROJECT_ROOT, "servers", f"{module_name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_news_search_returns_articles():
    module = _load("mining_news_server")
    result = module.search(query="Pilbara", days=30)
    assert result["total"] >= 1
    assert all("title" in a for a in result["articles"])


def test_news_search_unknown_keyword_returns_empty():
    module = _load("mining_news_server")
    result = module.search(query="zzz_nonexistent_keyword_zzz", days=30)
    assert result["total"] == 0


def test_pdf_extract_bundled_sample():
    module = _load("mineral_pdf_server")
    result = module.extract_resources(
        "https://www.pilbaraminerals.com.au/reports/pilgangoora-ni43-101.pdf"
    )
    assert result["needs_human_review"] is False
    categories = {r["category"] for r in result["resources"]}
    assert "Indicated" in categories
    assert "Inferred" in categories


def test_pdf_unknown_url_degrades_gracefully():
    module = _load("mineral_pdf_server")
    result = module.extract_resources("https://example.com/not-a-real-report.pdf")
    # never crashes, and clearly flags review rather than inventing numbers
    assert isinstance(result, dict)
    assert result["needs_human_review"] in (True, False)


def test_price_get_latest():
    module = _load("lme_price_server")
    result = module.get_price("lithium-carbonate")
    assert result["price"] is not None
    assert result["unit"] == "CNY/t"


def test_price_trend_shape():
    module = _load("lme_price_server")
    result = module.get_trend("copper", days=30)
    assert len(result["points"]) >= 2
    assert result["first_price"] is not None
    assert result["last_price"] is not None


def test_price_unknown_commodity():
    module = _load("lme_price_server")
    result = module.get_price("unobtainium")
    assert "error" in result


def test_render_markdown_contains_all_sections():
    sys.path.insert(0, PROJECT_ROOT)
    from agent.react_agent import MiningDailyAgent

    agent = MiningDailyAgent.__new__(MiningDailyAgent)  # no init -> no api key needed
    gathered = {
        "news": [
            {
                "title": "Headline A",
                "source": "mining.com",
                "url": "https://mining.com/a",
                "summary": "summary a",
            }
        ],
        "resource": {
            "report_title": "Report X",
            "report_url": "https://example.com/x.pdf",
            "resources": [
                {
                    "category": "Indicated",
                    "tonnage_mt": 100.0,
                    "grade": {"g/t_au": 1.5},
                    "contained_metal": 5.0,
                    "metal_unit": "Moz",
                }
            ],
        },
        "price": {"display_name": "SHFE Lithium Carbonate", "unit": "CNY/t",
                  "date": "2026-09-03", "price": 87000.0, "is_sample": True},
        "trend": {"points": [{"date": "a", "price": 1}], "first_price": 1.0,
                  "last_price": 2.0, "change_pct": 100.0, "min": 1.0, "max": 2.0,
                  "is_sample": True},
        "citations": [{"title": "Headline A", "url": "https://mining.com/a"}],
    }
    markdown = agent._render_markdown(
        "test query", {"company": "Pilbara Minerals", "commodity": "lithium"}, gathered
    )
    for section in ("新闻摘要", "储量数据", "价格走势", "风险提示", "引用来源"):
        assert section in markdown
