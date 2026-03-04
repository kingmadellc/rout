"""Micro-initiations — ambient awareness pings.

Messages with zero utility that make Rout feel alive.
"Quiet week in the markets. Enjoy it."
"Markets closed Monday for the holiday — I'll keep watching crypto."

These are NOT triggered by data. They're triggered by:
- Time (weekly cadence)
- Calendar awareness (holidays, weekends)
- Absence of activity (quiet stretch)

Rules:
- Max 2 per week
- Never on a day with 3+ real alerts
- Varied phrasing, never repeat within 2 weeks
- Short. 1-2 sentences max.
"""

import hashlib
import random
import time
from datetime import datetime, timedelta
from typing import Optional


# ── Micro-initiation pools ───────────────────────────────────────────────────

QUIET_MARKET = [
    "Quiet day across the board. Sometimes that's the best kind.",
    "Nothing moving. Markets are asleep. Enjoy the peace.",
    "Flat everywhere. No edge, no alerts, no noise. Rare.",
    "All quiet. I'm watching, but there's nothing to flag.",
    "Dead calm in the markets today.",
]

WEEKEND = [
    "Weekend mode. Crypto never sleeps, but everything else does.",
    "Markets are closed. I'll keep an eye on crypto and X signals.",
    "Taking it easy today. I'll ping you if anything breaks.",
]

MONDAY = [
    "New week. Let's see what the market opens with.",
    "Monday. Fresh start. I'll have the lay of the land soon.",
]

FRIDAY = [
    "End of week. Consider closing any positions you don't want to hold over the weekend.",
    "Friday close approaching. Good time to review what's open.",
]

HOLIDAY_AWARENESS = [
    "Markets closed today for the holiday. Crypto's still live.",
    "Holiday schedule — lighter data today. I'll flag anything unusual.",
]

STREAK_GOOD = [
    "Three green days in a row. Momentum's real. Don't get greedy.",
    "Winning streak. Nice. Stay disciplined.",
]

STREAK_BAD = [
    "Rough stretch. Take a step back, review your thesis on each position.",
    "Red days happen. If your thesis hasn't changed, the position shouldn't either.",
]

ABSENCE = [
    "Been a while since you've checked in. Everything's stable — no fires.",
    "Haven't heard from you today. Markets are quiet, nothing urgent.",
]

# US market holidays (month, day) — approximate, some shift by year
US_HOLIDAYS = [
    (1, 1), (1, 20), (2, 17), (4, 18), (5, 26), (6, 19),
    (7, 4), (9, 1), (11, 27), (12, 25),
]


def get_micro_initiation(state: dict) -> Optional[str]:
    """Get a micro-initiation message if conditions are right.

    Returns a message string, or None if we shouldn't send one.
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    # ── Rate limit: max 2 per week ────────────────────────────────
    week_key = now.strftime("%Y-W%W")
    micro_count = state.get("micro_initiations_week", {}).get(week_key, 0)
    if micro_count >= 2:
        return None

    # ── Don't send on busy alert days ─────────────────────────────
    alerts_today = state.get("personality_messages_today", 0)
    if alerts_today >= 3:
        return None

    # ── Don't repeat within 3 days ────────────────────────────────
    last_micro_ts = state.get("last_micro_initiation_ts", 0)
    if time.time() - last_micro_ts < 3 * 24 * 3600:
        return None

    # ── Don't send too early or too late ──────────────────────────
    hour = now.hour
    if hour < 9 or hour > 20:
        return None

    # ── Pick the right pool ───────────────────────────────────────
    day = now.weekday()
    month_day = (now.month, now.day)

    pool = None

    # Holiday check
    if month_day in US_HOLIDAYS:
        pool = HOLIDAY_AWARENESS

    # Weekend
    elif day >= 5:
        pool = WEEKEND

    # Monday morning (10am-12pm window)
    elif day == 0 and 10 <= hour <= 12:
        pool = MONDAY

    # Friday afternoon (2pm-5pm window)
    elif day == 4 and 14 <= hour <= 17:
        pool = FRIDAY

    # Absence detection (no user messages in 24+ hours)
    elif _user_absent(state):
        pool = ABSENCE

    # Random quiet day (low probability — ~15% chance when checked)
    elif random.random() < 0.15 and alerts_today == 0:
        pool = QUIET_MARKET

    if not pool:
        return None

    # ── Dedup: don't repeat a message within 2 weeks ──────────────
    recent_hashes = state.get("micro_message_hashes", [])
    candidates = [msg for msg in pool if _hash(msg) not in recent_hashes]
    if not candidates:
        # All used recently — skip this cycle
        return None

    message = random.choice(candidates)
    return message


def record_micro_initiation(state: dict, message: str):
    """Record that a micro-initiation was sent."""
    now = datetime.now()
    week_key = now.strftime("%Y-W%W")

    # Update weekly count
    if "micro_initiations_week" not in state:
        state["micro_initiations_week"] = {}
    state["micro_initiations_week"][week_key] = (
        state["micro_initiations_week"].get(week_key, 0) + 1
    )

    # Clean old week entries (keep last 4 weeks)
    weeks = sorted(state["micro_initiations_week"].keys())
    if len(weeks) > 4:
        for old_week in weeks[:-4]:
            del state["micro_initiations_week"][old_week]

    # Record timestamp
    state["last_micro_initiation_ts"] = time.time()

    # Record hash for dedup
    h = _hash(message)
    hashes = state.get("micro_message_hashes", [])
    hashes.append(h)
    # Keep last 20 hashes (~2 weeks of potential messages)
    state["micro_message_hashes"] = hashes[-20:]


def _user_absent(state: dict) -> bool:
    """Check if the user hasn't sent a message in 24+ hours."""
    last_user_msg_ts = state.get("last_user_message_ts", 0)
    if last_user_msg_ts == 0:
        return False  # No tracking data yet
    return time.time() - last_user_msg_ts > 24 * 3600


def _hash(msg: str) -> str:
    """Short hash of a message for dedup."""
    return hashlib.sha256(msg.encode()).hexdigest()[:12]
