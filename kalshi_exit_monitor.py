#!/usr/bin/env python3
"""
Kalshi Exit Monitor — runs hourly via launchd.
Alerts via iMessage when positions hit:
  - +20% profit  → take profit suggestion
  - -15% loss    → stop loss alert
  - Expiry ≤ 3 days → expiry warning

Alert state is tracked in .kalshi_exit_monitor_state.json
so the same alert won't fire more than once per day.

Setup:
  1. Set kalshi.enabled: true in config.yaml
  2. Fill in key_id + private_key_path
  3. Install plist: cp launchd/com.rout.kalshi-monitor.plist ~/Library/LaunchAgents/
                    launchctl load ~/Library/LaunchAgents/com.rout.kalshi-monitor.plist
"""

import json
import os
import sys
import subprocess
import time
import yaml
from pathlib import Path
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent

def _load_config() -> dict:
    for candidate in [
        SCRIPT_DIR / "config.yaml",
        Path.home() / ".config/imsg-watcher/config.yaml",
    ]:
        if candidate.exists():
            with open(candidate) as f:
                return yaml.safe_load(f)
    return {}

_CONFIG = _load_config()
_KALSHI = _CONFIG.get("kalshi", {})
_PATHS = _CONFIG.get("paths", {})
_CHATS = _CONFIG.get("chats", {})

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KEY_ID = _KALSHI.get("key_id", "")
PRIVATE_KEY_PATH = os.path.expanduser(_KALSHI.get("private_key_path", ""))
PYTHON = _PATHS.get("python", "/opt/homebrew/bin/python3")
IMSG = _PATHS.get("imsg", "/opt/homebrew/bin/imsg")
ALERT_CHAT_ID = _CHATS.get("personal_id", 1)

STATE_FILE = SCRIPT_DIR / ".kalshi_exit_monitor_state.json"
LOG_FILE = SCRIPT_DIR / "kalshi_exit_monitor.log"

PROFIT_THRESHOLD = 0.20    # alert at +20% profit
LOSS_THRESHOLD = -0.15     # alert at -15% loss
EXPIRY_WARN_DAYS = 3       # warn N days before expiry
ALERT_COOLDOWN_HOURS = 24  # once daily per (ticker, condition)

KALSHI_PATHS = [
    '/opt/homebrew/lib/python3.13/site-packages',
    '/opt/homebrew/lib/python3.14/site-packages',
    '/usr/local/lib/python3.13/site-packages',
]


# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().isoformat(timespec='seconds')
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


# ── State management ──────────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {"alerts": {}}

def save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log(f"⚠️  Failed to save state: {e}")

def should_alert(state: dict, key: str) -> bool:
    last = state["alerts"].get(key, 0)
    return (time.time() - last) / 3600 >= ALERT_COOLDOWN_HOURS

def record_alert(state: dict, key: str):
    state["alerts"][key] = time.time()


# ── iMessage ──────────────────────────────────────────────────────────────────
def send_imessage(text: str):
    try:
        subprocess.run(
            [IMSG, "send", "--chat-id", str(ALERT_CHAT_ID),
             "--service", "imessage", "--text", text],
            timeout=10, check=False, capture_output=True
        )
        log(f"📤 Sent alert")
    except Exception as e:
        log(f"❌ Failed to send: {e}")


