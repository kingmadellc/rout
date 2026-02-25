"""
Calendar tools for Rout agent.

Read and write Apple Calendar events via osascript.
"""

import subprocess
from datetime import date


def read_calendar(date_offset_days: int = 0) -> str:
    """Fetch Calendar.app events for a given day (0=today, 1=tomorrow, etc.)."""
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


def create_event(title: str, date_str: str, start_hour: int, start_min: int,
                 duration_hours: float = 1.0, calendar_name: str = "Home") -> str:
    """Create a Calendar.app event via osascript."""
    # Sanitize inputs for AppleScript
    title = title.replace('"', '\\"').replace('\\', '\\\\')
    calendar_name = calendar_name.replace('"', '\\"')

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
