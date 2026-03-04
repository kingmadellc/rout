# Rout — Coinbase + Polymarket Read-Only Integration

**Author:** Claude (for Matt)
**Date:** Feb 26, 2026
**Status:** Architecture Spec — Phase 1 (Read-Only)

---

## Strategic Context

Rout already has Kalshi trading (buy/sell/portfolio/alerts). Adding Coinbase portfolio tracking and Polymarket odds gives Rout a three-platform financial intelligence layer — all through iMessage. The narrative: *"Rout is the only AI assistant that gives you real-time market intelligence across crypto, equities (via Kalshi event contracts), and prediction markets — in your texts."*

Phase 1 is read-only. Phase 2 (trade execution) is gated on user demand signal from Phase 1.

---

## Scope

### In Scope (Phase 1)
- Coinbase: Portfolio balances, asset prices, price alerts
- Polymarket: Market odds, event browsing, position tracking, odds movement alerts
- Proactive triggers for both (price thresholds, significant moves)

### Out of Scope (Phase 2+)
- Trade execution on Coinbase or Polymarket
- Robinhood (no public API — dead end)
- Cross-platform portfolio aggregation/net worth view (Phase 1.5 candidate)

---

## Architecture

### Pattern: Follow Kalshi

Both integrations should mirror the existing Kalshi module pattern:

```
rout/
  tools/
    coinbase/
      __init__.py
      client.py          # API client (auth, HTTP, rate limiting)
      commands.py         # Command handlers registered with Rout
      cache.py            # Price/portfolio cache with TTL
      alerts.py           # Threshold-based alert logic
    polymarket/
      __init__.py
      client.py
      commands.py
      cache.py
      alerts.py
  triggers/
    coinbase_triggers.py  # Webhook trigger templates
    polymarket_triggers.py
```

### Integration with Existing Systems

```
┌─────────────────────────────────────────────────┐
│                   iMessage (BB)                  │
└──────────────────────┬──────────────────────────┘
                       │
              ┌────────▼────────┐
              │   Rout Agent    │
              │  (dispatcher)   │
              └───┬───┬───┬────┘
                  │   │   │
         ┌────────┘   │   └────────┐
         ▼            ▼            ▼
   ┌──────────┐ ┌──────────┐ ┌──────────┐
   │  Kalshi  │ │ Coinbase │ │Polymarket│
   │          │ │          │ │          │
   └──────────┘ └──────────┘ └──────────┘
                      │            │
              ┌───────▼────────────▼───────┐
              │   Webhook Server (7888)    │
              │   Alert triggers           │
              └────────────────────────────┘
```

---

## Coinbase Integration

### Auth
- **Method:** CDP API Key (ES256 / ECDSA P-256)
- **Flow:** Generate JWT from API key + secret → Bearer token in headers
- **Token lifetime:** Short-lived JWTs generated per-request (no refresh dance)
- **SDK:** `coinbase-advanced-py` (official Python SDK handles auth automatically)

```bash
pip install coinbase-advanced-py
```

### Endpoints Used

| Endpoint | Purpose | Cache TTL |
|----------|---------|-----------|
| `GET /api/v3/brokerage/accounts` | List all accounts + balances | 60s |
| `GET /api/v3/brokerage/portfolios` | List portfolios | 60s |
| `GET /api/v3/brokerage/portfolios/{id}` | Portfolio breakdown | 60s |
| `GET /api/v3/brokerage/market/products` | List tradeable assets | 300s |
| `GET /api/v3/brokerage/market/products/{id}` | Asset price + 24h change | 15s |
| `GET /api/v3/brokerage/market/products/{id}/candles` | Price history (charts) | 300s |

### Commands

| Command | Example | What it does |
|---------|---------|-------------|
| `cb portfolio` | "cb portfolio" | Total portfolio value + top holdings by % |
| `cb price <asset>` | "cb price ETH" | Current price, 24h change, 7d sparkline |
| `cb prices` | "cb prices" | Watchlist prices (configurable) |
| `cb alert <asset> <condition>` | "cb alert BTC above 100k" | Set price threshold alert |
| `cb alerts` | "cb alerts" | List active alerts |
| `cb history <asset>` | "cb history SOL 7d" | Price history summary |

### Alert Triggers

Wired into the existing webhook server trigger system:

