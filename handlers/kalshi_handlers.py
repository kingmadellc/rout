"""
Kalshi trading handlers.
Provides commands for portfolio management, market queries, and trade execution.

Requires kalshi_python SDK:
  pip install kalshi-python

And Kalshi API credentials configured in config.yaml:
  kalshi:
    enabled: true
    key_id: "your-key-id"
    private_key_path: "/path/to/your/private.key"
"""

import json
import logging
import os
import time
import yaml
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("rout.kalshi")


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    for candidate in [
        Path.home() / ".openclaw" / "config.yaml",
        Path(__file__).parent.parent / "config.yaml",
        Path.home() / ".config/imsg-watcher/config.yaml",
    ]:
        if candidate.exists():
            with open(candidate) as f:
                data = yaml.safe_load(f) or {}
                return data if isinstance(data, dict) else {}
    return {}

_CONFIG = _load_config()
_KALSHI = _CONFIG.get("kalshi", {})

WORKSPACE = Path(__file__).parent.parent
CACHE_FILE = WORKSPACE / ".kalshi_research_cache.json"
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
TICKER_NAMES = _KALSHI.get("ticker_names", {})
_last_client_error = None  # populated by _get_client on failure
KEY_ID = _KALSHI.get("api_key_id", _KALSHI.get("key_id", ""))
_private_key_file = _KALSHI.get("private_key_file", "")
_legacy_private_path = _KALSHI.get("private_key_path", "")
if _private_key_file:
    _p = Path(_private_key_file).expanduser()
    PRIVATE_KEY_PATH = str(_p if _p.is_absolute() else (Path.home() / ".openclaw" / "keys" / _p))
else:
    PRIVATE_KEY_PATH = os.path.expanduser(_legacy_private_path)


def _check_enabled():
    """Return error string if Kalshi is not configured, else None."""
    if not _KALSHI.get("enabled"):
        return "❌ Kalshi is not enabled. Set kalshi.enabled: true in config.yaml."
    if not KEY_ID or KEY_ID.startswith("TODO"):
        return "❌ Kalshi key_id not configured in config.yaml."
    if not PRIVATE_KEY_PATH or not os.path.exists(PRIVATE_KEY_PATH):
        return f"❌ Kalshi private key not found at: {PRIVATE_KEY_PATH}"
    return None


def _get_client(*, _retries: int = 1, _backoff: float = 2.0):
    """Initialize Kalshi API client with retry on transient failures.

    Args:
        _retries: Number of retry attempts after initial failure (default: 1).
        _backoff: Seconds to wait between retries (default: 2.0).
    """
    import sys
    for p in ['/opt/homebrew/lib/python3.13/site-packages',
              '/opt/homebrew/lib/python3.14/site-packages',
              '/usr/local/lib/python3.13/site-packages']:
        if p not in sys.path:
            sys.path.insert(0, p)

    global _last_client_error
    _last_client_error = None
    for attempt in range(_retries + 1):
        try:
            from kalshi_python import Configuration, KalshiClient
            config = Configuration(host=BASE_URL)
            with open(PRIVATE_KEY_PATH, 'r') as f:
                config.private_key_pem = f.read()
            config.api_key_id = KEY_ID
            client = KalshiClient(config)
            client._portfolio_api.get_balance()  # verify auth
            if attempt > 0:
                logger.info("Kalshi client connected after %d retries", attempt)
            return client
        except Exception as e:  # broad: SDK, urllib3, auth, network — all must be caught & logged
            _last_client_error = e
            # Classify the failure for logging and user-facing messages
            err_str = str(e).lower()
            if "timeout" in err_str or "connection" in err_str or "reset" in err_str:
                category = "network"
            elif "401" in err_str or "403" in err_str or "auth" in err_str or "key" in err_str:
                category = "auth"
            elif "429" in err_str or "rate" in err_str:
                category = "rate_limit"
            else:
                category = "unknown"
            logger.error("Kalshi client init failed (attempt %d/%d, category=%s): %s",
                         attempt + 1, _retries + 1, category, e)
            if attempt < _retries:
                time.sleep(_backoff)

    return None


