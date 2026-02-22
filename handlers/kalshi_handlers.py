"""Kalshi trading handlers: portfolio, positions, markets, cache."""

import os
import sys
import time
from pathlib import Path

# Lazy singleton for the Kalshi client
_client = None


def _workspace_root() -> Path:
    env_workspace = os.environ.get("ROUT_WORKSPACE", "").strip()
    if env_workspace:
        p = Path(env_workspace).expanduser()
        if p.exists():
            return p
    return Path(__file__).resolve().parent.parent


def _get_client():
    """Get or create the KalshiClient singleton."""
    global _client
    if _client is not None:
        return _client

    workspace = str(_workspace_root())
    if workspace not in sys.path:
        sys.path.insert(0, workspace)

    try:
        from trading.kalshi_client import KalshiClient
        _client = KalshiClient()
        return _client
    except ImportError:
        return None
    except Exception:
        return None


def portfolio_command(args=None):
    """Show balance and position summary."""
    client = _get_client()
    if not client:
        return "Kalshi client not available. Check trading/kalshi_client.py."

    try:
        balance_resp = client.get_balance()
        balance = float(balance_resp.get("balance", 0)) / 100.0 if isinstance(balance_resp, dict) else float(balance_resp)
        positions = client.get_positions()

        if not positions:
            return f"Balance: ${balance:.2f}\nNo open positions."

        total_invested = sum(
            abs(p.get("total_cost", 0)) / 100 for p in positions
        )
        return (
            f"Portfolio Summary:\n"
            f"  Balance: ${balance:.2f}\n"
            f"  Open positions: {len(positions)}\n"
            f"  Total invested: ${total_invested:.2f}"
        )
    except Exception as e:
        return f"Portfolio error: {e}"


def positions_command(args=None):
    """Show detailed position breakdown (capped at 10 for iMessage)."""
    client = _get_client()
    if not client:
        return "Kalshi client not available."

    try:
        positions = client.get_positions()
        if not positions:
            return "No open positions."

        lines = [f"Open Positions ({len(positions)} total):"]
        for p in positions[:10]:
            ticker = p.get("ticker", "???")
            net_position = p.get("position", 0)
            side = "YES" if net_position > 0 else "NO"
            qty = abs(net_position) or p.get("total_traded", 0)
            cost = abs(p.get("total_cost", 0)) / 100
            lines.append(f"  {ticker}: {side} x{qty} (${cost:.2f})")

        if len(positions) > 10:
            lines.append(f"  ... and {len(positions) - 10} more")

        return "\n".join(lines)
    except Exception as e:
        return f"Positions error: {e}"


def markets_command(args=None):
    """Search or browse markets. Optional query in args."""
    client = _get_client()
    if not client:
        return "Kalshi client not available."

    try:
        query = args.strip() if args else None
        markets = client.get_markets(query=query, limit=5)

        if not markets:
            return f"No markets found{f' for: {query}' if query else ''}."

        lines = [f"Markets{f' matching: {query}' if query else ''}:"]
        for m in markets[:5]:
            title = m.get("title", "???")[:60]
            yes_price = m.get("yes_price", 0)
            volume = m.get("volume", 0)
            lines.append(f"  {title}\n    Yes: {yes_price}¢ | Vol: {volume}")

        return "\n".join(lines)
    except Exception as e:
        return f"Markets error: {e}"


def cache_command(args=None):
    """Show research cache status with freshness check."""
    openclaw_dir = Path.home() / ".openclaw"
    candidates = [
        openclaw_dir / "state" / "kalshi_research_cache.json",
        openclaw_dir / "hardened" / ".kalshi_research_cache.json",
        _workspace_root() / ".kalshi_research_cache.json",
    ]
    cache_path = next((p for p in candidates if p.exists()), None)

    if cache_path is None:
        return "No research cache found."

    try:
        age = time.time() - cache_path.stat().st_mtime
        size = cache_path.stat().st_size

        if age < 3600:
            freshness = f"FRESH ({int(age / 60)}m ago)"
        elif age < 86400:
            freshness = f"STALE ({int(age / 3600)}h ago)"
        else:
            freshness = f"OLD ({int(age / 86400)}d ago)"

        return (
            f"Research Cache:\n"
            f"  Status: {freshness}\n"
            f"  Size: {size:,} bytes"
        )
    except Exception as e:
        return f"Cache error: {e}"
