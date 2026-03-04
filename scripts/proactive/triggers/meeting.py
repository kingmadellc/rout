"""Meeting reminder trigger."""

from datetime import datetime
from proactive.base import (
    PROACTIVE_CFG,
    _log,
    _read_calendar_events,
    _parse_upcoming_events,
    _send_message,
    _record_send,
)

# Personality-aware send
try:
    from proactive.personality.engine import personality_send as _personality_send
    _HAS_PERSONALITY = True
except ImportError:
    _HAS_PERSONALITY = False


def check_meeting_reminders(state: dict, dry_run: bool = False, force: bool = False) -> bool:
    """Send reminders for meetings happening soon."""
    cfg = PROACTIVE_CFG.get("meeting_reminders", {})
    if not cfg.get("enabled", True):
        return False

    lookahead = cfg.get("lookahead_minutes", 30)
    events_text = _read_calendar_events(0)
    upcoming = _parse_upcoming_events(events_text, lookahead_minutes=lookahead)

    if not upcoming:
        return False

    # Filter out already-reminded events
    reminded = set(state.get("reminded_events", []))
    now_key = datetime.now().strftime("%Y-%m-%d")

    sent_any = False
    for event in upcoming:
        event_key = f"{now_key}:{event['title']}:{event['time']}"
        if event_key in reminded:
            continue

        mins = event["minutes_away"]
        message = f"⏰ {event['title']} in {mins} minutes ({event['time']})"

        data = {
            "event_title": event["title"],
            "minutes_away": mins,
            "event_time": event["time"],
        }

        if _HAS_PERSONALITY and not force:
            if _personality_send("meeting", message, data, state, dry_run=dry_run):
                reminded.add(event_key)
                _log(f"Meeting reminder sent (personality): {event['title']}")
                sent_any = True
        elif _send_message(message, dry_run=dry_run):
            reminded.add(event_key)
            _record_send(state)
            _log(f"Meeting reminder sent: {event['title']}")
            sent_any = True

    state["reminded_events"] = list(reminded)

    # Clean old reminded events (keep only today's)
    state["reminded_events"] = [e for e in state["reminded_events"] if e.startswith(now_key)]

    return sent_any