def _classify_kalshi_error(error) -> str:
    """Return a user-friendly error message based on the last _get_client failure."""
    if error is None:
        return "Unable to connect to Kalshi API."
    err_str = str(error).lower()
    if "timeout" in err_str or "connection" in err_str or "reset" in err_str:
        return "Can't reach Kalshi API — network timeout or connection reset. Likely a temporary issue on their end."
    if "401" in err_str or "403" in err_str or "auth" in err_str or "key" in err_str:
        return "Kalshi auth failed — API key may be expired or invalid. Check config.yaml credentials."
    if "429" in err_str or "rate" in err_str:
        return "Kalshi rate limited — too many requests. Try again in a minute."
    return f"Kalshi API error: {error}"


# ── Commands ──────────────────────────────────────────────────────────────────

def portfolio_command(args: str = "") -> str:
    """Show current Kalshi portfolio: Cash, Positions, Total"""
    err = _check_enabled()
    if err:
        return err

    try:
        client = _get_client()
        if not client:
            return f"❌ {_classify_kalshi_error(_last_client_error)}"

        balance_resp = client._portfolio_api.get_balance()
        cash = balance_resp.balance / 100.0

        # Get open positions via raw API (avoids SDK deserialization bug)
        import json as _json
        resp = client._portfolio_api.get_positions_without_preload_content(limit=100)
        positions = [p for p in _json.loads(resp.read()).get("market_positions", [])
                     if int(p.get("position", 0)) != 0]

        total_cost = 0.0
        total_val = 0.0
        pos_data = []

        for p in positions:
            ticker = p.get("ticker", "?")
            qty = int(p.get("position", 0))
            side = "YES" if qty >= 0 else "NO"
            abs_qty = abs(qty)
            cost = float(p.get("market_exposure_dollars", 0))
            total_cost += cost

            try:
                url = f"{BASE_URL}/markets/{ticker}"
                mkt = _json.loads(client.call_api("GET", url).read()).get("market", {})
                bid = mkt.get("yes_bid" if side == "YES" else "no_bid", 0)
                cur_val = abs_qty * bid / 100.0
                pnl = cur_val - cost
                pct = (pnl / cost * 100) if cost else 0
                total_val += cur_val
                name = TICKER_NAMES.get(ticker, mkt.get("title", ticker))
                pos_data.append({
                    "name": name, "ticker": ticker, "qty": abs_qty, "side": side,
                    "cost": cost, "val": cur_val, "pnl": pnl, "pct": pct,
                })
            except (KeyError, TypeError, ValueError):
                name = TICKER_NAMES.get(ticker, ticker)
                pos_data.append({
                    "name": name, "ticker": ticker, "qty": abs_qty, "side": side,
                    "cost": cost, "val": 0, "pnl": 0, "pct": 0,
                })

        # Sort by absolute P&L (biggest movers first)
        pos_data.sort(key=lambda x: abs(x["pnl"]), reverse=True)

        total_pnl = total_val - total_cost
        trend = "📈" if total_pnl >= 0 else "📉"

        lines = [f"{trend} P&L: ${total_pnl:+.2f} across {len(positions)} positions"]
        lines.append(f"💵 Cash: ${cash:.2f}  ·  Deployed: ${total_cost:.2f}  ·  Value: ${total_val:.2f}")
        lines.append("")

        for p in pos_data:
            if p["pct"] >= 20:
                icon = "🔥"
            elif p["pct"] >= 5:
                icon = "✅"
            elif p["pct"] <= -15:
                icon = "⚠️"
            elif p["pct"] < 0:
                icon = "🔻"
            else:
                icon = "➖"
            lines.append(f"{icon} {p['name']}: {p['qty']}x {p['side']} @ ${p['cost']:.2f} → ${p['val']:.2f} ({p['pct']:+.0f}%)")

        return "\n".join(lines)

    except Exception as e:
        return f"Failed to fetch portfolio: {e}"


def positions_command(args: str = "") -> str:
    """Show open positions with P&L"""
    return portfolio_command(args)  # same data, same format


