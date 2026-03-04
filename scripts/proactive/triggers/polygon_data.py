"""Polygon.io data layer for real-world signal enrichment.

Provides market data, economic indicators, and news that the edge engine
uses to form independent probability estimates (vs just reading Kalshi prices).

Requires POLYGON_API_KEY in environment or config.yaml:
  polygon:
    api_key: "your-polygon-api-key"
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from proactive.base import _log


# ── Config ──────────────────────────────────────────────────────────────────

def _get_polygon_key() -> str:
    """Get Polygon API key from env or config."""
    key = os.environ.get("POLYGON_API_KEY", "")
    if key:
        return key
    try:
        import yaml
        for candidate in [
            Path.home() / ".openclaw" / "config.yaml",
            Path(__file__).resolve().parent.parent.parent.parent / "config.yaml",
        ]:
            if candidate.exists():
                with open(candidate) as f:
                    cfg = yaml.safe_load(f) or {}
                    key = cfg.get("polygon", {}).get("api_key", "")
                    if key:
                        return key
    except Exception:
        pass
    return ""


POLYGON_BASE = "https://api.polygon.io"
_CACHE_DIR = Path.home() / ".openclaw" / "state" / "polygon_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_TTL = 1800  # 30 min cache for most data


def _polygon_get(endpoint: str, params: dict | None = None, cache_key: str = "") -> dict | list | None:
    """Authenticated GET to Polygon API with disk caching."""
    api_key = _get_polygon_key()
    if not api_key:
        _log("Polygon: no API key configured")
        return None

    # Check cache first
    if cache_key:
        cache_path = _CACHE_DIR / f"{cache_key}.json"
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text())
                if time.time() - cached.get("_cached_at", 0) < _CACHE_TTL:
                    return cached.get("data")
            except (json.JSONDecodeError, OSError):
                pass

    # Build URL
    params = params or {}
    params["apiKey"] = api_key
    url = f"{POLYGON_BASE}{endpoint}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "Rout/1.9.0",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        # Write cache
        if cache_key:
            cache_path = _CACHE_DIR / f"{cache_key}.json"
            try:
                cache_path.write_text(json.dumps({"data": data, "_cached_at": time.time()}))
            except OSError:
                pass

        return data

    except Exception as e:
        _log(f"Polygon API error: {e}")
        return None


# ── Public Data Functions ───────────────────────────────────────────────────

def get_ticker_news(query: str, limit: int = 5) -> list[dict]:
    """Fetch recent news articles relevant to a query/topic.

    Returns list of {title, description, published_utc, tickers, source}.
    Used by edge engine to gauge sentiment and event proximity.
    """
    data = _polygon_get(
        "/v2/reference/news",
        params={"limit": str(limit), "order": "desc", "sort": "published_utc"},
        cache_key=f"news_{query[:30].replace(' ', '_')}",
    )
    if not data or not isinstance(data, dict):
        return []

    results = data.get("results", [])
    return [
        {
            "title": r.get("title", ""),
            "description": (r.get("description") or "")[:200],
            "published_utc": r.get("published_utc", ""),
            "tickers": r.get("tickers", []),
            "source": r.get("publisher", {}).get("name", ""),
        }
        for r in results[:limit]
    ]


def get_market_status() -> dict:
    """Get current market status (open/closed, early close, etc.)."""
    data = _polygon_get("/v1/marketstatus/now", cache_key="market_status")
    if not data or not isinstance(data, dict):
        return {}
    return {
        "market": data.get("market", "unknown"),
        "exchanges": data.get("exchanges", {}),
        "currencies": data.get("currencies", {}),
    }


def get_economic_indicators() -> dict:
    """Get key economic data points for macro context.

    Fetches: S&P 500, VIX, Treasury yields, BTC, Gold.
    These help Qwen assess macro environment for political/event markets.
    """
    indicators = {}

    # S&P 500 (SPY as proxy)
    spy = _get_latest_price("SPY")
    if spy:
        indicators["sp500"] = spy

    # VIX
    # Polygon doesn't have VIX directly; use VIXY ETF as proxy
    vix = _get_latest_price("VIXY")
    if vix:
        indicators["vix_proxy"] = vix

    # Bitcoin
    btc = _get_crypto_price("BTC")
    if btc:
        indicators["btc"] = btc

    # Gold
    gold = _get_latest_price("GLD")
    if gold:
        indicators["gold_proxy"] = gold

    return indicators


def _get_latest_price(ticker: str) -> dict | None:
    """Get previous day close + change for an equity ticker."""
    data = _polygon_get(
        f"/v2/aggs/ticker/{ticker}/prev",
        cache_key=f"prev_{ticker}",
    )
    if not data or not isinstance(data, dict):
        return None
    results = data.get("results", [])
    if not results:
        return None
    r = results[0]
    return {
        "ticker": ticker,
        "close": r.get("c", 0),
        "open": r.get("o", 0),
        "high": r.get("h", 0),
        "low": r.get("l", 0),
        "volume": r.get("v", 0),
        "change_pct": round((r.get("c", 0) - r.get("o", 0)) / r.get("o", 1) * 100, 2) if r.get("o") else 0,
    }


def _get_crypto_price(symbol: str) -> dict | None:
    """Get latest crypto price from Polygon."""
    data = _polygon_get(
        f"/v2/aggs/ticker/X:{symbol}USD/prev",
        cache_key=f"crypto_{symbol}",
    )
    if not data or not isinstance(data, dict):
        return None
    results = data.get("results", [])
    if not results:
        return None
    r = results[0]
    return {
        "symbol": symbol,
        "price": r.get("c", 0),
        "change_pct": round((r.get("c", 0) - r.get("o", 0)) / r.get("o", 1) * 100, 2) if r.get("o") else 0,
    }


def get_related_news_for_market(market_title: str) -> list[dict]:
    """Extract keywords from a Kalshi market title and fetch relevant Polygon news."""
    # Extract meaningful keywords (skip common words)
    stopwords = {
        "will", "the", "a", "an", "in", "on", "by", "of", "to", "for", "be",
        "is", "at", "or", "and", "this", "that", "with", "from", "not", "yes",
        "no", "before", "after", "during", "more", "than", "less", "between",
    }
    words = [w.strip("?.,!") for w in market_title.lower().split() if len(w) > 2]
    keywords = [w for w in words if w not in stopwords][:5]

    if not keywords:
        return []

    # Search Polygon news with top keywords
    query = " ".join(keywords[:3])
    return get_ticker_news(query, limit=3)


def is_polygon_configured() -> bool:
    """Check if Polygon API key is available."""
    return bool(_get_polygon_key())
