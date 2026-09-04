"""
lme-price-mcp: Commodity price行情 MCP server.

Tools:
  - get_price(commodity, date)      -> price for a commodity (latest, or on/before a date)
  - get_trend(commodity, days)      -> recent daily price series + simple analytics

Supported commodities:
  - copper / zinc / nickel   -> LME (London Metal Exchange) cash prices, USD/t
  - lithium-carbonate        -> GFEX (广州期货交易所) lithium carbonate continuous, CNY/t
  - iron-ore                 -> DCE (大连商品交易所) iron ore continuous, CNY/t

Real data sources (all public, no API key, reachable directly from mainland China):
  1. Sina Finance real-time quotes  https://hq.sinajs.cn/list=hf_*,nf_*  (GBK encoded)
  2. Sina Finance daily K-line history for trends (Global/Inner futures services)
The server degrades gracefully: if live sources are unreachable (sandbox / offline
demo), it falls back to a bundled date-stamped sample series so the tool contract
always holds. Sample data is ALWAYS flagged is_sample=true and MUST NOT be quoted
as live market data in a briefing without that caveat.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lme-price-mcp")

PRICE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "price_samples.json")

QUOTE_TIMEOUT_SECONDS = 6.0
KLINE_TIMEOUT_SECONDS = 15.0

# 品种注册表: symbol -> (新浪行情代码, K线symbol, K线服务类型, 显示名, 单位)
# K线服务类型: "global" 走 GlobalFuturesService(外盘), "domestic" 走 InnerFuturesNewService(国内)
COMMODITIES: dict[str, tuple[str, str, str, str, str]] = {
    "copper": ("hf_CAD", "CAD", "global", "LME Copper", "USD/t"),
    "zinc": ("hf_ZSD", "ZSD", "global", "LME Zinc", "USD/t"),
    "nickel": ("hf_NID", "NID", "global", "LME Nickel", "USD/t"),
    "lithium-carbonate": ("nf_LC0", "LC0", "domestic", "GFEX Lithium Carbonate", "CNY/t"),
    "iron-ore": ("nf_I0", "I0", "domestic", "DCE Iron Ore (continuous)", "CNY/t"),
}
# 别名归一化
_ALIASES = {"lithium": "lithium-carbonate"}

SINA_QUOTE_URL = "https://hq.sinajs.cn/list={codes}"
SINA_KLINE_GLOBAL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
    "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={symbol}"
)
SINA_KLINE_DOMESTIC = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
    "InnerFuturesNewService.getDailyKLine?symbol={symbol}"
)
# 新浪要求带浏览器 Referer / UA，否则拒绝(403)
_SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

# 日K序列缓存（全量序列从上市日起拉取，约几千条，做 TTL 缓存避免重复请求）
_KLINE_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_KLINE_CACHE_TTL_SECONDS = 600.0  # 10 分钟


# ---------------------------------------------------------------------------
# Bundled sample series (offline fallback)
# ---------------------------------------------------------------------------


def _load_samples() -> dict[str, Any]:
    path = os.path.normpath(PRICE_DIR)
    if not os.path.exists(path):
        return {"series": {}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"series": {}}


def _get_sample_series(symbol: str) -> list[dict[str, Any]]:
    """返回样例序列（升序）。样例兜底路径用，永远标注 is_sample=true。"""
    samples = _load_samples()
    series = samples.get("series", {}).get(symbol, [])
    return sorted(series, key=lambda point: point["date"])


# ---------------------------------------------------------------------------
# Sina Finance live sources
# ---------------------------------------------------------------------------


def _fetch_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """批量拉取新浪实时快照。

    Returns:
        {sina_code: {"price": float, "quote_date": "YYYY-MM-DD"}}
        网络异常 / 解析失败时返回空 dict（调用方走降级）。
    """
    try:
        response = httpx.get(
            SINA_QUOTE_URL.format(codes=",".join(codes)),
            headers=_SINA_HEADERS,
            timeout=QUOTE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        text = response.content.decode("gbk", errors="replace")
    except Exception:  # noqa: BLE001 - network / encoding issues -> degrade
        return {}

    quotes: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        match = re.match(r'var hq_str_([A-Za-z0-9_]+)="(.*)";', line.strip())
        if not match:
            continue
        code, payload = match.group(1), match.group(2)
        fields = payload.split(",")
        try:
            if code.startswith("hf_"):
                # 外盘格式: [0]=最新价 ... [12]=日期(YYYY-MM-DD) [13]=中文名
                price = float(fields[0])
                quote_date = fields[12] if len(fields) > 12 else ""
            elif code.startswith("nf_"):
                # 国内期货格式: [0]=名称 [1]=时间 [2]=最新价 ... [17]=日期
                price = float(fields[2])
                quote_date = fields[17] if len(fields) > 17 else ""
            else:
                continue
        except (ValueError, IndexError):
            continue
        if price <= 0:
            continue
        quotes[code] = {"price": price, "quote_date": quote_date}
    return quotes


def _fetch_kline(kind: str, symbol: str) -> list[dict[str, Any]] | None:
    """拉取新浪日K收盘价序列 [{date, price}]，升序。失败返回 None。

    JSONP 响应形如: var t=([{...},{...}]); 
    外盘字段: date/open/high/low/close；国内字段: d/o/h/l/c。
    """
    if kind == "global":
        url = SINA_KLINE_GLOBAL.format(symbol=symbol)
    else:
        url = SINA_KLINE_DOMESTIC.format(symbol=symbol)
    try:
        response = httpx.get(url, headers=_SINA_HEADERS, timeout=KLINE_TIMEOUT_SECONDS)
        response.raise_for_status()
        text = response.text
    except Exception:  # noqa: BLE001
        return None

    match = re.search(r"\((\[.*\])\)", text, re.S)
    if not match:
        return None
    try:
        rows = json.loads(match.group(1))
    except ValueError:
        return None

    series: list[dict[str, Any]] = []
    for row in rows:
        if kind == "global":
            day, close = row.get("date"), row.get("close")
        else:
            day, close = row.get("d"), row.get("c")
        try:
            series.append({"date": day, "price": float(close)})
        except (TypeError, ValueError):
            continue
    return sorted(series, key=lambda point: point["date"]) or None


def _live_series(kind: str, symbol: str) -> list[dict[str, Any]] | None:
    """带 TTL 缓存的日K序列获取。"""
    cache_key = f"{kind}:{symbol}"
    cached = _KLINE_CACHE.get(cache_key)
    now = time.time()
    if cached and now - cached[0] < _KLINE_CACHE_TTL_SECONDS:
        return cached[1]
    series = _fetch_kline(kind, symbol)
    if series:
        _KLINE_CACHE[cache_key] = (now, series)
    return series


def _normalise_symbol(commodity: str) -> str:
    raw = commodity.strip().lower()
    return _ALIASES.get(raw, raw)


def _live_price_payload(symbol: str) -> dict[str, Any] | None:
    """优先实时快照，其次日K最新收盘。失败返回 None。"""
    sina_code, kline_symbol, kind, _display, _unit = COMMODITIES[symbol]
    quotes = _fetch_quotes([sina_code])
    if sina_code in quotes:
        quote = quotes[sina_code]
        return {
            "date": quote["quote_date"],
            "price": quote["price"],
            "note": "Live source: Sina Finance real-time quote.",
        }
    series = _live_series(kind, kline_symbol)
    if series:
        point = series[-1]
        return {
            "date": point["date"],
            "price": point["price"],
            "note": "Live source: Sina Finance daily K-line (latest close).",
        }
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_price(commodity: str, date: str | None = None) -> dict[str, Any]:
    """Get the price of a commodity (latest quote, or the last close on/before a date).

    Args:
        commodity: copper | zinc | nickel | lithium-carbonate | iron-ore
        date: ISO date string (YYYY-MM-DD). If omitted, the latest available quote.

    Returns:
        {"commodity", "display_name", "unit", "date", "price",
         "is_sample": bool, "note": "..."}
    """
    symbol = _normalise_symbol(commodity)
    if symbol not in COMMODITIES:
        return {
            "error": f"Unknown commodity '{commodity}'. Supported: {', '.join(COMMODITIES)}"
        }
    display_name, unit = COMMODITIES[symbol][3], COMMODITIES[symbol][4]
    _, kline_symbol, kind, _, _ = COMMODITIES[symbol]

    # 1) 指定了历史日期 -> 用真实日K序列中 <= date 的最后一条收盘价
    if date:
        series = _live_series(kind, kline_symbol)
        if series:
            point = next(
                (p for p in reversed(series) if p["date"] <= date), None
            )
            if point:
                return {
                    "commodity": symbol,
                    "display_name": display_name,
                    "unit": unit,
                    "date": point["date"],
                    "price": point["price"],
                    "is_sample": False,
                    "note": "Live source: Sina Finance daily K-line (close on/before date).",
                }

    # 2) 未指定日期(或快照更实时) -> 实时快照 / 日K最新
    live = _live_price_payload(symbol)
    if live:
        return {
            "commodity": symbol,
            "display_name": display_name,
            "unit": unit,
            **live,
            "is_sample": False,
        }

    # 3) 降级：内置样例序列（明确标注，绝不冒充实时行情）
    series = _get_sample_series(symbol)
    if not series:
        return {
            "commodity": symbol,
            "display_name": display_name,
            "unit": unit,
            "date": date or "n/a",
            "price": None,
            "is_sample": True,
            "note": "No sample data bundled for this commodity.",
        }
    target = date
    if target is None:
        point = series[-1]
    else:
        point = next((p for p in reversed(series) if p["date"] <= target), series[-1])
    return {
        "commodity": symbol,
        "display_name": display_name,
        "unit": unit,
        "date": point["date"],
        "price": point["price"],
        "is_sample": True,
        "note": "Sample series (offline demo) - NOT live market data.",
    }


@mcp.tool()
def get_trend(commodity: str, days: int = 30) -> dict[str, Any]:
    """Get a recent price trend for a commodity.

    Args:
        commodity: copper | zinc | nickel | lithium-carbonate | iron-ore
        days: number of days to look back (default 30, max 365).

    Returns:
        {"commodity", "unit", "points": [{date, price}], "first_price",
         "last_price", "change_pct", "min", "max", "is_sample", "note"}
    """
    symbol = _normalise_symbol(commodity)
    if symbol not in COMMODITIES:
        return {"error": f"Unknown commodity '{commodity}'."}
    display_name, unit = COMMODITIES[symbol][3], COMMODITIES[symbol][4]
    _, kline_symbol, kind, _, _ = COMMODITIES[symbol]
    window = max(1, min(int(days), 365))

    # 1) 真实日K优先
    live_series = _live_series(kind, kline_symbol)
    if live_series:
        cutoff = (datetime.now() - timedelta(days=window)).date().isoformat()
        windowed = [p for p in live_series if p["date"] >= cutoff]
        if len(windowed) < 2:
            windowed = live_series[-window:]
        prices = [p["price"] for p in windowed if p.get("price") is not None]
        if prices:
            change_pct = (
                round((prices[-1] - prices[0]) / prices[0] * 100, 2)
                if prices[0]
                else None
            )
            return {
                "commodity": symbol,
                "display_name": display_name,
                "unit": unit,
                "points": windowed,
                "first_price": prices[0],
                "last_price": prices[-1],
                "change_pct": change_pct,
                "min": min(prices),
                "max": max(prices),
                "is_sample": False,
                "note": "Live source: Sina Finance daily K-line.",
            }

    # 2) 降级：内置样例序列
    series = _get_sample_series(symbol)
    if not series:
        return {
            "commodity": symbol,
            "display_name": display_name,
            "unit": unit,
            "points": [],
            "first_price": None,
            "last_price": None,
            "change_pct": None,
            "min": None,
            "max": None,
            "is_sample": True,
            "note": "No data available for this commodity.",
        }
    cutoff = (datetime.now() - timedelta(days=window)).date().isoformat()
    windowed = [p for p in series if p["date"] >= cutoff] or series[-window:]
    prices = [p["price"] for p in windowed if p.get("price") is not None]
    if not prices:
        return {
            "commodity": symbol,
            "display_name": display_name,
            "unit": unit,
            "points": windowed,
            "first_price": None,
            "last_price": None,
            "change_pct": None,
            "min": None,
            "max": None,
            "is_sample": True,
            "note": "Sample series (offline demo) - NOT live market data.",
        }
    change_pct = round((prices[-1] - prices[0]) / prices[0] * 100, 2) if prices[0] else None
    return {
        "commodity": symbol,
        "display_name": display_name,
        "unit": unit,
        "points": windowed,
        "first_price": prices[0],
        "last_price": prices[-1],
        "change_pct": change_pct,
        "min": min(prices),
        "max": max(prices),
        "is_sample": True,
        "note": "Sample series (offline demo) - NOT live market data.",
    }


@mcp.tool()
def list_commodities() -> dict[str, Any]:
    """List supported commodities, units and data mode."""
    return {
        "commodities": {key: value[3] for key, value in COMMODITIES.items()},
        "note": (
            "Live source: Sina Finance (real-time quotes + daily K-line). "
            "Bundled offline sample series as fallback."
        ),
    }


if __name__ == "__main__":
    mcp.run()