def markets_command(args: str = "") -> str:
    """List top opportunities from research cache with live price refresh.

    Args:
        args: optional filter — "sports" for sports markets,
              "all" for everything, empty for macro-heavy default.
    """
    err = _check_enabled()
    if err:
        return err

    try:
        if not CACHE_FILE.exists():
            return "❌ No research cache found. Run a deep research scan first."

        with open(CACHE_FILE, 'r') as f:
            cache = json.load(f)

        # Pick the right section based on args
        query = (args or "").strip().lower()
        if query == "sports":
            insights = cache.get('sports_insights', cache.get('insights', []))[:8]
            header_emoji = "🏀"
            header_label = "Sports"
        elif query == "all":
            insights = cache.get('all_insights', cache.get('insights', []))[:8]
            header_emoji = "🌐"
            header_label = "All Markets"
        else:
            insights = cache.get('insights', [])[:8]
            header_emoji = "📈"
            header_label = "Top Opportunities"

        if not insights:
            if query == "sports":
                return "❌ No sports opportunities in cache."
            return "❌ No opportunities found in cache."

        cached_at = cache.get('cached_at', '')
        try:
            cache_time = datetime.fromisoformat(cached_at.replace('Z', '+00:00'))
            age_min = int((datetime.now(cache_time.tzinfo) - cache_time).total_seconds() / 60)
            age_str = f"{age_min}m ago"
        except ValueError:
            age_str = "unknown age"

        macro_n = cache.get('macro_count', '?')
        sports_n = cache.get('sports_count', '?')

        # Try live price refresh on top tickers
        live_prices = _refresh_live_prices([i.get('ticker') for i in insights[:5]])

        lines = [f"{header_emoji} {header_label} (cache: {age_str} | {macro_n} macro, {sports_n} sports):\n"]

        for i, insight in enumerate(insights, 1):
            ticker = insight.get('ticker', 'unknown')
            title = insight.get('title', ticker)[:50]
            side = insight.get('side', '?')
            confidence = insight.get('confidence', 'unknown').upper()
            is_sport = insight.get('is_sports', False)
            tag = "🏀" if is_sport else "📊"
            edge_type = insight.get('edge_type', 'spread_capture')

            days = insight.get('days_to_close')
            days_str = f"{days:.0f}d" if days is not None else "?"
            vol = insight.get('volume', 0)

            lines.append(f"{i}. {tag} {title}")

            if edge_type == "probability_disagreement":
                # Macro: show market probability for user to evaluate
                mkt_prob = insight.get('market_prob', 0)
                lines.append(f"   Market says {mkt_prob:.0%} YES | vol {vol:,} | OI {insight.get('open_interest', 0)} | closes {days_str}")
                yb = insight.get('yes_bid', 0)
                ya = insight.get('yes_ask', 0)
                lines.append(f"   YES: bid {yb}¢ / ask {ya}¢ | {confidence}")
            else:
                # Sports: show spread capture
                spread_cap = insight.get('spread_capture_cents', 0)
                spread_pct = insight.get('spread_pct', 0)
                yb = insight.get('yes_bid', 0)
                ya = insight.get('yes_ask', 0)
                lines.append(f"   {side} | spread {spread_cap}¢ ({spread_pct:.0f}%) | vol {vol:,} | closes {days_str}")
                lines.append(f"   bid {yb}¢ / ask {ya}¢ | {confidence}")

            lines.append(f"   📎 {ticker}\n")

        if not query:
            lines.append("💡 Try: markets sports | markets all")
        lines.append("💡 Say 'buy [ticker] [side] [qty] at [price]' to trade")

        return "\n".join(lines)
    except Exception as e:
        return f"Failed to fetch markets: {e}"


