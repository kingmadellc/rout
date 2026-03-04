"""Edge Engine — replaces legacy kalshi_edge.py + kalshi_research.py.

The fundamental shift: instead of sorting markets by spread and distance-from-50
(which surfaces weather and noise), this engine:

1. Fetches Kalshi markets with category + timeframe filtering
2. Enriches with Polygon real-world data (news, economic indicators)
3. Runs local Qwen to estimate independent probability
4. Calculates real edge: |market_price - qwen_estimate|
5. Alerts only on markets with genuine mispricing above threshold

Two modes:
  - PROACTIVE (iMessage alert): High-edge opportunities matching portfolio style
  - CACHE (silent): Populates research cache for `markets` command
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from proactive.base import (
    PROACTIVE_CFG,
    OPENCLAW_DIR,
    PROJECT_ROOT,
    _log,
    _send_message,
    _record_send,
    _record_scanner_run,
)

# Personality-aware send
try:
    from proactive.personality.engine import personality_send as _personality_send
    _HAS_PERSONALITY = True
except ImportError:
    _HAS_PERSONALITY = False


# ── Category Filtering ──────────────────────────────────────────────────────

# Categories that match your portfolio style (political/macro event risk)
_ALLOWED_CATEGORIES = {
    "politics", "policy", "government", "election", "geopolitics",
    "economics", "macro", "fed", "regulation", "legal", "trade",
    "crypto", "finance", "technology", "ai",
}

# Ticker prefixes to BLOCK — high-volume garbage that isn't your game
_BLOCKED_TICKER_PREFIXES = {
    "KXHIGH",     # Weather highs
    "KXLOW",      # Weather lows
    "KXRAIN",     # Weather rain
    "KXSNOW",     # Weather snow
    "KXTEMP",     # Weather temperature
    "KXWIND",     # Weather wind
    "KXWEATH",    # Weather general
    "INX",        # Intraday index (S&P settlement range bets — coin flip)
    "NASDAQ",     # Intraday index
    "FED-MR",     # Fed meeting minute-range (noise bets)
    "KXCELEB",    # Celebrity/entertainment
    "KXMOVIE",    # Box office / entertainment
    "KXTIKTOK",   # Social media novelty
    "KXYT",       # YouTube novelty
    "KXTWIT",     # Twitter/X follower bets
    "KXSTREAM",   # Streaming metrics
}

# Event category slugs from Kalshi API to BLOCK
# (Kalshi returns category info in market data — use it)
_BLOCKED_CATEGORIES_API = {
    "weather", "climate", "entertainment", "sports",
    "social-media", "streaming", "celebrities",
}

# Sports tokens to categorize (still tracked, just separated)
_SPORTS_TOKENS = {
    # Major leagues
    "nfl", "nba", "mlb", "nhl", "mls", "ncaa", "pga", "ufc", "wwe",
    "super bowl", "superbowl", "march madness", "world series",
    "stanley cup", "finals", "playoff", "mvp", "heisman",
    "rushing", "passing", "touchdown", "home run", "strikeout",
    "quarterback", "pitcher", "espn", "sports",
    # Esports
    "valorant", "league of legends", "counter-strike", "dota",
    "overwatch", "fortnite", "call of duty", "esports", "e-sports",
    "nrg", "paper rex", "fnatic", "sentinels", "team liquid",
    "gentle mates", "cloud9", "100 thieves", "faze", "optic",
    # Individual sports / tennis / golf / fighting
    "atp", "wta", "tennis", "match:", "vs.", "round of",
    "budkov", "molcan", "reis da silva", "soto",  # tennis players from scan
    "boxing", "mma", "bellator",
    # Racing
    "formula 1", "f1 ", "nascar", "indycar",
}


def _is_blocked(ticker: str, category: str = "") -> bool:
    """Check if a market should be excluded from analysis.

    Uses both ticker prefix matching AND API category field.
    This is a BLOCKLIST approach — block known bad, let everything else through.
    """
    ticker_upper = ticker.upper()
    if any(ticker_upper.startswith(prefix) for prefix in _BLOCKED_TICKER_PREFIXES):
        return True
    if category and category.lower().strip() in _BLOCKED_CATEGORIES_API:
        return True
    return False


def _is_sports(ticker: str, title: str) -> bool:
    combined = f"{ticker} {title}".lower()
    return any(tok in combined for tok in _SPORTS_TOKENS)


def _passes_category_filter(ticker: str, title: str) -> bool:
    """Check if market matches allowed categories based on title keywords."""
    combined = f"{ticker} {title}".lower()
    return any(cat in combined for cat in _ALLOWED_CATEGORIES)


# ── Market Fetching ─────────────────────────────────────────────────────────

def _fetch_kalshi_markets(cfg: dict) -> list[dict]:
    """Fetch and pre-filter Kalshi markets."""
    try:
        from handlers.kalshi_handlers import _get_client, _check_enabled

        err = _check_enabled()
        if err:
            _log(f"Edge engine: {err}")
            return []

        client = _get_client()
        if not client:
            _log("Edge engine: client init failed")
            return []

        min_volume = cfg.get("min_volume", 50)
        min_days = cfg.get("min_days_to_close", 7)
        max_days = cfg.get("max_days_to_close", 365)
        max_pages = cfg.get("max_pages", 10)

        all_markets = []
        cursor = None
        fetch_start = time.time()
        max_fetch_seconds = cfg.get("max_fetch_seconds", 30)  # total fetch budget
        page_timeout = cfg.get("page_timeout_seconds", 8)     # per-page timeout

        for page in range(max_pages):
            # Enforce total fetch budget
            if time.time() - fetch_start > max_fetch_seconds:
                _log(f"Edge engine: hit {max_fetch_seconds}s total fetch budget at page {page}")
                break
            url = (
                "https://api.elections.kalshi.com/trade-api/v2/markets"
                "?limit=200&status=open&mve_filter=exclude"
            )
            if cursor:
                url += f"&cursor={cursor}"
            try:
                import signal as _sig

                # Per-page timeout using alarm (Unix only, main thread only)
                # Falls back to no per-page timeout if alarm isn't available
                _alarm_available = hasattr(_sig, 'SIGALRM') and hasattr(_sig, 'alarm')
                _old_handler = None
                if _alarm_available:
                    def _timeout_handler(signum, frame):
                        raise TimeoutError(f"Page {page} fetch exceeded {page_timeout}s")
                    try:
                        _old_handler = _sig.signal(_sig.SIGALRM, _timeout_handler)
                        _sig.alarm(page_timeout)
                    except (ValueError, OSError):
                        _alarm_available = False

                try:
                    resp = client.call_api("GET", url)
                    data = json.loads(resp.read())
                finally:
                    if _alarm_available:
                        _sig.alarm(0)
                        if _old_handler is not None:
                            _sig.signal(_sig.SIGALRM, _old_handler)

                markets = data.get("markets", [])
                all_markets.extend(markets)
                cursor = data.get("cursor")
                if not cursor or not markets:
                    break
            except TimeoutError:
                _log(f"Edge engine: page {page} timed out after {page_timeout}s — stopping pagination")
                break
            except Exception as e:
                _log(f"Edge engine: page {page} fetch error: {e}")
                break

        _log(f"Edge engine: fetched {len(all_markets)} raw markets")

        # Pre-filter
        filtered = []
        blocked_count = 0
        sports_count = 0
        category_miss = 0
        volume_miss = 0
        timeframe_miss = 0

        for m in all_markets:
            ticker = m.get("ticker", "")
            title = m.get("title", "")
            category = m.get("category", "") or m.get("series_ticker", "")
            volume = m.get("volume", 0) or 0
            yes_bid = m.get("yes_bid", 0) or 0
            yes_ask = m.get("yes_ask", 0) or 0

            # Must have a functioning order book
            if not yes_bid or not yes_ask:
                continue

            # Block garbage categories (ticker prefix + API category)
            if _is_blocked(ticker, category):
                blocked_count += 1
                continue

            # Volume floor
            if volume < min_volume:
                volume_miss += 1
                continue

            # Sports → separate track (not blocked, just deprioritized)
            if _is_sports(ticker, title):
                sports_count += 1
                continue

            # Timeframe filter
            days_to_close = _calc_days_to_close(m)
            if days_to_close is not None:
                if days_to_close < min_days:
                    timeframe_miss += 1
                    continue
                if days_to_close > max_days:
                    timeframe_miss += 1
                    continue

            mid = (yes_bid + yes_ask) / 2
            spread = yes_ask - yes_bid

            filtered.append({
                "ticker": ticker,
                "title": title[:80],
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "yes_price": int(mid),
                "spread": spread,
                "volume": volume,
                "open_interest": m.get("open_interest", 0) or 0,
                "days_to_close": days_to_close,
                "expiration_time": m.get("expiration_time", ""),
            })

        _log(
            f"Edge engine: {len(filtered)} passed filters "
            f"(blocked={blocked_count}, sports={sports_count}, "
            f"volume={volume_miss}, timeframe={timeframe_miss}, "
            f"category={category_miss})"
        )
        return filtered

    except Exception as e:
        _log(f"Edge engine fetch error: {e}")
        return []


def _calc_days_to_close(m: dict) -> float | None:
    """Calculate days until market closes."""
    expiration = m.get("expiration_time") or m.get("close_time", "")
    if not expiration or not isinstance(expiration, str):
        return None
    try:
        exp_str = expiration.replace("Z", "+00:00")
        exp_dt = datetime.fromisoformat(exp_str)
        return max(0, (exp_dt - datetime.now(timezone.utc)).total_seconds() / 86400)
    except (ValueError, TypeError):
        return None


def _dedup_by_event(markets: list[dict], limit: int) -> list[dict]:
    """Deduplicate markets by event — keep only the highest-priority per event series.

    Kalshi creates many sub-markets per event (e.g. 8 variants of "What will Hochul say?").
    This extracts the event "stem" from the title and keeps only one per stem.
    Markets are assumed to already be sorted by priority (highest first).
    """
    seen_stems = {}
    deduped = []

    for m in markets:
        stem = _extract_event_stem(m.get("title", ""), m.get("ticker", ""))
        if stem in seen_stems:
            continue
        seen_stems[stem] = True
        deduped.append(m)
        if len(deduped) >= limit:
            break

    return deduped


def _extract_event_stem(title: str, ticker: str) -> str:
    """Extract the event 'stem' from a market title for dedup purposes.

    Strategy: use the event_ticker prefix from the Kalshi ticker (before the last hyphen segment),
    falling back to the first N significant words of the title.

    Examples:
        "KXHOCHUL-26MAR02-A" → "KXHOCHUL-26MAR02"  (all Hochul speech variants share this)
        "Will gas prices be above $3.06?" → "will gas prices be above"
    """
    # Strategy 1: Ticker-based stem (most reliable)
    # Kalshi tickers are like "KXHOCHUL-26MAR02-A", "KXHOCHUL-26MAR02-B"
    # The event stem is everything before the last segment
    if ticker and "-" in ticker:
        parts = ticker.split("-")
        if len(parts) >= 3:
            return "-".join(parts[:-1])  # Drop the variant suffix
        elif len(parts) == 2:
            return parts[0]  # Just the category prefix

    # Strategy 2: Title-based stem (fallback)
    # Take first 6 significant words
    stopwords = {"will", "the", "a", "an", "in", "on", "by", "of", "to", "for", "be", "is", "?"}
    words = [w.lower().strip("?.,!*") for w in title.split()]
    sig_words = [w for w in words if w and w not in stopwords][:6]
    return " ".join(sig_words) if sig_words else title[:40].lower()


# ── Edge Calculation ────────────────────────────────────────────────────────

def _calculate_edges(markets: list[dict], cfg: dict) -> list[dict]:
    """Run Qwen probability estimation and calculate real edge for each market.

    This is where the magic happens. For each market:
    1. Fetch related Polygon news
    2. Get economic context
    3. Ask Qwen for independent probability estimate
    4. edge = |market_price - qwen_estimate|
    """
    from proactive.triggers.polygon_data import (
        get_related_news_for_market,
        get_economic_indicators,
        is_polygon_configured,
    )
    from proactive.triggers.qwen_analyzer import estimate_batch

    # Get economic context once (shared across all markets)
    economic_context = None
    if is_polygon_configured():
        _log("Edge engine: fetching Polygon economic indicators...")
        economic_context = get_economic_indicators()
        _log(f"Edge engine: got {len(economic_context or {})} indicators")
    else:
        _log("Edge engine: Polygon not configured — running without economic context")

    # Enrich markets with news
    max_to_analyze = cfg.get("max_markets_to_analyze", 20)
    use_polygon = is_polygon_configured()

    # Sort by a simple heuristic to prioritize which markets get Qwen analysis:
    # Higher OI + reasonable distance from extremes → more interesting
    def _priority_score(m: dict) -> float:
        mid = m.get("yes_price", 50)
        oi = m.get("open_interest", 0)
        # Markets near 20-80 range are more interesting than 5 or 95
        centrality = 1 - abs(mid - 50) / 50  # 1.0 at 50, 0.0 at extremes
        return centrality * (oi ** 0.3) * (m.get("volume", 0) ** 0.2)

    markets.sort(key=_priority_score, reverse=True)

    # Dedup by event/series — keep only the highest-priority market per event
    # Kalshi often has 5-10 variants of the same event (e.g. "Hochul says X", "Hochul says Y")
    # We only want to analyze one per event to avoid wasting Qwen budget
    candidates = _dedup_by_event(markets, max_to_analyze)

    _log(f"Edge engine: analyzing top {len(candidates)} markets with Qwen (after event dedup)...")

    # Enrich with Polygon news
    if use_polygon:
        for m in candidates:
            m["news"] = get_related_news_for_market(m["title"])
    else:
        for m in candidates:
            m["news"] = []

    # Load X signal cache (from existing x_signals trigger)
    x_cache = _load_x_signal_cache()
    if x_cache:
        for m in candidates:
            m["x_signal"] = _match_x_signal(m["title"], x_cache)

    # Run Qwen batch analysis
    results = estimate_batch(
        candidates,
        economic_context=economic_context,
        max_markets=max_to_analyze,
    )

    # Filter by minimum edge
    min_edge = cfg.get("min_edge_pct", 8.0)
    min_confidence = cfg.get("min_qwen_confidence", 0.4)

    edges = [
        r for r in results
        if r.get("edge_pct", 0) >= min_edge
        and r.get("confidence", 0) >= min_confidence
    ]

    edges.sort(key=lambda x: x.get("edge_pct", 0), reverse=True)
    _log(f"Edge engine: {len(edges)} markets with edge >= {min_edge}% (confidence >= {min_confidence})")

    return edges


def _load_x_signal_cache() -> list[dict]:
    """Load cached X signals for cross-referencing."""
    cache_path = OPENCLAW_DIR / "state" / "x_signal_cache.json"
    try:
        if cache_path.exists():
            data = json.loads(cache_path.read_text())
            # Only use signals from last 4 hours
            if time.time() - data.get("timestamp", 0) < 14400:
                return data.get("signals", [])
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _match_x_signal(title: str, signals: list[dict]) -> dict | None:
    """Match an X signal to a market by keyword overlap."""
    title_words = set(title.lower().split())
    stopwords = {"will", "the", "a", "in", "on", "by", "of", "to", "for", "be", "is", "?"}
    title_words -= stopwords

    best_match = None
    best_overlap = 0

    for sig in signals:
        topic_words = set(sig.get("topic", "").lower().split())
        overlap = len(title_words & topic_words)
        if overlap >= 2 and overlap > best_overlap:
            best_overlap = overlap
            best_match = sig

    return best_match


# ── Main Trigger ────────────────────────────────────────────────────────────

def check_edge_engine(state: dict, dry_run: bool = False, force: bool = False) -> bool:
    """Unified edge engine — replaces legacy kalshi_edge + kalshi_research.

    Runs in two phases:
    1. CACHE: Always populates .kalshi_research_cache.json for `markets` command
    2. ALERT: Sends iMessage for high-edge opportunities (if any beat threshold)
    """
    cfg = PROACTIVE_CFG.get("edge_engine", {})
    if not cfg.get("enabled", True):
        return False

    interval = cfg.get("check_interval_minutes", 180)
    last_check = state.get("last_edge_engine_check", 0)
    if not force and time.time() - last_check < interval * 60:
        return False

    state["last_edge_engine_check"] = time.time()
    alert_threshold = cfg.get("alert_edge_pct", 12.0)
    max_alerts = cfg.get("max_alerts", 3)

    _log("=" * 60)
    _log("Edge engine starting...")
    _log("=" * 60)

    # Phase 1: Fetch + filter markets
    markets = _fetch_kalshi_markets(cfg)
    if not markets:
        _log("Edge engine: no markets passed filters")
        _record_scanner_run("edge_engine", signals_found=0, signals_passed=0, signals_blocked=0)
        return False

    # Phase 2: Calculate edges with Qwen + Polygon
    edges = _calculate_edges(markets, cfg)

    # Phase 3: Write research cache (always — this feeds the `markets` command)
    cache_payload = {
        "insights": [_format_insight(e) for e in edges[:20]],
        "all_insights": [_format_insight(e) for e in edges[:20]],
        "macro_count": len(edges),
        "sports_count": 0,  # Sports are filtered out
        "total_scanned": len(markets),
        "engine_version": "0.8.0",
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }

    cache_path = PROJECT_ROOT / ".kalshi_research_cache.json"
    try:
        with open(cache_path, "w") as f:
            json.dump(cache_payload, f, indent=2)
        _log(f"Edge engine: cache written — {len(edges)} insights ({cache_path})")
    except OSError as e:
        _log(f"Edge engine: cache write FAILED: {e}")

    # Also write detailed edge data for analysis
    edge_detail_path = OPENCLAW_DIR / "state" / "edge_engine_results.json"
    try:
        with open(edge_detail_path, "w") as f:
            json.dump({
                "edges": edges[:30],
                "markets_scanned": len(markets),
                "timestamp": time.time(),
            }, f, indent=2)
    except OSError:
        pass

    # Phase 4: Alert on high-edge opportunities
    alert_candidates = [e for e in edges if e.get("edge_pct", 0) >= alert_threshold]
    if not alert_candidates:
        _log(f"Edge engine: no markets above alert threshold ({alert_threshold}%)")
        _record_scanner_run("edge_engine", signals_found=len(edges), signals_passed=0, signals_blocked=len(edges))
        return True  # Cache was updated, just no alerts

    top_alerts = alert_candidates[:max_alerts]
    parts = [f"🎯 Edge Engine — {len(alert_candidates)} opportunities:"]

    for e in top_alerts:
        direction_icon = "📈" if e.get("direction") == "underpriced" else "📉"
        side = "YES" if e.get("direction") == "underpriced" else "NO"
        est = e.get("estimated_probability", 0)
        mkt = e.get("market_implied", 0)
        edge = e.get("edge_pct", 0)
        conf = e.get("confidence", 0)
        days = e.get("days_to_close")
        days_str = f"{days:.0f}d" if days is not None else "?"

        parts.append(
            f"\n{direction_icon} {e.get('title', '?')[:55]}\n"
            f"  Mkt: {mkt:.0%} → Qwen: {est:.0%} = {edge:.0f}% edge ({side})\n"
            f"  Conf: {conf:.0%} | {days_str} | vol {e.get('volume', 0):,}\n"
            f"  {e.get('reasoning', '')[:80]}"
        )

    message = "\n".join(parts)

    # Route through personality
    data = {
        "alert_count": len(alert_candidates),
        "top_edge_pct": top_alerts[0].get("edge_pct", 0) if top_alerts else 0,
        "top_ticker": top_alerts[0].get("ticker", "") if top_alerts else "",
        "top_title": top_alerts[0].get("title", "")[:40] if top_alerts else "",
        "markets_scanned": len(markets),
    }

    if _HAS_PERSONALITY and not force:
        sent = _personality_send("edge", message, data, state, dry_run=dry_run)
        if sent:
            _log(f"Edge engine: sent {len(top_alerts)} alerts (personality)")
            _record_scanner_run("edge_engine", signals_found=len(edges), signals_passed=len(top_alerts), signals_blocked=len(edges) - len(top_alerts))
        else:
            _record_scanner_run("edge_engine", signals_found=len(edges), signals_passed=0, signals_blocked=len(edges))
        return True  # Cache was updated regardless

    if _send_message(message, dry_run=dry_run):
        _record_send(state, message)
        _log(f"Edge engine: sent {len(top_alerts)} alerts")
        _record_scanner_run("edge_engine", signals_found=len(edges), signals_passed=len(top_alerts), signals_blocked=len(edges) - len(top_alerts))
        return True

    # Message send failed
    _record_scanner_run("edge_engine", signals_found=len(edges), signals_passed=0, signals_blocked=len(edges))
    return True  # Cache updated even if message failed


def _format_insight(edge: dict) -> dict:
    """Format an edge result for the research cache (backward-compatible with markets command)."""
    est = edge.get("estimated_probability", 0.5)
    mkt = edge.get("market_implied", 0.5)
    direction = edge.get("direction", "fair")

    return {
        "ticker": edge.get("ticker", "?"),
        "title": edge.get("title", "?")[:60],
        "side": "YES" if direction == "underpriced" else "NO",
        "confidence": "high" if edge.get("confidence", 0) > 0.6 else "medium",
        "yes_bid": edge.get("yes_bid", 0),
        "yes_ask": edge.get("yes_ask", 0),
        "volume": edge.get("volume", 0),
        "open_interest": edge.get("open_interest", 0),
        "days_to_close": edge.get("days_to_close"),
        "edge_type": "qwen_probability",
        "spread_capture_cents": edge.get("spread", 0),
        "spread_pct": round(edge.get("spread", 0) / max(edge.get("yes_price", 50), 1) * 100, 1),
        "market_prob": round(mkt, 4),
        "estimated_prob": round(est, 4),
        "edge_pct": edge.get("edge_pct", 0),
        "direction": direction,
        "reasoning": edge.get("reasoning", ""),
        "is_sports": False,
    }
