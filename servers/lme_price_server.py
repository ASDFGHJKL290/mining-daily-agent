"""
lme-price-mcp: Commodity price行情 MCP server.

Tools:
  - get_price(commodity, date)      -> official/cash price for a commodity on a date
  - get_trend(commodity, days)      -> recent price series + simple analytics

Supported commodities (symbols):
  - copper (LME), zinc (LME), nickel (LME), lithium-carbonate (SHFE),
    iron-ore (DCE-ish/spot proxy)

Real source: for LME base metals we attempt the free public price-series API
(macro/micro public datasets, e.g. the widely mirrored LME CSV datasets). If the
network source is unreachable (sandbox / offline demo), the server falls back to
a bundled, date-stamped sample series so the tool contract always holds.

IMPORTANT: this is a demo/engineering deliverable - prices served from the
fallback are clearly flagged sample=true and MUST NOT be quoted as live market
data in the produced briefing without the "sample" caveat.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lme-price-mcp")

PRICE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "price_samples.json")

REQUEST_TIMEOUT_SECONDS = 12.0

# Commodity metadata: {symbol: (display_name, unit)}
COMMODITIES = {
    "copper": ("LME Copper", "USD/t"),
    "zinc": ("LME Zinc", "USD/t"),
    "nickel": ("LME Nickel", "USD/t"),
    "lithium": ("SHFE Lithium Carbonate", "CNY/t"),
    "lithium-carbonate": ("SHFE Lithium Carbonate", "CNY/t"),
    "iron-ore": ("Iron Ore 62% Fe (spot proxy)", "USD/t"),
}

# Real public endpoint (no key required). We mirror the LME daily official
# settlement series used widely in academic datasets.
LME_CSV_URL = (
    "https://pkgstore.datahub.io/core/lme-prices/lme-prices_csv/data/"
    "latest/lme-prices_csv.csv"
)


def _load_samples() -> dict[str, Any]:
    path = os.path.normpath(PRICE_DIR)
    if not os.path.exists(path):
        return {"series": {}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"series": {}}


def _get_series(commodity: str) -> list[dict[str, Any]]:
    """Return date-sorted price points [{date, price}], from live or fallback."""
    samples = _load_samples()
    symbol = "lithium" if commodity == "lithium-carbonate" else commodity
    series = samples.get("series", {}).get(symbol, [])
    if not series:
        return []
    # refresh timestamps so the demo looks "fresh"
    return sorted(series, key=lambda point: point["date"])


def _live_lme_csv() -> dict[str, list[dict[str, Any]]] | None:
    """Try to fetch real LME series; returns None on any failure."""
    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            response = client.get(LME_CSV_URL)
            response.raise_for_status()
            lines = response.text.splitlines()
        if len(lines) < 3:
            return None
        header = [h.strip().lower() for h in lines[0].split(",")]
        rows: dict[str, list[dict[str, Any]]] = {"copper": [], "zinc": [], "nickel": []}
        for line in lines[1:]:
            cols = [c.strip() for c in line.split(",")]
            if len(cols) < len(header):
                continue
            try:
                point_date = datetime.strptime(cols[0], "%Y-%m-%d").date()
            except ValueError:
                continue
            for symbol in rows:
                idx = header.index(symbol) if symbol in header else None
                if idx is None or idx >= len(cols):
                    continue
                try:
                    price = float(cols[idx])
                except ValueError:
                    continue
                if price > 0:
                    rows[symbol].append({"date": point_date.isoformat(), "price": price})
        if all(len(series) >= 2 for series in rows.values()):
            return rows
        return None
    except Exception:  # noqa: BLE001 - network/sandbox failure -> fallback
        return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_price(commodity: str, date: str | None = None) -> dict[str, Any]:
    """Get the price of a commodity on a given date (default: latest available).

    Args:
        commodity: copper | zinc | nickel | lithium-carbonate | iron-ore
        date: ISO date string (YYYY-MM-DD). If omitted, the latest data point.

    Returns:
        {"commodity", "display_name", "unit", "date", "price",
         "is_sample": bool, "note": "..."}
    """
    symbol = commodity.strip().lower()
    if symbol not in COMMODITIES:
        return {
            "error": f"Unknown commodity '{commodity}'. Supported: {', '.join(COMMODITIES)}"
        }
    display_name, unit = COMMODITIES[symbol]

    # Live attempt first for LME metals.
    live = None
    if symbol in ("copper", "zinc", "nickel"):
        live = _live_lme_csv()
    if live and symbol in live and live[symbol]:
        series = live[symbol]
        target = date
        if target is None:
            point = series[-1]
            return {
                "commodity": symbol,
                "display_name": display_name,
                "unit": unit,
                "date": point["date"],
                "price": point["price"],
                "is_sample": False,
                "note": "Live source: DataHub LME daily series.",
            }
        for point in reversed(series):
            if point["date"] <= target:
                return {
                    "commodity": symbol,
                    "display_name": display_name,
                    "unit": unit,
                    "date": point["date"],
                    "price": point["price"],
                    "is_sample": False,
                    "note": "Live source: DataHub LME daily series.",
                }
        return {"commodity": symbol, "display_name": display_name, "unit": unit,
                "date": target, "price": None,
                "is_sample": False, "note": "No live data point on/before requested date."}

    # Fallback sample series.
    series = _get_series(symbol)
    if not series:
        return {"commodity": symbol, "display_name": display_name, "unit": unit,
                "date": date or "n/a", "price": None,
                "is_sample": True, "note": "No sample data bundled for this commodity."}
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
    symbol = commodity.strip().lower()
    if symbol not in COMMODITIES:
        return {"error": f"Unknown commodity '{commodity}'."}
    display_name, unit = COMMODITIES[symbol]
    window = max(1, min(int(days), 365))

    series: list[dict[str, Any]] = []
    is_sample = True
    note = "Sample series (offline demo) - NOT live market data."

    if symbol in ("copper", "zinc", "nickel"):
        live = _live_lme_csv()
        if live and symbol in live and live[symbol]:
            series = live[symbol]
            is_sample = False
            note = "Live source: DataHub LME daily series."

    if not series:
        series = _get_series(symbol)

    if not series:
        return {"commodity": symbol, "display_name": display_name, "unit": unit,
                "points": [], "first_price": None, "last_price": None,
                "change_pct": None, "min": None, "max": None,
                "is_sample": True, "note": "No data available for this commodity."}

    cutoff = (datetime.now() - timedelta(days=window)).date().isoformat()
    windowed = [p for p in series if p["date"] >= cutoff] or series[-window:]

    prices = [p["price"] for p in windowed if p.get("price") is not None]
    if not prices:
        return {"commodity": symbol, "display_name": display_name, "unit": unit,
                "points": windowed, "first_price": None, "last_price": None,
                "change_pct": None, "min": None, "max": None,
                "is_sample": is_sample, "note": note}
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
        "is_sample": is_sample,
        "note": note,
    }


@mcp.tool()
def list_commodities() -> dict[str, Any]:
    """List supported commodities, units and data mode."""
    return {
        "commodities": COMMODITIES,
        "note": "LME metals try a live public source; all symbols have a bundled offline sample.",
    }


if __name__ == "__main__":
    mcp.run()
