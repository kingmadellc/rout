"""Morning briefing trigger."""

from datetime import datetime
from proactive.base import (
    PROACTIVE_CFG,
    _log,
    _read_calendar_events,
    _read_reminders,
    _read_portfolio_summary,
    _send_message,
    _is_duplicate_message,
    _record_send,
)

# Personality-aware send
try:
    from proactive.personality.engine import personality_send as _personality_send
    _HAS_PERSONALITY = True
except ImportError:
    _HAS_PERSONALITY = False


def check_morning_briefing(state: dict, dry_run: bool = False, force: bool = False) -> bool:
    """Send morning briefing if it's the right time and hasn't been sent today."""
    cfg = PROACTIVE_CFG.get("morning_briefing", {})
    if not cfg.get("enabled", True):
        return False

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    # Already sent today? (skip check when forced — e.g. demo recording)
    if not force and state.get("last_briefing_date") == today:
        return False

    # Is it the right time? (within 15 min window of configured time)
    # force=True bypasses this check (used by --only flag and demo script)
    if not force:
        briefing_time = cfg.get("time", "08:00")
        try:
            bt_hour, bt_min = map(int, briefing_time.split(":"))
        except (ValueError, AttributeError):
            bt_hour, bt_min = 8, 0

        target = now.replace(hour=bt_hour, minute=bt_min, second=0)
        diff_minutes = (now - target).total_seconds() / 60

        if not (0 <= diff_minutes <= 15):
            return False

    # Build briefing
    include = cfg.get("include", ["calendar", "reminders", "portfolio"])
    parts = [f"Good morning! Here's your day ({now.strftime('%A, %B %d')}):"]

    if "calendar" in include:
        events = _read_calendar_events(0)
        if events and "no events" not in events.lower():
            parts.append(f"\n📅 Calendar:\n{events}")
        else:
            parts.append("\n📅 No events today.")

    if "reminders" in include:
        reminders = _read_reminders()
        if reminders and "no reminders" not in reminders.lower() and "[" not in reminders[:5]:
            parts.append(f"\n📝 Reminders:\n{reminders}")

    if "portfolio" in include:
        portfolio_summary = _read_portfolio_summary()
        if portfolio_summary:
            parts.append(f"\n💰 Portfolio:\n{portfolio_summary}")

    message = "\n".join(parts)

    # Route through personality pipeline
    data = {
        "event_count": len(events) if "calendar" in include and events else 0,
        "has_reminders": "reminders" in include and reminders and "no reminders" not in reminders.lower(),
        "has_portfolio": "portfolio" in include and portfolio_summary is not None,
        "day_of_week": now.strftime("%A"),
    }

    if _HAS_PERSONALITY and not force:
        sent = _personality_send("morning", message, data, state, dry_run=dry_run)
        if sent:
            state["last_briefing_date"] = today
            _log("Morning briefing sent (personality)")
        return sent

    # Fallback: direct send (force mode or no personality)
    if _is_duplicate_message(state, message):
        _log("Morning briefing skipped — duplicate detected within dedup window")
        state["last_briefing_date"] = today
        return False

    if _send_message(message, dry_run=dry_run):
        state["last_briefing_date"] = today
        _record_send(state, message)
        _log("Morning briefing sent")
        return True

    return False