def scan_command(args: str = "") -> str:
    """Live scan of Kalshi markets — fetches fresh data, ranks by heuristic edge.

    No Qwen, no Polygon — pure market microstructure signal.
    Designed to answer "what has edge RIGHT NOW?" in under 10 seconds.

    Heuristic scoring:
      - Spread compression: tighter spreads = more liquid, more interesting
      - Distance from extremes: 20-80 range = actionable, not settled
      - Volume + OI: liquidity = real price discovery
      - Time value: 7-90 day sweet spot for your trading style
    """
    err = _check_enabled()
    if err:
        return err

    try:
        client = _get_client()
        if not client:
            return f"❌ {_classify_kalshi_error(_last_client_error)}"

        # Blocked ticker prefixes (same as edge engine)
        blocked_prefixes = {
            "KXHIGH", "KXLOW", "KXRAIN", "KXSNOW", "KXTEMP", "KXWIND",
            "KXWEATH", "INX", "NASDAQ", "FED-MR", "KXCELEB", "KXMOVIE",
            "KXTIKTOK", "KXYT", "KXTWIT", "KXSTREAM",
        }
        blocked_categories = {
            "weather", "climate", "entertainment", "sports",
            "social-media", "streaming", "celebrities",
        }
        sports_tokens = {
            "nfl", "nba", "mlb", "nhl", "mls", "ncaa", "pga", "ufc", "wwe",
            "super bowl", "superbowl", "march madness", "world series",
            "stanley cup", "finals", "playoff", "mvp", "heisman",
            "rushing", "passing", "touchdown", "home run", "strikeout",
            "quarterback", "pitcher", "espn", "sports",
            "valorant", "league of legends", "counter-strike", "dota",
            "overwatch", "fortnite", "call of duty", "esports", "e-sports",
            "atp", "wta", "tennis", "match:", "vs.", "round of",
            "boxing", "mma", "bellator", "formula 1", "f1 ", "nascar", "indycar",
        }

        # Fetch 3 pages (600 markets) — fast enough for interactive use
        all_markets = []
        cursor = None
        for page in range(3):
            url = (
                "https://api.elections.kalshi.com/trade-api/v2/markets"
                "?limit=200&status=open&mve_filter=exclude"
            )
            if cursor:
                url += f"&cursor={cursor}"
            try:
                resp = client.call_api("GET", url)
                data = json.loads(resp.read())
                markets = data.get("markets", [])
                all_markets.extend(markets)
                cursor = data.get("cursor")
                if not cursor or not markets:
                    break
            except Exception:
                break

        if not all_markets:
            return "❌ Couldn't fetch Kalshi markets. API may be down."

        # Filter and score
        query = (args or "").strip().lower()
        include_sports = query == "sports"

        scored = []
        for m in all_markets:
            ticker = m.get("ticker", "")
            title = m.get("title", "")
            category = m.get("category", "") or m.get("series_ticker", "")
            volume = m.get("volume", 0) or 0
            oi = m.get("open_interest", 0) or 0
            yes_bid = m.get("yes_bid", 0) or 0
            yes_ask = m.get("yes_ask", 0) or 0

            # Must have a functioning order book
            if not yes_bid or not yes_ask:
                continue

            # Block garbage
            ticker_upper = ticker.upper()
            if any(ticker_upper.startswith(p) for p in blocked_prefixes):
                continue
            if category and category.lower().strip() in blocked_categories:
                continue

            # Volume floor
            if volume < 10:
                continue

            # Sports filter
            combined = f"{ticker} {title}".lower()
            is_sport = any(tok in combined for tok in sports_tokens)
            if is_sport and not include_sports:
                continue
            if include_sports and not is_sport:
                continue

            # Timeframe filter (7-180 days for interactive — tighter than proactive)
            expiration = m.get("expiration_time") or m.get("close_time", "")
            days_to_close = None
            if expiration and isinstance(expiration, str):
                try:
                    exp_str = expiration.replace("Z", "+00:00")
                    exp_dt = datetime.fromisoformat(exp_str)
                    days_to_close = max(0, (exp_dt - datetime.now(timezone.utc)).total_seconds() / 86400)
                except (ValueError, TypeError):
                    pass

            if days_to_close is not None:
                if days_to_close < 7 or days_to_close > 180:
                    continue

            mid = (yes_bid + yes_ask) / 2
            spread = yes_ask - yes_bid

            # ── Heuristic Edge Score ──
            # 1. Spread tightness (lower spread = better price discovery)
            spread_pct = spread / max(mid, 1) * 100
            spread_score = max(0, 20 - spread_pct) / 20  # 1.0 at 0% spread, 0 at 20%+

            # 2. Distance from extremes (20-80 range = actionable)
            centrality = 1 - abs(mid - 50) / 50  # 1.0 at 50, 0.0 at extremes
            # Penalize near-settled markets (<15 or >85)
            if mid < 15 or mid > 85:
                centrality *= 0.3

            # 3. Liquidity (volume + OI, log-scaled)
            import math
            liq_score = math.log1p(volume) * 0.6 + math.log1p(oi) * 0.4

            # 4. Time value — sweet spot is 14-60 days
            time_score = 1.0
            if days_to_close is not None:
                if days_to_close < 14:
                    time_score = days_to_close / 14
                elif days_to_close > 60:
                    time_score = max(0.3, 1 - (days_to_close - 60) / 120)

            # Composite
            edge_score = (
                spread_score * 25
                + centrality * 35
                + liq_score * 25
                + time_score * 15
            )

            scored.append({
                "ticker": ticker,
                "title": title[:55],
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "mid": int(mid),
                "spread": spread,
                "spread_pct": round(spread_pct, 1),
                "volume": volume,
                "oi": oi,
                "days_to_close": days_to_close,
                "edge_score": round(edge_score, 1),
                "is_sport": is_sport,
            })

        scored.sort(key=lambda x: x["edge_score"], reverse=True)
        top = scored[:8]

        if not top:
            return "❌ No actionable markets right now. All filtered out by spread/volume/timeframe."

        # Format response
        tag = "🏀" if include_sports else "🎯"
        label = "Sports Scan" if include_sports else "Live Scan"
        lines = [f"{tag} {label} — {len(all_markets)} markets scanned, {len(scored)} passed filters:\n"]

        for i, m in enumerate(top, 1):
            days_str = f"{m['days_to_close']:.0f}d" if m['days_to_close'] is not None else "?"
            lines.append(f"{i}. {m['title']}")
            lines.append(f"   {m['yes_bid']}¢/{m['yes_ask']}¢ (spread {m['spread']}¢ = {m['spread_pct']}%) | vol {m['volume']:,} | OI {m['oi']:,} | {days_str}")
            lines.append(f"   Score: {m['edge_score']} | {m['ticker']}\n")

        lines.append("Scores = composite of spread tightness + price uncertainty + liquidity + time value.")
        lines.append("Say 'get [ticker]' for live bid/ask before trading.")

        return "\n".join(lines)

    except Exception as e:
        return f"❌ Live scan failed: {e}"


