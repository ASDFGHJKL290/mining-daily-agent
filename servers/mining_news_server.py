"""
mining-news-mcp: Mining news aggregation MCP server.

Tools:
  - search(query, days)          -> search mining news headlines by keyword and time window
  - fetch_article(url)           -> fetch and extract full article text from a URL

Data sources (real, in priority order):
  1. mining.com RSS feed
  2. S&P Global Mining RSS feed (marketplace.mining.com/feed)
  3. Built-in sample corpus (offline fallback so the demo always works)

The server degrades gracefully: if remote feeds are unreachable (no network,
rate-limited, geo-blocked), it transparently falls back to the local sample
corpus so downstream agents always get a structured response.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

mcp = FastMCP("mining-news-mcp")

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "news_samples.json")

# Real RSS endpoints. These are public feeds, no auth required.
RSS_FEEDS = [
    {"name": "mining.com", "url": "https://www.mining.com/feed/"},
    {"name": "spg-mining-marketplace", "url": "https://marketplace.mining.com/feed/"},
]

REQUEST_TIMEOUT_SECONDS = 10.0
MAX_FETCH_BYTES = 500_000  # cap article body download


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_samples() -> list[dict[str, Any]]:
    """Load the bundled sample corpus (used when live feeds are unavailable)."""
    path = os.path.normpath(SAMPLE_DIR)
    if not os.path.exists(path):
        # inline minimal fallback so import never crashes
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload.get("articles", [])
    except (OSError, ValueError):
        return []


def _fetch_feed(feed_url: str) -> list[dict[str, Any]]:
    """Fetch and normalise one RSS feed into article dicts."""
    parsed = feedparser.parse(feed_url)
    articles: list[dict[str, Any]] = []
    for entry in parsed.entries[:50]:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if published:
            pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
            pub_iso = pub_dt.isoformat()
        else:
            pub_iso = _utc_now().isoformat()

        summary = html.unescape(entry.get("summary", ""))
        summary = re.sub(r"<[^>]+>", " ", summary)
        summary = re.sub(r"\s+", " ", summary).strip()

        articles.append(
            {
                "id": f"rss-{feed_url.split('//')[-1].split('/')[0]}-{int(time.time() * 1000)}-{len(articles)}",
                "title": html.unescape(entry.get("title", "")).strip(),
                "url": entry.get("link", ""),
                "source": feed_url,
                "summary": summary[:600],
                "published": pub_iso,
            }
        )
    return articles


def _gather_articles(days: int = 30) -> list[dict[str, Any]]:
    """Try live feeds first; fall back to the bundled sample corpus."""
    cutoff = _utc_now() - timedelta(days=max(1, days))
    merged: list[dict[str, Any]] = []
    live_ok = False

    if httpx is not None:
        for feed in RSS_FEEDS:
            try:
                feed_articles = _fetch_feed(feed["url"])
                if feed_articles:
                    live_ok = True
                merged.extend(feed_articles)
            except Exception:  # network errors, timeouts -> keep going
                continue

    if not live_ok or len(merged) < 3:
        # Offline / degraded path: use the bundled sample corpus so the tool
        # contract (structured articles) is always satisfiable.
        for idx, article in enumerate(_load_samples()):
            article = dict(article)  # copy, never mutate cache
            article["id"] = f"sample-{idx}"
            merged.append(article)

    # de-duplicate by url, keep newest first, filter by cutoff window
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for article in sorted(merged, key=lambda a: a.get("published", ""), reverse=True):
        url_key = (article.get("url") or article.get("title") or "").strip()
        if url_key in seen:
            continue
        seen.add(url_key)
        try:
            pub_dt = datetime.fromisoformat(article["published"])
            if pub_dt < cutoff:
                continue
        except (ValueError, KeyError):
            pass  # unknown date -> keep it
        result.append(article)

    return result


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def search(query: str, days: int = 30) -> dict[str, Any]:
    """Search mining industry news by keyword and recency window.

    Args:
        query: Keyword(s) to match against title and summary (e.g. "lithium",
            "Pilbara", "copper", "NI 43-101"). Case-insensitive substring match
            on each term (space-separated terms are AND-ed).
        days: Look-back window in days (default 30, min 1).

    Returns:
        {"query": ..., "days": ..., "total": n, "articles": [...]}
    """
    articles = _gather_articles(days=days)
    terms = [term.lower() for term in query.split() if term.strip()]
    if not terms:
        return {"query": query, "days": days, "total": 0, "articles": []}

    matched: list[dict[str, Any]] = []
    for article in articles:
        haystack = " ".join(
            [
                article.get("title", ""),
                article.get("summary", ""),
                article.get("source", ""),
            ]
        ).lower()
        if all(term in haystack for term in terms):
            matched.append(article)
    return {
        "query": query,
        "days": days,
        "total": len(matched),
        "articles": matched[:20],
    }


@mcp.tool()
def fetch_article(url: str) -> dict[str, Any]:
    """Fetch a full news article from a URL and extract readable text.

    Args:
        url: The article URL (any mining news site).

    Returns:
        {"url": ..., "title": ..., "content": "...", "truncated": bool}
        In degraded mode (network unavailable) returns the URL as-is with an
        empty content and a note, or a bundled sample when the URL matches one.
    """
    # 1. If the url matches a bundled sample, serve it directly (offline demo).
    for article in _load_samples():
        if article.get("url") == url:
            return {
                "url": url,
                "title": article.get("title", ""),
                "content": article.get("content", article.get("summary", "")),
                "truncated": False,
            }

    # 2. Live fetch with body-size cap.
    if httpx is not None:
        try:
            with httpx.Client(
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; MiningDailyAgent/1.0)"},
            ) as client:
                response = client.get(url)
                response.raise_for_status()
                body = response.text[:MAX_FETCH_BYTES]

            from bs4 import BeautifulSoup

            soup = BeautifulSoup(body, "lxml")
            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()
            title = soup.title.get_text(strip=True) if soup.title else ""
            content = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
            return {
                "url": url,
                "title": title,
                "content": content[:4000],
                "truncated": len(content) > 4000,
            }
        except Exception as exc:  # noqa: BLE001 - degrade instead of crash
            return {
                "url": url,
                "title": "",
                "content": f"[unavailable: {type(exc).__name__}]",
                "truncated": False,
                "note": "Live fetch failed; no bundled sample matched this URL.",
            }

    return {"url": url, "title": "", "content": "", "truncated": False}


@mcp.tool()
def list_sources() -> dict[str, Any]:
    """List the news sources this server aggregates (for transparency/citation)."""
    return {
        "sources": [
            {"name": "mining.com", "url": "https://www.mining.com/feed/"},
            {
                "name": "S&P Global Mining Marketplace",
                "url": "https://marketplace.mining.com/feed/",
            },
        ],
        "degraded_fallback": "bundled sample corpus",
    }


if __name__ == "__main__":
    mcp.run()
