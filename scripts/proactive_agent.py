#!/usr/bin/env python3
"""
Rout proactive agent — cron-driven outbound messages.

The feature that makes Rout feel alive. Runs on a 15-minute interval
via launchd and sends contextual messages without being asked.

Triggers:
  - Morning briefing (configurable time): Today's calendar + pending reminders
  - Meeting reminder (30 min before): "You have X in 30 minutes"
  - Reminder nudge: When a reminder's due time hits

Rate limit: Max 3 proactive messages per hour.
Launch: launchd plist, 15-minute interval.

Usage:
    python3 scripts/proactive_agent.py          # Run once (check all triggers)
    python3 scripts/proactive_agent.py --dry-run  # Show what would fire, don't send
"""

import json
import os
import re
import subprocess
import shutil
import sys
import time
import yaml
from datetime import datetime
from pathlib import Path


# ── Setup ───────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OPENCLAW_DIR = Path(
    os.environ.get("ROUT_OPENCLAW_DIR", str(Path.home() / ".openclaw"))
).expanduser()

STATE_DIR = OPENCLAW_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

PROACTIVE_STATE_PATH = STATE_DIR / "proactive_state.json"
PROACTIVE_LOG_PATH = OPENCLAW_DIR / "logs" / "proactive_agent.log"
PROACTIVE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


# ── Config ──────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    for candidate in [
        PROJECT_ROOT / "config.yaml",
        Path.home() / ".config/imsg-watcher/config.yaml",
    ]:
        if candidate.exists():
            with open(candidate) as f:
                return yaml.safe_load(f) or {}
    return {}


def _load_proactive_config() -> dict:
    """Load proactive triggers config."""
    triggers_path = PROJECT_ROOT / "config" / "proactive_triggers.yaml"
    if triggers_path.exists():
        with open(triggers_path) as f:
            return yaml.safe_load(f) or {}
    return _default_proactive_config()


def _default_proactive_config() -> dict:
    return {
        "proactive": {
            "enabled": True,
            "max_messages_per_hour": 3,
            "morning_briefing": {
                "enabled": True,
                "time": "08:00",
                "include": ["calendar", "reminders"],
            },
            "meeting_reminders": {
                "enabled": True,
                "lookahead_minutes": 30,
            },
        }
    }


CONFIG = _load_config()
PROACTIVE_CFG = _load_proactive_config().get("proactive", {})
PATHS = CONFIG.get("paths", {})
IMSG = PATHS.get("imsg", "/opt/homebrew/bin/imsg")
OSASCRIPT = shutil.which("osascript") or "/usr/bin/osascript"
PERSONAL_CHAT_ID = CONFIG.get("chats", {}).get("personal_id", 1)
CHAT_HANDLES = {
    int(k): tuple(v)
    for k, v in CONFIG.get("chat_handles", {}).items()
}

MAX_MESSAGES_PER_HOUR = PROACTIVE_CFG.get("max_messages_per_hour", 3)


# ── State ───────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if PROACTIVE_STATE_PATH.exists():
        try:
            with open(PROACTIVE_STATE_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"sent_timestamps": [], "last_briefing_date": "", "reminded_events": []}