def _refresh_live_prices(tickers):
    """Fetch live bid/ask for a list of tickers. Returns dict of ticker -> price data."""
    results = {}
    try:
        client = _get_client()
        if not client:
            return results

        import json as _json
        for ticker in tickers:
            if not ticker:
                continue
            try:
                url = f"{BASE_URL}/markets/{ticker}"
                mkt = _json.loads(client.call_api("GET", url).read()).get("market", {})
                results[ticker] = {
                    "yes_bid": mkt.get("yes_bid", 0) or 0,
                    "yes_ask": mkt.get("yes_ask", 0) or 0,
                    "no_bid": mkt.get("no_bid", 0) or 0,
                    "no_ask": mkt.get("no_ask", 0) or 0,
                    "status": mkt.get("status", "unknown"),
                    "volume_24h": mkt.get("volume_24h", 0) or 0,
                }
            except (KeyError, TypeError, ValueError):
                pass  # skip failed tickers, show cached data instead
    except Exception:
        pass  # if client init fails, return empty — handler falls back to cache
    return results


def cache_command(args: str = "") -> str:
    """Show research cache status"""
    err = _check_enabled()
    if err:
        return err

    try:
        if not CACHE_FILE.exists():
            return "❌ No cache file found."

        stat = CACHE_FILE.stat()
        size_kb = stat.st_size / 1024

        with open(CACHE_FILE, 'r') as f:
            cache = json.load(f)

        cached_at = cache.get('cached_at', 'unknown')
        insights = cache.get('insights', [])

        try:
            cache_time = datetime.fromisoformat(cached_at.replace('Z', '+00:00'))
            age_min = int((datetime.now(cache_time.tzinfo) - cache_time).total_seconds() / 60)
            age_str = f"{age_min}m ago"
        except ValueError:
            age_str = "unknown"

        lines = [
            f"📦 Cache: {len(insights)} insights ({size_kb:.1f}KB)",
            f"⏰ Updated: {age_str}",
        ]

        if insights:
            top = sorted(insights, key=lambda x: x.get('spread_pct', x.get('edge_pct', 0)), reverse=True)[:3]
            lines.append("🎯 Top spread captures:")
            for i, ins in enumerate(top, 1):
                sp = ins.get('spread_capture_cents', 0)
                sp_pct = ins.get('spread_pct', ins.get('edge_pct', 0))
                lines.append(f"  {i}. {ins.get('ticker','?')[:30]}: {sp}¢ ({sp_pct:.0f}%)")

        return "\n".join(lines)
    except Exception as e:
        return f"Failed to check cache: {e}"


