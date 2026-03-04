"""Cross-platform comparator trigger (Kalshi vs Polymarket)."""

import json
import time
import urllib.request
import urllib.parse
from proactive.base import (
    PROACTIVE_CFG,
    OPENCLAW_DIR,
    _log,
    _send_message,
    _record_send,
)

# Personality-aware send
try:
    from proactive.personality.engine import personality_send as _personality_send
    _HAS_PERSONALITY = True
except ImportError:
    _HAS_PERSONALITY = False


def _fetch_kalshi_markets() -> list:
    """Fetch active Kalshi markets. Returns list of dicts with title, yes_price, volume."""
    try:
        from handlers.kalshi_handlers import _get_client, _check_enabled
        err = _check_enabled()
        if err:
            return []
        client = _get_client()
        if not client:
            return []
        import json as _json
        # Paginate to get markets with actual activity
        all_markets = []
        cursor = None
        for _ in range(5):  # max 5 pages of 200
            url = (
                f"https://api.elections.kalshi.com/trade-api/v2/markets"
                f"?limit=200&status=open"
            )
            if cursor:
                url += f"&cursor={cursor}"
            resp = client.call_api("GET", url)
            data = _json.loads(resp.read())
            batch = data.get("markets", [])
            if not batch:
                break
            for m in batch:
                # Use last_price (cents) or yes_ask as price signal
                price = m.get("last_price", 0) or m.get("yes_ask", 0)
                vol = m.get("volume", 0)
                oi = m.get("open_interest", 0)
                if price > 0 and (vol > 0 or oi > 0):
                    all_markets.append({
                        "title": m.get("title", ""),
                        "ticker": m.get("ticker", ""),
                        "yes_price": price,
                        "volume": vol,
                        "source": "kalshi",
                    })
            cursor = data.get("cursor")
            if not cursor:
                break
        return all_markets
    except Exception as e:
        _log(f"Kalshi fetch error: {e}")
        return []


def _fetch_polymarket_markets() -> list:
    """Fetch active Polymarket markets. Returns list of dicts with title, yes_price, volume."""
    try:
        url = "https://gamma-api.polymarket.com/markets?closed=false&limit=200&order=volume&ascending=false"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "Rout/1.9.0",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        markets = []
        for m in data if isinstance(data, list) else []:
            prices = m.get("outcomePrices", "[]")
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except Exception:
                    continue
            if not prices:
                continue
            yes_price = int(float(prices[0]) * 100) if prices else 0
            vol = float(m.get("volume", 0) or 0)
            if yes_price > 0 and vol > 0:
                markets.append({
                    "title": m.get("question", m.get("title", "")),
                    "slug": m.get("slug", ""),
                    "yes_price": yes_price,
                    "volume": vol,
                    "source": "polymarket",
                })
        return markets
    except Exception as e:
        _log(f"Polymarket fetch error: {e}")
        return []


def _fuzzy_match_title(a: str, b: str) -> float:
    """Simple word-overlap similarity score between two titles."""
    a_words = set(a.lower().split())
    b_words = set(b.lower().split())
    # Remove common stopwords
    stopwords = {"will", "the", "a", "an", "in", "on", "by", "of", "to", "for", "be", "is", "at", "?"}
    a_words -= stopwords
    b_words -= stopwords
    if not a_words or not b_words:
        return 0.0
    intersection = a_words & b_words
    union = a_words | b_words
    return len(intersection) / len(union) if union else 0.0


def check_cross_platform(state: dict, dry_run: bool = False, force: bool = False) -> bool:
    """Compare Kalshi vs Polymarket prices, alert on significant divergences."""
    cfg = PROACTIVE_CFG.get("cross_platform", {})
    if not cfg.get("enabled", True):
        return False

    interval = cfg.get("check_interval_minutes", 240)
    last_check = state.get("last_cross_platform_check", 0)
    if not force and time.time() - last_check < interval * 60:
        return False

    state["last_cross_platform_check"] = time.time()
    threshold = cfg.get("divergence_threshold_pct", 8.0)
    match_threshold = cfg.get("fuzzy_match_threshold", 0.6)
    min_vol = cfg.get("min_volume", 1000)

    _log("Cross-platform comparator starting...")
    kalshi_markets = _fetch_kalshi_markets()
    pm_markets = _fetch_polymarket_markets()

    if not kalshi_markets or not pm_markets:
        _log(f"Comparator: K={len(kalshi_markets)}, PM={len(pm_markets)} — skipping (need both)")
        return False

    _log(f"Comparator: {len(kalshi_markets)} Kalshi, {len(pm_markets)} Polymarket markets")

    # Find matches and compare prices
    divergences = []
    for km in kalshi_markets:
        for pm in pm_markets:
            score = _fuzzy_match_title(km["title"], pm["title"])
            if score < match_threshold:
                continue
            combined_vol = km["volume"] + pm["volume"]
            if combined_vol < min_vol:
                continue
            delta = abs(km["yes_price"] - pm["yes_price"])
            # Compute percentage divergence relative to midpoint
            midpoint = (km["yes_price"] + pm["yes_price"]) / 2
            delta_pct = (delta / midpoint * 100) if midpoint > 0 else 0
            if delta_pct >= threshold:
                divergences.append({
                    "kalshi_title": km["title"][:60],
                    "pm_title": pm["title"][:60],
                    "kalshi_price": km["yes_price"],
                    "pm_price": pm["yes_price"],
                    "delta": delta,
                    "delta_pct": round(delta_pct, 1),
                    "match_score": score,
                })

    # Save to cache for interactive queries
    cache_path = OPENCLAW_DIR / "state" / "cross_platform_cache.json"
    try:
        with open(cache_path, "w") as f:
            json.dump({
                "matches": divergences[:20],
                "kalshi_count": len(kalshi_markets),
                "pm_count": len(pm_markets),
                "timestamp": time.time(),
            }, f, indent=2)
    except OSError:
        pass

    _log(f"Comparator: {len(divergences)} divergences >= {threshold}%")

    if not divergences:
        return False

    # Sort by largest divergence
    divergences.sort(key=lambda x: x["delta"], reverse=True)
    parts = [f"📊 Cross-platform divergences (>={int(threshold)}%):"]
    for d in divergences[:5]:
        arrow = "↑" if d["kalshi_price"] > d["pm_price"] else "↓"
        parts.append(
            f"  {d['kalshi_title'][:40]}\n"
            f"    Kalshi {d['kalshi_price']}¢ vs PM {d['pm_price']}¢ ({arrow}{d['delta']}%)"
        )

    message = "\n".join(parts)

    # Route through personality
    data = {
        "divergence_count": len(divergences),
        "max_spread": divergences[0]["delta_pct"] if divergences else 0,
        "market_key": divergences[0]["kalshi_title"][:40] if divergences else "",
        "top_divergences": divergences[:3],
    }

    if _HAS_PERSONALITY and not force:
        sent = _personality_send("cross_platform", message, data, state, dry_run=dry_run)
        if sent:
            _log(f"Cross-platform alert (personality): {len(divergences)} divergences")
        return sent

    if _send_message(message, dry_run=dry_run):
        _record_send(state, message)
        _log(f"Cross-platform alert: {len(divergences)} divergences")
        return True
    return False
