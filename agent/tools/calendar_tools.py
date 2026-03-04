"""
Calendar tools for Rout agent.

Read and write Apple Calendar events via osascript.
"""

import subprocess
import time
from datetime import date
from dateutil import parser as dateparser


def _ensure_calendar_running():
    """Ensure Calendar.app is running before any osascript call.

    On headless Mac Mini, Calendar.app won't be running after reboot.
    AppleScript returns -600 ("Application isn't running") without this.
    Uses 'open -gj' to launch hidden and in background.
    """
    # Check if already running (fast path — no launch delay)
    check = subprocess.run(
        ['pgrep', '-x', 'Calendar'],
        capture_output=True, timeout=5
    )
    if check.returncode == 0:
        return  # Already running

    # Launch hidden, in background
    subprocess.run(
        ['open', '-gja', 'Calendar'],
        capture_output=True, timeout=10
    )
    time.sleep(2)  # Give it time to initialize event store


def read_calendar(date_offset_days: int = 0) -> str:
    """Fetch Calendar.app events for a given day (0=today, 1=tomorrow, etc.)."""
    _ensure_calendar_running()
    skip = '{"US Holidays", "Siri Suggestions", "Birthdays"}'
    script = f'''
tell application "Calendar"
  set targetDate to (current date) + ({date_offset_days} * 86400)
  set dayStart to targetDate
  set hours of dayStart to 0
  set minutes of dayStart to 0
  set seconds of dayStart to 0
  set dayEnd to dayStart + 86400
  set results to {{}}
  repeat with cal in calendars
    if name of cal is not in {skip} then
      repeat with e in (every event of cal whose start date >= dayStart and start date < dayEnd)
        set results to results & {{(summary of e) & " @ " & ((start date of e) as string) & " [" & (name of cal) & "]"}}
      end repeat
    end if
  end repeat
  if (count of results) = 0 then return "No events"
  set AppleScript's text item delimiters to linefeed
  return results as string
end tell
'''
    try:
        result = subprocess.run(['osascript', '-e', script],
                                capture_output=True, text=True, timeout=15)
        return result.stdout.strip() or result.stderr.strip() or "No events"
    except Exception as e:
        return f"[Calendar error: {e}]"


def read_calendar_range(days: int = 7) -> str:
    """Read calendar events for the next N days."""
    events = []
    for i in range(days):
        day_events = read_calendar(date_offset_days=i)
        if day_events and day_events != "No events":
            events.append(f"Day {i} ({'+' + str(i) + 'd' if i > 0 else 'today'}):\n{day_events}")
    return "\n".join(events) if events else "No events found in the next {days} days."


def _validate_date(date_str: str) -> str:
    """Validate and normalize a date string for AppleScript.
    Returns a consistently formatted date string or raises ValueError."""
    try:
        parsed = dateparser.parse(date_str)
        # AppleScript expects "Month Day, Year" format
        return parsed.strftime("%B %d, %Y")
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid date format '{date_str}': {e}")


def create_event(title: str, date_str: str, start_hour: int, start_min: int,
                 duration_hours: float = 1.0, calendar_name: str = "Home") -> str:
    """Create a Calendar.app event via osascript."""
    # Validate date before passing to AppleScript
    try:
        date_str = _validate_date(date_str)
    except ValueError as e:
        return f"[Calendar error: {e}]"

    # Validate time range
    if not (0 <= start_hour <= 23 and 0 <= start_min <= 59):
        return f"[Calendar error: Invalid time {start_hour}:{start_min:02d}]"

    # Sanitize inputs for AppleScript
    title = title.replace('"', '\\"').replace('\\', '\\\\')
    calendar_name = calendar_name.replace('"', '\\"')

    _ensure_calendar_running()
    duration_seconds = int(duration_hours * 3600)
    script = f'''
tell application "Calendar"
  set targetCal to first calendar whose name is "{calendar_name}"
  set eventDate to date "{date_str}"
  set hours of eventDate to {start_hour}
  set minutes of eventDate to {start_min}
  set seconds of eventDate to 0
  set endDate to eventDate + {duration_seconds}
  tell targetCal
    make new event with properties {{summary:"{title}", start date:eventDate, end date:endDate}}
  end tell
  return "Event created: {title}"
end tell
'''
    try:
        result = subprocess.run(['osascript', '-e', script],
                                capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return result.stdout.strip()
        return f"[Calendar write error: {result.stderr.strip()[:200]}]"
    except Exception as e:
        return f"[Calendar error: {e}]"


def today_str() -> str:
    """Return today's date formatted for extraction prompts."""
    return date.today().strftime("%A, %B %d, %Y")
