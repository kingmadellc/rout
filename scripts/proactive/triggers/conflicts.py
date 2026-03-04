"""Calendar conflicts trigger."""

from datetime import datetime
from proactive.base import (
    PROACTIVE_CFG,
    _log,
    _read_calendar_events,
    _detect_calendar_conflicts,
    _send_message,
    _record_send,
)

# Personality-aware send
try:
    from proactive.personality.engine import personality_send as _personality_send
    _HAS_PERSONALITY = True
except ImportError:
    _HAS_PERSONALITY = False


def check_calendar_conflicts(state: dict, dry_run: bool = False, force: bool = False) -> bool:
    """Check tomorrow's calendar for overlapping events."""
    cfg = PROACTIVE_CFG.get("calendar_conflicts", {})
    if not cfg.get("enabled", True):
        return False

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    # Only check once per day (evening check for tomorrow)
    if state.get("last_conflict_check_date") == today:
        return False
    if now.hour < 18:  # Only check after 6 PM
        return False

    state["last_conflict_check_date"] = today

    # Check tomorrow's events
    events_text = _read_calendar_events(1)
    if not events_text or "no events" in events_text.lower():
        return False

    conflicts = _detect_calendar_conflicts(events_text)
    if not conflicts:
        return False

    parts = ["⚠️ Tomorrow has scheduling conflicts:"]
    for a, b in conflicts[:3]:
        parts.append(f"  • {a} overlaps with {b}")

    message = "\n".join(parts)

    data = {
        "conflict_count": len(conflicts),
        "conflicts": [(a, b) for a, b in conflicts[:3]],
    }

    if _HAS_PERSONALITY and not force:
        sent = _personality_send("conflicts", message, data, state, dry_run=dry_run)
        if sent:
            _log(f"Calendar conflict alert (personality): {len(conflicts)} conflicts tomorrow")
        return sent

    if _send_message(message, dry_run=dry_run):
        _record_send(state)
        _log(f"Calendar conflict alert: {len(conflicts)} conflicts tomorrow")
        return True
    return False