```python
# triggers/coinbase_triggers.py
COINBASE_TRIGGERS = [
    {
        "id": "cb_price_alert",
        "type": "polling",           # Poll every 30s for watched assets
        "check": "coinbase.alerts.check_thresholds",
        "template": "🪙 {asset} just hit ${price} ({direction} your {threshold} target). 24h: {change_24h}%",
        "rate_limit": "1/asset/15min"
    },
    {
        "id": "cb_big_move",
        "type": "polling",
        "check": "coinbase.alerts.check_big_moves",
        "template": "⚡ {asset} moved {pct}% in the last {window}. Now ${price}.",
        "config": {
            "threshold_pct": 5,       # Alert on 5%+ moves
            "window_minutes": 60
        },
        "rate_limit": "1/asset/30min"
    },
    {
        "id": "cb_daily_summary",
        "type": "cron",
        "schedule": "0 8 * * *",     # 8 AM daily
        "template": "📊 Morning crypto brief:\n{portfolio_summary}\nBiggest movers: {top_movers}",
        "rate_limit": "1/day"
    }
]
```

### Caching Strategy

```python
# cache.py
CACHE_CONFIG = {
    "portfolio": {"ttl": 60, "key": "cb:portfolio"},
    "price": {"ttl": 15, "key": "cb:price:{asset}"},
    "products": {"ttl": 300, "key": "cb:products"},
    "candles": {"ttl": 300, "key": "cb:candles:{asset}:{granularity}"}
}
```

In-memory dict with TTL expiry (same pattern as Kalshi). No Redis needed at this scale.

---

## Polymarket Integration

### Auth
- **Market data:** No auth required (public API)
- **Position tracking:** Requires wallet address or API key for user-specific data
- **Rate limits:** 1,000 calls/hour free tier (more than enough)

### Base URLs
- CLOB API: `https://clob.polymarket.com`
- Gamma API: `https://gamma-api.polymarket.com` (enriched market metadata)

### Endpoints Used

| Endpoint | Purpose | Cache TTL |
|----------|---------|-----------|
| `GET /markets` | Browse active markets | 120s |
| `GET /markets/{id}` | Single market detail + odds | 30s |
| `GET /book?token_id={id}` | Order book depth | 15s |
| `GET /midpoint?token_id={id}` | Current probability midpoint | 10s |
| `GET /prices-history?market={id}` | Historical odds | 300s |
| Gamma: `GET /events` | Events with grouped markets | 120s |
| Gamma: `GET /events/{slug}` | Single event detail | 60s |

### Commands

| Command | Example | What it does |
|---------|---------|-------------|
| `pm hot` | "pm hot" | Top markets by volume/activity |
| `pm odds <query>` | "pm odds trump" | Search markets, show current odds |
| `pm event <slug>` | "pm event us-election" | Full event with all market options |
| `pm watch <market>` | "pm watch fed-rate-cuts" | Add to watchlist for odds alerts |
| `pm watchlist` | "pm watchlist" | Show all watched markets + current odds |
| `pm history <market>` | "pm history btc-100k" | Odds movement over time |

### Alert Triggers

```python
# triggers/polymarket_triggers.py
POLYMARKET_TRIGGERS = [
    {
        "id": "pm_odds_shift",
        "type": "polling",
        "interval_seconds": 120,
        "check": "polymarket.alerts.check_odds_shifts",
        "template": "📈 {market_title}\nOdds moved {direction} {delta}¢ → now {current_odds}¢\n({timeframe} shift)",
        "config": {
            "threshold_cents": 5,     # Alert on 5¢+ moves
            "window_minutes": 60
        },
        "rate_limit": "1/market/30min"
    },
    {
        "id": "pm_resolution",
        "type": "polling",
        "interval_seconds": 300,
        "check": "polymarket.alerts.check_resolutions",
        "template": "🏁 RESOLVED: {market_title}\nOutcome: {outcome}\nFinal odds were {final_odds}¢",
        "rate_limit": "1/market/once"
    },
    {
        "id": "pm_daily_watchlist",
        "type": "cron",
        "schedule": "0 8 * * *",
        "template": "🔮 Prediction markets update:\n{watchlist_summary}",
        "rate_limit": "1/day"
    }
]
```

### Search Strategy

Polymarket's `/markets` endpoint supports text search. For natural language queries ("what are the odds Trump wins"), extract the search term and hit the endpoint with `closed=false` filter. Return top 3 by volume with current odds.

---

## Unified Morning Brief

The killer feature: combine all three platforms into a single daily push at 8 AM.

