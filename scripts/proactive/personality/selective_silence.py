"""Selective silence — knowing when NOT to talk.

The most lifelike thing Rout can do is choose silence.

"Skipped the brief — nothing worth your attention this morning."

This module decides when Rout should stay quiet AND when to
send a deliberate "I'm being quiet on purpose" message.

A silence message is more agentic than 10 data alerts.
It implies judgment: "I looked at everything and decided not to bother you."
"""

import random
import time
from datetime import datetime
from typing import Optional


def should_stay_silent(trigger: str, data: dict, state: dict) -> bool:
    """Determine if Rout should suppress a message entirely.

    Different from variable_timing (which decides urgency-based send/defer).
    This is about content quality: is there actually anything worth saying?

    Returns True if the trigger's data is too boring/thin to justify a message.
    """
    if trigger == "morning":
        return _morning_is_boring(data)
    elif trigger == "cross_platform":
        return _divergence_is_stale(data, state)
    elif trigger == "x_signals":
        return _signals_are_noise(data, state)
    elif trigger == "edge":
        return _edge_is_weak(data)
    elif trigger == "portfolio":
        return _portfolio_is_flat(data)
    elif trigger == "conflicts":
        return False  # Conflicts are always worth mentioning
    elif trigger == "meeting":
        return False  # Meeting reminders are always worth it
    return False


def silence_message(trigger: str, data: dict) -> Optional[str]:
    """Generate a deliberate silence notification.

    "I looked and there's nothing worth your time."

    Returns a message string, or None if we should be truly silent
    (no message at all).

    Rules:
    - Only send silence messages for triggers the user EXPECTS
      (morning brief, edge scans). Don't send "nothing happened"
      for background monitors.
    - Max 1 silence message per day to avoid meta-spam.
    - Vary the phrasing.
    """
    if trigger == "morning":
        return random.choice([
            "Skipped the brief — nothing worth your attention this morning.",
            "Nothing moving this morning. Calendar's clear, markets are flat.",
            "Quiet morning. I'll ping you if something changes.",
        ])
    elif trigger == "edge":
        return random.choice([
            "Ran the edge scan. Nothing actionable right now.",
            "Markets are efficiently priced today. No edge worth flagging.",
            "Edge scan came up empty. Boring day for mispricing.",
        ])
    # For other triggers, true silence (no message)
    return None


def should_send_silence_message(trigger: str, state: dict) -> bool:
    """Decide whether to send a deliberate silence message vs. true silence.

    We don't want to send "nothing happened" every single cycle.
    Rules:
    - Only for expected triggers (morning, edge)
    - Max 1 silence message per day
    - Only if we haven't sent a real message for this trigger today
    """
    if trigger not in ("morning", "edge"):
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    silence_key = f"last_silence_date_{trigger}"
    global_silence_key = "last_silence_message_date"

    # Already sent a silence message today (for any trigger)?
    if state.get(global_silence_key) == today:
        return False

    # Already sent a silence message for THIS trigger today?
    if state.get(silence_key) == today:
        return False

    # Already sent a real message for this trigger today?
    trigger_sent_key = f"last_{trigger}_sent_date"
    if state.get(trigger_sent_key) == today:
        return False

    return True


def record_silence(trigger: str, state: dict):
    """Record that a silence message was sent."""
    today = datetime.now().strftime("%Y-%m-%d")
    state[f"last_silence_date_{trigger}"] = today
    state["last_silence_message_date"] = today


# ── Content Quality Checks ───────────────────────────────────────────────────

def _morning_is_boring(data: dict) -> bool:
    """Is the morning brief data boring enough to skip?"""
    event_count = data.get("event_count", 0)
    has_positions = data.get("has_positions", False)
    has_reminders = data.get("has_reminders", False)
    portfolio_change = abs(data.get("portfolio_change_pct", 0))

    # Nothing on calendar, no position moves, no reminders
    if event_count == 0 and not has_reminders and portfolio_change < 2:
        return True

    return False


def _divergence_is_stale(data: dict, state: dict) -> bool:
    """Is this the same divergence we already flagged?"""
    current_key = data.get("market_key", "")
    last_flagged = state.get("last_divergence_market", "")
    last_spread = state.get("last_divergence_spread", 0)
    current_spread = data.get("max_spread", 0)

    # Same market, spread hasn't changed much
    if current_key == last_flagged and abs(current_spread - last_spread) < 3:
        return True

    return False


def _signals_are_noise(data: dict, state: dict) -> bool:
    """Are the X signals just noise / repeats?"""
    topics = data.get("topics", [])
    last_topics = state.get("last_x_signal_topics", [])

    # Exact same topics as last time
    if set(topics) == set(last_topics) and len(topics) > 0:
        return True

    # Low confidence across the board
    max_confidence = data.get("confidence", 0)
    if max_confidence < 0.7:
        return True

    return False


def _edge_is_weak(data: dict) -> bool:
    """Is the edge too thin to bother?"""
    top_edge = data.get("top_edge", 0)
    confidence = data.get("confidence", 0)

    # Edge exists but confidence is low
    if top_edge > 0 and confidence < 0.5:
        return True

    # Edge is barely above threshold
    if top_edge < 10:
        return True

    return False


def _portfolio_is_flat(data: dict) -> bool:
    """Is the portfolio essentially unchanged?"""
    pnl_delta = abs(data.get("pnl_delta", 0))
    biggest_move = abs(data.get("biggest_move_pct", 0))

    if pnl_delta < 5 and biggest_move < 3:
        return True

    return False
