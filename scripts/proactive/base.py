"""Proactive agent shared infrastructure: config, state, locking, messaging, etc."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import yaml
from datetime import datetime
from pathlib import Path


# ── Setup ───────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OPENCLAW_DIR = Path(
    os.environ.get("ROUT_OPENCLAW_DIR", str(Path.home() / ".openclaw"))
).expanduser()

STATE_DIR = OPENCLAW_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

PROACTIVE_STATE_PATH = STATE_DIR / "proactive_state.json"
PORTFOLIO_SNAPSHOT_PATH = STATE_DIR / "portfolio_snapshot.json"
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
                "include": ["calendar", "reminders", "portfolio"],
            },
            "meeting_reminders": {
                "enabled": True,
                "lookahead_minutes": 30,
            },
            "portfolio_drift": {
                "enabled": True,
                "threshold_pct": 5.0,
                "check_interval_minutes": 60,
            },
            "calendar_conflicts": {
                "enabled": True,
                "lookahead_days": 1,
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
    """Atomically save proactive state (write to temp, then rename)."""
    try:
        tmp_path = PROACTIVE_STATE_PATH.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(str(tmp_path), str(PROACTIVE_STATE_PATH))
    except OSError:
        pass


def _rate_limited(state: dict) -> bool:
    """Check if we've hit the hourly rate limit."""
    now = time.time()
    hour_ago = now - 3600
    recent = [ts for ts in state.get("sent_timestamps", []) if ts > hour_ago]
    state["sent_timestamps"] = recent  # Clean old timestamps
    return len(recent) >= MAX_MESSAGES_PER_HOUR


def _record_send(state: dict, message: str = ""):
    state.setdefault("sent_timestamps", []).append(time.time())
    if message:
        _record_message_hash(state, message)


def _is_duplicate_message(state: dict, message: str, window_seconds: int = 300) -> bool:
    """Check if an identical message was sent recently (default: 5 min window).

    Prevents duplicate sends when multiple systems (launchd, cron agents,
    manual runs) trigger the same proactive message.
    """
    msg_hash = hashlib.sha256(message.encode()).hexdigest()[:16]
    now = time.time()
    recent_hashes = state.get("recent_message_hashes", [])
    # Clean expired entries
    recent_hashes = [
        (h, ts) for h, ts in recent_hashes if now - ts < window_seconds
    ]
    state["recent_message_hashes"] = recent_hashes
    return any(h == msg_hash for h, _ in recent_hashes)


def _record_message_hash(state: dict, message: str):
    """Record a message hash for dedup."""
    msg_hash = hashlib.sha256(message.encode()).hexdigest()[:16]
    state.setdefault("recent_message_hashes", []).append((msg_hash, time.time()))


# ── Process Lock ───────────────────────────────────────────────────────────

LOCK_PATH = STATE_DIR / "proactive_agent.lock"


def _acquire_lock() -> bool:
    """Acquire a PID-based lockfile. Returns True if lock acquired.

    Prevents concurrent runs from overlapping — e.g. when a launchd plist
    and an AI agent both invoke this script within the same minute.
    Stale locks (from dead processes) are automatically cleaned up.
    """
    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text().strip())
            # Check if process is still alive
            os.kill(old_pid, 0)
            return False  # Process is still running
        except (ValueError, OSError):
            # Process is dead or PID is invalid — stale lock
            LOCK_PATH.unlink(missing_ok=True)

    try:
        LOCK_PATH.write_text(str(os.getpid()))
        return True
    except OSError:
        return False


def _release_lock():
    """Release the PID lockfile."""
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


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


# ── Portfolio + Conflict Reading ────────────────────────────────────────────

def _read_portfolio_summary() -> str:
    """Get a one-line portfolio summary for morning brief."""
    try:
        from handlers.kalshi_handlers import portfolio_command as _kalshi_portfolio
        raw = _kalshi_portfolio()
        if raw and "[" not in raw[:5]:
            # Truncate to key info (first 300 chars)
            lines = raw.strip().splitlines()
            summary_lines = [l for l in lines[:6] if l.strip()]
            return "\n".join(summary_lines)
        return ""
    except Exception:
        return ""


def _load_portfolio_snapshot() -> dict:
    """Load last known portfolio snapshot from disk."""
    if PORTFOLIO_SNAPSHOT_PATH.exists():
        try:
            with open(PORTFOLIO_SNAPSHOT_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_portfolio_snapshot(snapshot: dict):
    """Atomically save portfolio snapshot to disk."""
    try:
        tmp_path = PORTFOLIO_SNAPSHOT_PATH.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(snapshot, f, indent=2)
        os.replace(str(tmp_path), str(PORTFOLIO_SNAPSHOT_PATH))
    except OSError:
        pass


def _get_current_portfolio() -> dict:
    """Get current portfolio state as structured data."""
    try:
        from handlers.kalshi_handlers import portfolio_command as _kalshi_portfolio
        raw = _kalshi_portfolio()
        if not raw or "[" in raw[:5]:
            return {}
        # Parse the portfolio output for P&L and balance
        result = {"raw": raw, "timestamp": time.time(), "positions": {}}
        for line in raw.splitlines():
            line_lower = line.lower()
            if "balance" in line_lower or "cash" in line_lower:
                # Extract dollar amounts
                import re as _re
                amounts = _re.findall(r'\$[\d,.]+', line)
                if amounts:
                    result["balance"] = amounts[0]
            if "p&l" in line_lower or "pnl" in line_lower or "profit" in line_lower:
                import re as _re
                amounts = _re.findall(r'[+-]?\$[\d,.]+', line)
                if amounts:
                    result["total_pnl"] = amounts[0]
            # Track individual positions by ticker
            import re as _re
            ticker_match = _re.match(r'^\s*([A-Z0-9-]+)\s', line)
            if ticker_match:
                ticker = ticker_match.group(1)
                pct_match = _re.search(r'([+-]?\d+\.?\d*)%', line)
                if pct_match:
                    result["positions"][ticker] = float(pct_match.group(1))
        return result
    except Exception:
        return {}


def _detect_calendar_conflicts(events_text: str) -> list:
    """Detect overlapping calendar events."""
    events = []
    for line in events_text.splitlines():
        # Parse "HH:MM - HH:MM Title" patterns
        time_match = re.search(
            r'(\d{1,2}):(\d{2})\s*(AM|PM)?\s*[-–]\s*(\d{1,2}):(\d{2})\s*(AM|PM)?',
            line, re.IGNORECASE
        )
        if not time_match:
            continue
        sh, sm = int(time_match.group(1)), int(time_match.group(2))
        s_ampm = time_match.group(3)
        eh, em = int(time_match.group(4)), int(time_match.group(5))
        e_ampm = time_match.group(6)

        for h, ampm in [(sh, s_ampm), (eh, e_ampm)]:
            pass  # just parsing

        if s_ampm:
            if s_ampm.upper() == "PM" and sh != 12:
                sh += 12
            elif s_ampm.upper() == "AM" and sh == 12:
                sh = 0
        if e_ampm:
            if e_ampm.upper() == "PM" and eh != 12:
                eh += 12
            elif e_ampm.upper() == "AM" and eh == 12:
                eh = 0

        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        title = line.strip()
        events.append({"title": title, "start": start_min, "end": end_min})

    # Find overlaps
    conflicts = []
    for i in range(len(events)):
        for j in range(i + 1, len(events)):
            a, b = events[i], events[j]
            if a["start"] < b["end"] and b["start"] < a["end"]:
                conflicts.append((a["title"][:40], b["title"][:40]))
    return conflicts


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