```
☀️ Morning Brief — Feb 27

📊 Crypto
Portfolio: $12,340 (+2.1% 24h)
BTC $98,200 (+1.4%) | ETH $3,180 (+3.2%) | SOL $142 (-0.8%)

🔮 Prediction Markets (watchlist)
Fed cuts in 2026: 68¢ (+3¢ overnight)
BTC 100k by March: 42¢ (-5¢)
Trump tariff reversal: 31¢ (new)

📉 Kalshi
S&P above 5500 EOM: 72¢
Next Fed meeting rate: Hold at 88¢

Top mover: SOL +12% (7d) | Biggest odds shift: Fed cuts +8¢ (48h)
```

This is implemented as a composite trigger that pulls from all three caches and formats a single iMessage.

---

## Implementation Sequence

### Week 1: Coinbase Client + Commands
1. Set up `coinbase-advanced-py` SDK, test auth with the operator's CDP API key
2. Implement `client.py` with connection handling, error mapping
3. Implement `cache.py` (clone Kalshi pattern)
4. Ship `cb portfolio`, `cb price`, `cb prices` commands
5. Register commands with Rout dispatcher

### Week 2: Polymarket Client + Commands
1. Implement `client.py` — raw HTTP (no SDK needed, public API)
2. Implement market search with fuzzy matching on titles
3. Ship `pm hot`, `pm odds`, `pm event` commands
4. Ship `pm watch`, `pm watchlist`

### Week 3: Alerts + Proactive Triggers
1. Coinbase price alert polling loop (30s interval for watched assets)
2. Coinbase big-move detection
3. Polymarket odds-shift detection
4. Polymarket resolution alerts
5. Register all triggers with webhook server

### Week 4: Unified Brief + Polish
1. Composite morning brief trigger (all three platforms)
2. Error handling hardening (API down, rate limits, auth expiry)
3. Command help text and natural language aliases
4. Test end-to-end: command → cache → alert → iMessage delivery

---

## Config

```python
# Add to rout config
COINBASE_CONFIG = {
    "api_key_name": "...",           # CDP API key name
    "api_key_secret": "...",         # CDP API private key (ES256)
    "watchlist": ["BTC-USD", "ETH-USD", "SOL-USD"],
    "alert_poll_interval": 30,       # seconds
    "big_move_threshold_pct": 5,
    "big_move_window_min": 60,
}

POLYMARKET_CONFIG = {
    "base_url": "https://clob.polymarket.com",
    "gamma_url": "https://gamma-api.polymarket.com",
    "watchlist": [],                  # Populated via `pm watch`
    "odds_shift_threshold_cents": 5,
    "odds_poll_interval": 120,       # seconds
}
```

---

## Risk / Gotchas

| Risk | Mitigation |
|------|-----------|
| Coinbase API key leakage | Store in env vars or keychain, never in config files |
| Polymarket rate limits (1k/hr) | Cache aggressively, batch market fetches |
| Coinbase JWT clock skew | Use NTP-synced system clock, allow 30s skew in JWT |
| Stale cache during volatile markets | Reduce TTL dynamically when volatility detected |
| iMessage length limits on big briefs | Truncate to top N holdings/markets, offer "more" command |
| Polymarket market search noise | Rank by volume, filter closed markets, fuzzy match titles |

---

## Phase 2 Gate Criteria

Move to Phase 2 (trade execution on Coinbase) only when:
- [ ] 30+ days of stable Phase 1 usage
- [ ] User explicitly requests trade execution (organic demand)
- [ ] Confirmation protocol designed (structured order summary, exact-match confirmation phrase, timeout, optional PIN)
- [ ] Liability/risk framework documented

---

## Demo Script

The "holy shit" moment for this integration:

> **Matt (iMessage):** "cb portfolio"
> **Rout:** "📊 Your crypto portfolio: $12,340 (+2.1% 24h). Top: BTC 58% ($7,157), ETH 24% ($2,961), SOL 18% ($2,221)"
>
> **Matt:** "pm odds fed rate cut"
> **Rout:** "🔮 Fed rate cuts in 2026: 3+ cuts at 68¢, 2 cuts at 22¢, 1 cut at 8¢. Biggest move: 3+ cuts up 8¢ this week."
>
> *(Next morning, unprompted)*
> **Rout:** "☀️ Morning Brief — BTC $99,800 (+4.2% overnight, approaching your $100k alert). Fed 3+ cuts odds jumped to 73¢ (+5¢). Your Kalshi S&P position looking good at 78¢."

That's the product. Three platforms, zero apps opened, all in your texts.