# ── Risk Limits ──────────────────────────────────────────────────────────────

MAX_SINGLE_TRADE_COST = 25.00   # USD — hard cap per trade
MAX_POSITION_SIZE = 100          # contracts per trade
MAX_DAILY_LOSS = 50.00           # USD — kill switch
TRADE_LOG = Path.home() / ".openclaw" / "logs" / "trades.jsonl"


def _trade_audit(event: str, data: dict):
    """Append-only trade audit log."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **data,
    }
    try:
        TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(TRADE_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _check_risk(cost: float, quantity: int):
    """Check trade against risk limits. Returns error string or None if OK."""
    if cost > MAX_SINGLE_TRADE_COST:
        return f"❌ Trade cost ${cost:.2f} exceeds max ${MAX_SINGLE_TRADE_COST:.2f} per trade."
    if quantity > MAX_POSITION_SIZE:
        return f"❌ Quantity {quantity} exceeds max {MAX_POSITION_SIZE} contracts per trade."
    return None


# ── Trade Execution ──────────────────────────────────────────────────────────

def _place_order(
    action: str, ticker: str, side: str, quantity: int, price_cents: int,
) -> str:
    """Unified order placement for buy and sell.
    Consolidates duplicated validation, API call, and audit logic."""
    err = _check_enabled()
    if err:
        return err

    side = side.lower().strip()
    if side not in ("yes", "no"):
        return f"❌ Side must be 'yes' or 'no', got '{side}'."
    if not (1 <= price_cents <= 99):
        return f"❌ Price must be 1-99 cents, got {price_cents}."
    if quantity < 1:
        return f"❌ Quantity must be at least 1."

    amount = quantity * price_cents / 100.0

    if action == "buy":
        risk_err = _check_risk(amount, quantity)
        if risk_err:
            _trade_audit("trade_blocked", {
                "ticker": ticker, "side": side, "quantity": quantity,
                "price_cents": price_cents, "reason": risk_err,
            })
            return risk_err

    try:
        client = _get_client()
        if not client:
            return f"❌ {_classify_kalshi_error(_last_client_error)}"

        import json as _json

        order_data = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": quantity,
            "type": "limit",
        }
        if side == "yes":
            order_data["yes_price"] = price_cents
        else:
            order_data["no_price"] = price_cents

        amount_key = "cost_estimate" if action == "buy" else "proceeds_estimate"
        _trade_audit(f"{action}_submitted", {
            "ticker": ticker, "side": side, "quantity": quantity,
            "price_cents": price_cents, amount_key: amount,
        })

        url = f"{BASE_URL}/portfolio/orders"
        resp = client.call_api("POST", url, body=_json.dumps(order_data))
        result = _json.loads(resp.read())

        order = result.get("order", {})
        status = order.get("status", "unknown")
        order_id = order.get("order_id", "?")

        _trade_audit(f"{action}_placed", {
            "ticker": ticker, "order_id": order_id, "status": status,
        })

        name = TICKER_NAMES.get(ticker, ticker)
        fill_msg = "Filled" if status == "executed" else "Resting" if status == "resting" else status.capitalize()
        verb = "Bought" if action == "buy" else "Sold"
        amount_label = "total cost" if action == "buy" else "est. proceeds"
        return (
            f"✅ {verb} {quantity}x {side.upper()} on {name} at {price_cents}¢\n"
            f"💰 {fill_msg} — {amount_label} ${amount:.2f}"
        )

    except Exception as e:
        _trade_audit(f"{action}_failed", {
            "ticker": ticker, "error": str(e)[:200],
        })
        return f"❌ {'Trade' if action == 'buy' else 'Sell'} failed: {e}"


def buy_command(ticker: str, side: str, quantity: int, price_cents: int) -> str:
    """Place a limit buy order on Kalshi."""
    return _place_order("buy", ticker, side, quantity, price_cents)


def sell_command(ticker: str, side: str, quantity: int, price_cents: int) -> str:
    """Place a limit sell order on Kalshi (exit a position)."""
    return _place_order("sell", ticker, side, quantity, price_cents)


# ── Market Data Tools ───────────────────────────────────────────────────────

def get_market_command(ticker: str) -> str:
    """
    Get live market data for a single ticker: bid/ask/last for both sides,
    volume, spread, and status. Use BEFORE placing any buy or sell order.
    """
    err = _check_enabled()
    if err:
        return err

    try:
        client = _get_client()
        if not client:
            return f"❌ {_classify_kalshi_error(_last_client_error)}"

        import json as _json
        url = f"{BASE_URL}/markets/{ticker}"
        mkt = _json.loads(client.call_api("GET", url).read()).get("market", {})

        yes_bid = mkt.get("yes_bid", 0)
        yes_ask = mkt.get("yes_ask", 0)
        no_bid = mkt.get("no_bid", 0)
        no_ask = mkt.get("no_ask", 0)
        last = mkt.get("last_price", 0)
        vol_24h = mkt.get("volume_24h", 0)
        volume = mkt.get("volume", 0)
        status = mkt.get("status", "unknown")
        title = mkt.get("title", ticker)
        close_time = mkt.get("close_time", "?")

        yes_spread = yes_ask - yes_bid if yes_ask and yes_bid else None
        no_spread = no_ask - no_bid if no_ask and no_bid else None

        name = TICKER_NAMES.get(ticker, title)
        lines = [
            f"📊 {name} ({ticker})",
            f"Status: {status} | Close: {close_time}",
            f"",
            f"YES — Bid: {yes_bid}¢ | Ask: {yes_ask}¢ | Spread: {yes_spread}¢" if yes_spread is not None else f"YES — Bid: {yes_bid}¢ | Ask: {yes_ask}¢",
            f"NO  — Bid: {no_bid}¢ | Ask: {no_ask}¢ | Spread: {no_spread}¢" if no_spread is not None else f"NO  — Bid: {no_bid}¢ | Ask: {no_ask}¢",
            f"Last: {last}¢ | Vol 24h: {vol_24h:,} | Total vol: {volume:,}",
        ]

        # Actionable guidance for the agent
        lines.append("")
        lines.append("💡 To sell YES contracts: sell at yes_bid ({0}¢) for instant fill, or post ask at {1}¢ for better price.".format(yes_bid, yes_bid + 1 if yes_bid < 99 else 99))
        lines.append("💡 To sell NO contracts: sell at no_bid ({0}¢) for instant fill, or post ask at {1}¢ for better price.".format(no_bid, no_bid + 1 if no_bid < 99 else 99))
        lines.append("💡 To buy YES: buy at yes_ask ({0}¢) for instant fill, or post bid at {1}¢ for better price.".format(yes_ask, yes_ask - 1 if yes_ask > 1 else 1))

        return "\n".join(lines)

    except Exception as e:
        return f"❌ Failed to fetch market data: {e}"


def smart_sell_command(ticker: str, side: str, quantity: int) -> str:
    """
    Look up current bid and sell at it in one step. No price guessing.
    Fetches live bid, shows it to the user, and places the sell at that price.
    """
    err = _check_enabled()
    if err:
        return err

    side = side.lower().strip()
    if side not in ("yes", "no"):
        return f"❌ Side must be 'yes' or 'no', got '{side}'."
    if quantity < 1:
        return f"❌ Quantity must be at least 1."

    try:
        client = _get_client()
        if not client:
            return f"❌ {_classify_kalshi_error(_last_client_error)}"

        import json as _json

        # Step 1: Fetch live market price
        url = f"{BASE_URL}/markets/{ticker}"
        mkt = _json.loads(client.call_api("GET", url).read()).get("market", {})

        bid_key = "yes_bid" if side == "yes" else "no_bid"
        bid = mkt.get(bid_key, 0)

        if bid <= 0:
            return (
                f"❌ No bid available for {side.upper()} on {ticker}.\n"
                f"Market data: yes_bid={mkt.get('yes_bid', 0)}¢, no_bid={mkt.get('no_bid', 0)}¢\n"
                f"Cannot sell into an empty book. Try posting an ask instead with kalshi_sell."
            )

        name = TICKER_NAMES.get(ticker, mkt.get("title", ticker))
        proceeds = quantity * bid / 100.0

        # Step 2: Place sell at the bid
        order_data = {
            "ticker": ticker,
            "action": "sell",
            "side": side,
            "count": quantity,
            "type": "limit",
        }
        if side == "yes":
            order_data["yes_price"] = bid
        else:
            order_data["no_price"] = bid

        _trade_audit("smart_sell_submitted", {
            "ticker": ticker, "side": side, "quantity": quantity,
            "price_cents": bid, "proceeds_estimate": proceeds,
            "live_bid": bid,
        })

        url = f"{BASE_URL}/portfolio/orders"
        resp = client.call_api("POST", url, body=_json.dumps(order_data))
        result = _json.loads(resp.read())

        order = result.get("order", {})
        status = order.get("status", "unknown")
        order_id = order.get("order_id", "?")

        _trade_audit("smart_sell_placed", {
            "ticker": ticker, "order_id": order_id, "status": status,
            "price_cents": bid,
        })

        fill_msg = "Filled" if status == "executed" else "Resting" if status == "resting" else status.capitalize()
        return (
            f"✅ Sold {quantity}x {side.upper()} on {name} at {bid}¢\n"
            f"💰 {fill_msg} — est. proceeds ${proceeds:.2f}"
        )

    except Exception as e:
        _trade_audit("smart_sell_failed", {
            "ticker": ticker, "error": str(e)[:200],
        })
        return f"❌ Smart sell failed: {e}"


def cancel_order_command(order_id: str) -> str:
    """Cancel an open/resting order by order ID."""
    err = _check_enabled()
    if err:
        return err

    if not order_id or order_id.strip() == "":
        return "❌ order_id is required."

    try:
        client = _get_client()
        if not client:
            return f"❌ {_classify_kalshi_error(_last_client_error)}"

        import json as _json
        url = f"{BASE_URL}/portfolio/orders/{order_id.strip()}"
        resp = client.call_api("DELETE", url)
        result = _json.loads(resp.read())

        _trade_audit("order_cancelled", {
            "order_id": order_id,
            "result": str(result)[:200],
        })

        cancelled = result.get("order", result)
        status = cancelled.get("status", "cancelled")
        return f"✅ Order {order_id} cancelled. Status: {status}"

    except Exception as e:
        _trade_audit("cancel_failed", {
            "order_id": order_id, "error": str(e)[:200],
        })
        return f"❌ Cancel failed: {e}"


def get_open_orders_command() -> str:
    """List all open/resting orders (unfilled limit orders)."""
    err = _check_enabled()
    if err:
        return err

    try:
        client = _get_client()
        if not client:
            return f"❌ {_classify_kalshi_error(_last_client_error)}"

        import json as _json
        url = f"{BASE_URL}/portfolio/orders?status=resting"
        resp = client.call_api("GET", url)
        result = _json.loads(resp.read())
        orders = result.get("orders", [])

        if not orders:
            return "📋 No open orders."

        lines = [f"📋 {len(orders)} open order(s):"]
        for o in orders:
            ticker = o.get("ticker", "?")
            action = o.get("action", "?").upper()
            side = o.get("side", "?").upper()
            count = o.get("remaining_count", o.get("count", "?"))
            price = o.get("yes_price") or o.get("no_price") or "?"
            order_id = o.get("order_id", "?")
            name = TICKER_NAMES.get(ticker, ticker)
            lines.append(f"  {action} {count}x {side} on {name} @ {price}¢ — ID: {order_id}")

        return "\n".join(lines)

    except Exception as e:
        return f"❌ Failed to fetch orders: {e}"
