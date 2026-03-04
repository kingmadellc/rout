"""Editorial voice — Rout has opinions, not just data.

Transforms raw trigger output from "BTC at $67,400" into
"BTC grinding sideways. Boring." or "Wild day — BTC ripped 8% overnight."

This is the single biggest lever for perceived agency.
One sentence of opinion does more than any amount of formatting.
"""

import random
import time
from datetime import datetime


def editorialize(trigger: str, data: dict, message: str) -> str:
    """Add editorial flavor to a proactive message.

    Args:
        trigger: Which trigger produced this (cross_platform, portfolio, etc.)
        data: Structured data from the trigger (spread values, P&L, etc.)
        message: The raw message that would be sent without personality

    Returns:
        The message with editorial voice prepended/appended.
    """
    editorial = _generate_editorial(trigger, data)
    if not editorial:
        return message

    # Prepend the editorial take, then the data
    return f"{editorial}\n\n{message}"


def _generate_editorial(trigger: str, data: dict) -> str:
    """Generate a 1-2 sentence editorial take based on trigger + data."""

    if trigger == "cross_platform":
        return _editorial_cross_platform(data)
    elif trigger == "portfolio":
        return _editorial_portfolio(data)
    elif trigger == "x_signals":
        return _editorial_x_signals(data)
    elif trigger == "edge":
        return _editorial_edge(data)
    elif trigger == "morning":
        return _editorial_morning(data)
    elif trigger == "meeting":
        return ""  # Meeting reminders don't need editorializing
    elif trigger == "conflicts":
        return _editorial_conflicts(data)
    return ""


def _editorial_cross_platform(data: dict) -> str:
    """Editorial for cross-platform divergence alerts."""
    spread = data.get("max_spread", 0)
    market = data.get("market_name", "")

    if spread >= 15:
        options = [
            f"Big divergence. One of these markets is wrong about {_short_name(market)}.",
            f"Kalshi and Polymarket can't agree on {_short_name(market)}. Somebody's mispriced.",
            f"15+ point spread — that's not noise. Something's off.",
        ]
    elif spread >= 10:
        options = [
            f"Interesting divergence on {_short_name(market)}. Worth watching.",
            f"Markets disagree here. Not huge, but notable.",
        ]
    else:
        options = [
            f"Mild divergence on {_short_name(market)}.",
        ]

    return random.choice(options)


def _editorial_portfolio(data: dict) -> str:
    """Editorial for portfolio drift alerts."""
    pnl_delta = data.get("pnl_delta", 0)
    biggest_mover = data.get("biggest_mover", "")
    biggest_move_pct = data.get("biggest_move_pct", 0)

    if pnl_delta > 20:
        return random.choice([
            "Good day. Portfolio's running.",
            f"Nice move. {biggest_mover} carrying the load." if biggest_mover else "Portfolio's hot.",
        ])
    elif pnl_delta < -20:
        return random.choice([
            "Rough patch. Check your stops.",
            f"{biggest_mover} is bleeding." if biggest_mover else "Red day. Keep an eye on it.",
        ])
    elif abs(biggest_move_pct) > 10:
        direction = "ripped" if biggest_move_pct > 0 else "tanked"
        return f"{biggest_mover} {direction} {abs(biggest_move_pct):.0f}%. Rest of book is quiet."
    else:
        return ""


def _editorial_x_signals(data: dict) -> str:
    """Editorial for X/Twitter signal alerts."""
    confidence = data.get("confidence", 0)
    topic = data.get("top_topic", "")
    signal_count = data.get("signal_count", 1)

    if confidence > 0.9:
        return random.choice([
            f"Strong signal on {_short_topic(topic)}. This feels real.",
            f"X is buzzing about {_short_topic(topic)}. High confidence.",
        ])
    elif signal_count > 2:
        return f"Multiple signals on {_short_topic(topic)}. Worth your attention."
    else:
        return f"New signal on {_short_topic(topic)}."


def _editorial_edge(data: dict) -> str:
    """Editorial for edge engine alerts."""
    edge = data.get("top_edge", 0)
    ticker = data.get("top_ticker", "")
    confidence = data.get("confidence", 0)

    if edge > 20:
        return random.choice([
            f"Fat edge on {ticker}. Qwen thinks this is significantly mispriced.",
            f"{edge:.0f}% edge — that's a standout. Worth a deep look.",
        ])
    elif edge > 15:
        return random.choice([
            f"Decent edge on {ticker}. Not a slam dunk, but interesting.",
            f"Qwen flagged {ticker}. Moderate edge, decent confidence.",
        ])
    else:
        return f"Mild edge on {ticker}. Keeping it on radar."


def _editorial_morning(data: dict) -> str:
    """Editorial for morning briefing — set the tone for the day."""
    event_count = data.get("event_count", 0)
    has_positions = data.get("has_positions", False)
    day_of_week = datetime.now().strftime("%A")

    if event_count == 0 and not has_positions:
        return random.choice([
            "Clear day ahead. No meetings, no active positions. Rare.",
            f"Empty {day_of_week}. Enjoy it or go find something.",
        ])
    elif event_count == 0:
        return random.choice([
            "No meetings today — just you and the markets.",
            "Calendar's clear. Market day.",
        ])
    elif event_count >= 5:
        return random.choice([
            f"Packed {day_of_week}. {event_count} things on the calendar.",
            f"Busy day — {event_count} events. Pace yourself.",
        ])
    elif day_of_week in ("Saturday", "Sunday"):
        return random.choice([
            f"Weekend check-in.",
            f"Happy {day_of_week}. Quick look at what's moving.",
        ])
    elif day_of_week == "Monday":
        return random.choice([
            "New week. Here's the lay of the land.",
            "Monday reset. Here's what matters today.",
        ])
    elif day_of_week == "Friday":
        return random.choice([
            "Friday rundown. Close out the week strong.",
            "End of week. Last look at what's open.",
        ])
    else:
        return ""


def _editorial_conflicts(data: dict) -> str:
    """Editorial for calendar conflict detection."""
    conflict_count = data.get("conflict_count", 0)
    if conflict_count >= 3:
        return "Tomorrow's a mess. Multiple overlaps."
    elif conflict_count == 2:
        return "Heads up — couple of conflicts tomorrow."
    else:
        return "Scheduling conflict tomorrow."


# ── Helpers ──────────────────────────────────────────────────────────────────

def _short_name(market_name: str) -> str:
    """Shorten a market name for editorial use."""
    if len(market_name) > 40:
        return market_name[:37] + "..."
    return market_name or "this market"


def _short_topic(topic: str) -> str:
    """Shorten a topic string."""
    if len(topic) > 30:
        return topic[:27] + "..."
    return topic or "this"