# ── Kalshi API ────────────────────────────────────────────────────────────────
def fetch_positions():
    """Fetch all open positions with current market prices via subprocess."""
    script = f"""
import sys, json
for p in {json.dumps([str(x) for x in KALSHI_PATHS])}:
    sys.path.insert(0, p)

from kalshi_python import Configuration, KalshiClient

config = Configuration(host="{BASE_URL}")
with open("{PRIVATE_KEY_PATH}", "r") as f:
    config.private_key_pem = f.read()
config.api_key_id = "{KEY_ID}"
client = KalshiClient(config)

cash = client.get_balance().balance / 100.0
resp = client._portfolio_api.get_positions_without_preload_content(limit=100)
positions = [p for p in json.loads(resp.read()).get("market_positions", [])
             if int(p.get("position", 0)) != 0]

results = []
for p in positions:
    ticker = p.get("ticker", "?")
    qty = int(p.get("position", 0))
    side = "YES" if qty >= 0 else "NO"
    abs_qty = abs(qty)
    cost = float(p.get("market_exposure_dollars", 0))

    cur_val = expiry_ts = None
    try:
        url = f"{BASE_URL}/markets/{{ticker}}"
        mkt = json.loads(client.call_api("GET", url).read()).get("market", {{}})
        bid = mkt.get("yes_bid" if side == "YES" else "no_bid", 0)
        cur_val = abs_qty * bid / 100.0
        expiry_ts = mkt.get("expiration_time")
    except Exception:
        pass

    results.append({{
        "ticker": ticker, "qty": abs_qty, "side": side,
        "cost": cost, "cur_val": cur_val, "expiry_ts": expiry_ts,
    }})

print(json.dumps({{"cash": cash, "positions": results}}))
"""
    result = subprocess.run(
        [PYTHON, "-c", script],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"Kalshi API error: {result.stderr[:200]}")
    return json.loads(result.stdout)


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    if not _KALSHI.get("enabled"):
        log("Kalshi not enabled in config.yaml — exiting.")
        return

    if not KEY_ID or KEY_ID.startswith("TODO"):
        log("Kalshi key_id not configured — exiting.")
        return

    log("🔍 Kalshi exit monitor running...")
    state = load_state()
    alerts_fired = []

    try:
        data = fetch_positions()
    except Exception as e:
        log(f"❌ Failed to fetch positions: {e}")
        return

    cash = data.get("cash", 0)
    positions = data.get("positions", [])
    log(f"  Cash: ${cash:.2f} | Positions: {len(positions)}")
    now = datetime.now(timezone.utc)

    for p in positions:
        ticker = p["ticker"]
        qty = p["qty"]
        side = p["side"]
        cost = p["cost"]
        cur_val = p["cur_val"]
        expiry_ts = p["expiry_ts"]

        # ── P&L alerts ────────────────────────────────────────────────────────
        if cur_val is not None and cost > 0:
            pnl = cur_val - cost
            pct = pnl / cost
            log(f"  {ticker}: {qty}x {side} | ${cost:.2f} → ${cur_val:.2f} | {pct:+.0%}")

            if pct >= PROFIT_THRESHOLD:
                key = f"{ticker}:profit"
                if should_alert(state, key):
                    send_imessage(
                        f"💰 Kalshi exit alert: {ticker}\n"
                        f"{qty}x {side} — up {pct:+.0%} (${pnl:+.2f} profit)\n"
                        f"Cost ${cost:.2f} → now ${cur_val:.2f}\n"
                        f"Consider taking profits 📈"
                    )
                    record_alert(state, key)
                    alerts_fired.append(f"{ticker} PROFIT {pct:+.0%}")

            elif pct <= LOSS_THRESHOLD:
                key = f"{ticker}:loss"
                if should_alert(state, key):
                    send_imessage(
                        f"🛑 Kalshi stop loss: {ticker}\n"
                        f"{qty}x {side} — down {pct:+.0%} (${pnl:+.2f})\n"
                        f"Stop loss threshold hit ⚠️"
                    )
                    record_alert(state, key)
                    alerts_fired.append(f"{ticker} LOSS {pct:+.0%}")

        # ── Expiry alerts ──────────────────────────────────────────────────────
        if expiry_ts:
            try:
                exp = datetime.fromisoformat(expiry_ts.replace("Z", "+00:00"))
                days_left = (exp - now).total_seconds() / 86400
                if 0 < days_left <= EXPIRY_WARN_DAYS:
                    key = f"{ticker}:expiry"
                    if should_alert(state, key):
                        pnl_str = ""
                        if cur_val is not None and cost > 0:
                            pnl_str = f" | P&L: {(cur_val-cost)/cost:+.0%} (${cur_val-cost:+.2f})"
                        send_imessage(
                            f"⏰ Kalshi expiry: {ticker}\n"
                            f"{qty}x {side} expires in {days_left:.1f} days "
                            f"({exp.strftime('%b %d')}){pnl_str}\n"
                            f"Hold or exit? 🕐"
                        )
                        record_alert(state, key)
                        alerts_fired.append(f"{ticker} EXPIRY {days_left:.1f}d")
            except Exception as e:
                log(f"    ⚠️  Expiry parse error for {ticker}: {e}")

    save_state(state)
    if alerts_fired:
        log(f"✅ Done — {len(alerts_fired)} alert(s): {alerts_fired}")
    else:
        log(f"✅ Done — no alerts this run")


if __name__ == "__main__":
    run()