def _save_state(state: dict):
    try:
        with open(PROACTIVE_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass


def _rate_limited(state: dict) -> bool:
    """Check if we've hit the hourly rate limit."""
    now = time.time()
    hour_ago = now - 3600
    recent = [ts for ts in state.get("sent_timestamps", []) if ts > hour_ago]
    state["sent_timestamps"] = recent  # Clean old timestamps
    return len(recent) >= MAX_MESSAGES_PER_HOUR


def _record_send(state: dict):
    state.setdefault("sent_timestamps", []).append(time.time())


# ── Messaging ───────────────────────────────────────────────────────────────

_UNSAFE_CHARS = re.compile(r'[\x00-\x1f]')


def _send_message(text: str, chat_id: int = None, dry_run: bool = False) -> bool:
    """Send a proactive message via iMessage."""
    chat_id = chat_id or PERSONAL_CHAT_ID
    text = text[:1500]  # iMessage truncation

    if dry_run:
        _log(f"[DRY RUN] Would send to chat {chat_id}: {text[:100]}...")
        return True

    # Try osascript first
    handle_info = CHAT_HANDLES.get(chat_id)
    if handle_info:
        handle, handle_type = handle_info
        escaped = _UNSAFE_CHARS.sub("", text).replace("\\", "\\\\").replace('"', '\\"')
        handle = _UNSAFE_CHARS.sub("", handle).replace('"', '\\"')

        if handle_type == "buddy":
            script = f'''tell application "Messages"
    set s to 1st service whose service type = iMessage
    set b to buddy "{handle}" of s
    send "{escaped}" to b
end tell'''
        else:
            script = f'''tell application "Messages"
    set c to (1st chat whose id = "{handle}")
    send "{escaped}" to c
end tell'''

        try:
            result = subprocess.run(
                [OSASCRIPT, "-e", script], timeout=30, capture_output=True
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass

    # Fallback: imsg CLI
    try:
        result = subprocess.run(
            [IMSG, "send", "--chat-id", str(chat_id),
             "--service", "imessage", "--text", text],
            timeout=30, check=False, capture_output=True
        )
        return result.returncode == 0
    except Exception:
        return False


# ── Calendar Reading ────────────────────────────────────────────────────────

def _read_calendar_events(date_offset: int = 0) -> str:
    """Read calendar events for today (offset=0) or tomorrow (offset=1)."""
    try:
        from agent.tools.calendar_tools import read_calendar
        return read_calendar(date_offset_days=date_offset)
    except Exception as e:
        return f"[Calendar error: {e}]"


def _read_reminders() -> str:
    """Read pending reminders."""
    try:
        from agent.tools.reminder_tools import read_reminders
        return read_reminders()
    except Exception as e:
        return f"[Reminders error: {e}]"


def _parse_upcoming_events(events_text: str, lookahead_minutes: int = 30) -> list:
    """Parse event text and return events happening within lookahead_minutes."""
    now = datetime.now()
    upcoming = []

    for line in events_text.splitlines():
        # Try to parse "HH:MM - HH:MM  Title" or "HH:MM AM/PM - Title" patterns
        time_match = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM)?\s*[-–]', line, re.IGNORECASE)
        if not time_match:
            continue

        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        ampm = time_match.group(3)

        if ampm:
            if ampm.upper() == "PM" and hour != 12:
                hour += 12
            elif ampm.upper() == "AM" and hour == 12:
                hour = 0

        event_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        diff = (event_time - now).total_seconds() / 60

        if 0 < diff <= lookahead_minutes:
            # Extract title (everything after the time range)
            title_match = re.search(r'[-–]\s*\d{1,2}:\d{2}\s*(AM|PM)?\s+(.+)', line, re.IGNORECASE)
            if not title_match:
                title_match = re.search(r'[-–]\s+(.+)', line)
            title = title_match.group(1).strip() if title_match else line.strip()

            upcoming.append({
                "title": title,
                "time": f"{hour}:{minute:02d}",
                "minutes_away": int(diff),
            })

    return upcoming


# ── Triggers ────────────────────────────────────────────────────────────────

def check_morning_briefing(state: dict, dry_run: bool = False) -> bool:
    """Send morning briefing if it's the right time and hasn't been sent today."""
    cfg = PROACTIVE_CFG.get("morning_briefing", {})
    if not cfg.get("enabled", True):
        return False

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    # Already sent today?
    if state.get("last_briefing_date") == today:
        return False

    # Is it the right time? (within 15 min window of configured time)
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
    include = cfg.get("include", ["calendar", "reminders"])
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

    message = "\n".join(parts)

    if _send_message(message, dry_run=dry_run):
        state["last_briefing_date"] = today
        _record_send(state)
        _log(f"Morning briefing sent")
        return True

    return False


def check_meeting_reminders(state: dict, dry_run: bool = False) -> bool:
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

        if _send_message(message, dry_run=dry_run):
            reminded.add(event_key)
            _record_send(state)
            _log(f"Meeting reminder sent: {event['title']}")
            sent_any = True

    state["reminded_events"] = list(reminded)

    # Clean old reminded events (keep only today's)
    state["reminded_events"] = [e for e in state["reminded_events"] if e.startswith(now_key)]

    return sent_any


# ── Logging ─────────────────────────────────────────────────────────────────

def _log(msg: str):
    timestamp = datetime.now().isoformat()
    line = f"[{timestamp}] {msg}"
    print(line)
    try:
        with open(PROACTIVE_LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Main ────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False):
    """Run all proactive triggers."""
    if not PROACTIVE_CFG.get("enabled", True):
        _log("Proactive agent disabled in config.")
        return

    state = _load_state()

    if _rate_limited(state):
        _log(f"Rate limited ({MAX_MESSAGES_PER_HOUR}/hr). Skipping.")
        _save_state(state)
        return

    _log("Checking triggers...")

    # Run triggers in priority order
    check_morning_briefing(state, dry_run=dry_run)

    if not _rate_limited(state):
        check_meeting_reminders(state, dry_run=dry_run)

    _save_state(state)
    _log("Done.")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    run(dry_run=dry)
