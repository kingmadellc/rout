"""Portfolio drift trigger."""

import time
from proactive.base import (
    PROACTIVE_CFG,
    _log,
    _get_current_portfolio,
    _load_portfolio_snapshot,
    _save_portfolio_snapshot,
    _send_message,
    _record_send,
)

# Personality-aware send
try:
    from proactive.personality.engine import personality_send as _personality_send
    _HAS_PERSONALITY = True
except ImportError:
    _HAS_PERSONALITY = False


def check_portfolio_drift(state: dict, dry_run: bool = False, force: bool = False) -> bool:
    """Alert if any position has moved significantly since last check."""
    cfg = PROACTIVE_CFG.get("portfolio_drift", {})
    if not cfg.get("enabled", True):
        return False

    threshold = cfg.get("threshold_pct", 5.0)
    interval = cfg.get("check_interval_minutes", 60)

    # Rate limit: don't check too frequently
    last_check = state.get("last_portfolio_check", 0)
    if time.time() - last_check < interval * 60:
        return False

    state["last_portfolio_check"] = time.time()

    current = _get_current_portfolio()
    if not current or not current.get("positions"):
        return False

    previous = _load_portfolio_snapshot()
    prev_positions = previous.get("positions", {})

    # Compare positions
    alerts = []
    for ticker, current_pct in current["positions"].items():
        prev_pct = prev_positions.get(ticker)
        if prev_pct is not None:
            delta = current_pct - prev_pct
            if abs(delta) >= threshold:
                direction = "📈" if delta > 0 else "📉"
                alerts.append(f"{direction} {ticker}: {delta:+.1f}% (now {current_pct:+.1f}%)")

    # Save current as new snapshot
    _save_portfolio_snapshot(current)

    if not alerts:
        return False

    message = "Portfolio alert:\n" + "\n".join(alerts[:5])

    # Route through personality
    data = {
        "alert_count": len(alerts),
        "threshold": threshold,
        "tickers": [a.split(":")[0].strip().lstrip("📈📉 ") for a in alerts[:5]],
        "max_delta": max(abs(current["positions"].get(t, 0) - prev_positions.get(t, 0))
                        for t in current["positions"] if t in prev_positions) if prev_positions else 0,
    }

    if _HAS_PERSONALITY and not force:
        sent = _personality_send("portfolio", message, data, state, dry_run=dry_run)
        if sent:
            _log(f"Portfolio drift alert (personality): {len(alerts)} positions moved >{threshold}%")
        return sent

    if _send_message(message, dry_run=dry_run):
        _record_send(state)
        _log(f"Portfolio drift alert: {len(alerts)} positions moved >{threshold}%")
        return True
    return False
