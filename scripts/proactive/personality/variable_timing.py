"""Variable timing — rhythm, not metronome.

Cron fires every 15 minutes. But Rout shouldn't SEND every 15 minutes.
This module decides: given the urgency of what I have to say and the
context of the day, should I send NOW or hold?

High urgency (big move, fat edge): send immediately.
Low urgency (mild divergence): defer, batch, or skip.
Late night: hold everything unless it's truly urgent.
Weekend: relax the cadence.

The 15-minute cron becomes a "check" interval, not a "send" interval.
Most checks should result in silence.
"""

import random
import time
from datetime import datetime


# ── Urgency Scoring ──────────────────────────────────────────────────────────

def compute_urgency(trigger: str, data: dict) -> float:
    """Score urgency from 0.0 (totally ignorable) to 1.0 (send immediately).

    This replaces the binary "threshold met → send" logic with a gradient.
    """
    if trigger == "cross_platform":
        spread = data.get("max_spread", 0)
        if spread >= 20:
            return 0.95  # Massive divergence — send now
        elif spread >= 15:
            return 0.7
        elif spread >= 10:
            return 0.4
        else:
            return 0.2

    elif trigger == "portfolio":
        pnl_delta = data.get("pnl_delta", 0)
        biggest_move = abs(data.get("biggest_move_pct", 0))
        if abs(pnl_delta) > 50 or biggest_move > 20:
            return 0.95  # Big P&L swing
        elif abs(pnl_delta) > 20 or biggest_move > 10:
            return 0.6
        elif abs(pnl_delta) > 10 or biggest_move > 5:
            return 0.3
        else:
            return 0.1

    elif trigger == "x_signals":
        confidence = data.get("confidence", 0)
        position_match = data.get("matches_position", False)
        base = confidence * 0.6  # High confidence = more urgent
        if position_match:
            base += 0.3  # Directly relevant to open position
        return min(base, 1.0)

    elif trigger == "edge":
        edge = data.get("top_edge", 0)
        if edge > 20:
            return 0.85
        elif edge > 15:
            return 0.5
        elif edge > 12:
            return 0.3
        else:
            return 0.15

    elif trigger == "meeting":
        minutes_away = data.get("minutes_away", 30)
        if minutes_away <= 5:
            return 1.0  # About to start
        elif minutes_away <= 15:
            return 0.9
        elif minutes_away <= 30:
            return 0.7
        return 0.4

    elif trigger == "conflicts":
        return 0.5  # Always moderate — it's preventive

    elif trigger == "morning":
        return 0.8  # Morning brief is expected, high send rate

    return 0.3  # Unknown triggers: moderate


def should_send_now(urgency: float, state: dict) -> bool:
    """Given urgency score and current state, decide whether to send.

    Factors:
    - Time of day (late night = higher threshold)
    - Day of week (weekend = more relaxed)
    - How many messages already sent today
    - Time since last message (don't cluster)
    - Random jitter (prevents robotic patterns)

    Returns True if the message should be sent now.
    """
    now = datetime.now()
    hour = now.hour
    day = now.weekday()  # 0=Mon, 6=Sun
    is_weekend = day >= 5

    # ── Time-of-day threshold modifier ────────────────────────────
    # Higher threshold = harder to send
    if hour < 7:
        # Before 7am: only truly urgent things
        time_threshold = 0.9
    elif hour < 9:
        # Early morning: slightly elevated (don't spam before fully awake)
        time_threshold = 0.5
    elif 9 <= hour <= 22:
        # Active hours: normal threshold
        time_threshold = 0.35
    elif hour <= 23:
        # Late evening: moderate threshold
        time_threshold = 0.6
    else:
        # After 11pm: high threshold
        time_threshold = 0.85

    # ── Weekend modifier ──────────────────────────────────────────
    if is_weekend:
        time_threshold += 0.1  # Slightly harder to trigger on weekends

    # ── Message clustering prevention ─────────────────────────────
    last_send_ts = state.get("last_personality_send_ts", 0)
    minutes_since_last = (time.time() - last_send_ts) / 60

    if minutes_since_last < 10:
        # Sent something less than 10 minutes ago — need high urgency
        time_threshold = max(time_threshold, 0.8)
    elif minutes_since_last < 30:
        # Within 30 min — moderate bump
        time_threshold = max(time_threshold, 0.55)

    # ── Daily message fatigue ─────────────────────────────────────
    messages_today = state.get("personality_messages_today", 0)
    if messages_today >= 10:
        time_threshold += 0.2  # Talked a lot today — be quieter
    elif messages_today >= 6:
        time_threshold += 0.1

    # ── Random jitter (±5%) ───────────────────────────────────────
    # Prevents exact-same-time sends across days
    jitter = random.uniform(-0.05, 0.05)
    time_threshold += jitter

    # Clamp
    time_threshold = max(0.05, min(0.98, time_threshold))

    return urgency >= time_threshold


def record_send(state: dict):
    """Record that a personality-aware message was sent."""
    state["last_personality_send_ts"] = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("personality_date") != today:
        state["personality_messages_today"] = 0
        state["personality_date"] = today
    state["personality_messages_today"] = state.get("personality_messages_today", 0) + 1
