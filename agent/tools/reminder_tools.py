"""
Reminder tools for Rout agent.

Create Apple Reminders tasks and timed reminder messages via `at`.
"""

import os
import subprocess
import tempfile


def create_reminder(title: str, notes: str = "", list_name: str = "Reminders",
                    deadline: str = "") -> str:
    """Add a task to Apple Reminders via osascript."""
    # Sanitize
    title = title.replace('"', '\\"')
    notes = notes.replace('"', '\\"')
    list_name = list_name.replace('"', '\\"')
    deadline = deadline.replace('"', '\\"')

    notes_line = f'set body of newItem to "{notes}"' if notes else ''
    deadline_line = f'set due date of newItem to date "{deadline}"' if deadline else ''
    script = f'''
tell application "Reminders"
  tell list "{list_name}"
    set newItem to make new reminder with properties {{name:"{title}"}}
    {notes_line}
    {deadline_line}
  end tell
  return "Added to {list_name}: {title}"
end tell
'''
    try:
        result = subprocess.run(['osascript', '-e', script],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip()
        # Fallback: add to default list
        fallback = f'''
tell application "Reminders"
  set newItem to make new reminder with properties {{name:"{title}"}}
  {notes_line}
  return "Added to Reminders: {title}"
end tell
'''
        result2 = subprocess.run(['osascript', '-e', fallback],
                                 capture_output=True, text=True, timeout=10)
        return result2.stdout.strip() or f"[Reminders error: {result.stderr.strip()[:100]}]"
    except Exception as e:
        return f"[Reminders error: {e}]"


def read_reminders(list_name: str = "Reminders") -> str:
    """Read incomplete reminders from Apple Reminders."""
    list_name = list_name.replace('"', '\\"')
    script = f'''
tell application "Reminders"
  set results to {{}}
  try
    set targetList to list "{list_name}"
    repeat with r in (every reminder of targetList whose completed is false)
      set results to results & {{(name of r)}}
    end repeat
  on error
    repeat with r in (every reminder whose completed is false)
      set results to results & {{(name of r)}}
    end repeat
  end try
  if (count of results) = 0 then return "No pending reminders"
  set AppleScript's text item delimiters to linefeed
  return results as string
end tell
'''
    try:
        result = subprocess.run(['osascript', '-e', script],
                                capture_output=True, text=True, timeout=10)
        return result.stdout.strip() or "No pending reminders"
    except Exception as e:
        return f"[Reminders error: {e}]"


def schedule_timed_reminder(text: str, minutes: int, chat_id: int,
                            imsg_binary: str = "/opt/homebrew/bin/imsg") -> str:
    """Schedule a reminder iMessage using the `at` command."""
    try:
        safe_text = text.replace("'", "\\'").replace('"', '\\"')
        cmd = f'{imsg_binary} send --chat-id {chat_id} --service imessage --text "⏰ Reminder: {safe_text}"\n'
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
            f.write(cmd)
            tmp = f.name
        os.chmod(tmp, 0o755)
        result = subprocess.run(
            ['at', '-f', tmp, f'now + {minutes} minutes'],
            capture_output=True, text=True, timeout=5)
        os.unlink(tmp)
        if result.returncode == 0:
            return f"Reminder set: '{text}' in {minutes} minute(s)."
        return f"[Reminder scheduling failed: {result.stderr[:100]}]"
    except Exception as e:
        return f"[Reminder error: {e}]"
