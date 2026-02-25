"""
Safety gate for Rout agent.

Intercepts destructive tool calls (calendar writes, reminder creation)
and requires user confirmation before execution.

Flow:
  1. Agent loop calls `check(tool_name, tool_inputs)`.
  2. If tool is SAFE → returns (True, None). Execute immediately.
  3. If tool is DESTRUCTIVE → stores pending action, returns (False, confirmation_prompt).
     Agent loop sends confirmation prompt to user.
  4. On next user message, watcher calls `check_pending(text)`:
     - If confirmation match → execute pending action, return result.
     - If cancel match → discard, return cancellation message.
     - If no match → return None (not a confirmation, process normally).

State: ~/.openclaw/state/pending_action.json
One pending action at a time. Expires after 1 hour.
"""

import json
import time
from pathlib import Path
from typing import Optional, Tuple

from agent.tool_registry import get_safety_level, execute_tool, DESTRUCTIVE


# ── Config ──────────────────────────────────────────────────────────────────

PENDING_ACTION_PATH = Path.home() / ".openclaw" / "state" / "pending_action.json"
EXPIRY_SECONDS = 3600  # 1 hour

# Confirmation patterns (case-insensitive, exact match after strip)
CONFIRM_WORDS = {"yes", "y", "confirm", "do it", "go ahead", "yep", "yeah",
                 "sure", "ok", "okay", "yea", "proceed", "approved", "send it",
                 "go for it", "definitely", "absolutely"}

CANCEL_WORDS = {"no", "n", "cancel", "nevermind", "never mind", "nah",
                "nope", "stop", "don't", "dont", "skip", "abort"}


# ── Pending Action Store ────────────────────────────────────────────────────

def _save_pending(action: dict) -> None:
    """Save a pending action to disk."""
    PENDING_ACTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PENDING_ACTION_PATH, "w") as f:
        json.dump(action, f, indent=2)


def _load_pending() -> Optional[dict]:
    """Load pending action from disk. Returns None if expired or missing."""
    if not PENDING_ACTION_PATH.exists():
        return None
    try:
        with open(PENDING_ACTION_PATH, "r") as f:
            action = json.load(f)
        # Check expiry
        created_at = action.get("created_at", 0)
        if time.time() - created_at > EXPIRY_SECONDS:
            _clear_pending()
            return None
        return action
    except (json.JSONDecodeError, KeyError, TypeError):
        _clear_pending()
        return None


def _clear_pending() -> None:
    """Remove pending action file."""
    try:
        if PENDING_ACTION_PATH.exists():
            PENDING_ACTION_PATH.unlink()
    except OSError:
        pass


# ── Public Interface ────────────────────────────────────────────────────────

def check(tool_name: str, tool_inputs: dict) -> Tuple[bool, Optional[str]]:
    """
    Check if a tool call is safe to execute immediately.

    Returns:
        (True, None)  — safe to execute now.
        (False, prompt) — destructive; prompt the user for confirmation.
    """
    safety = get_safety_level(tool_name)

    if safety != DESTRUCTIVE:
        return (True, None)

    # Build a human-readable confirmation prompt
    prompt = _build_confirmation_prompt(tool_name, tool_inputs)

    # Store pending action
    _save_pending({
        "tool_name": tool_name,
        "tool_inputs": tool_inputs,
        "prompt": prompt,
        "created_at": time.time(),
    })

    return (False, prompt)


def check_pending(text: str) -> Optional[str]:
    """
    Check if an incoming message is a confirmation or cancellation
    of a pending destructive action.

    Returns:
        - Result string if action was confirmed and executed.
        - Cancellation message if action was cancelled.
        - None if no pending action or text isn't a confirmation/cancel.
    """
    pending = _load_pending()
    if not pending:
        return None

    normalized = text.strip().lower()

    # Check for confirmation
    if normalized in CONFIRM_WORDS:
        tool_name = pending["tool_name"]
        tool_inputs = pending["tool_inputs"]
        _clear_pending()
        result = execute_tool(tool_name, tool_inputs)
        return f"✅ Done. {result}"

    # Check for cancellation
    if normalized in CANCEL_WORDS:
        _clear_pending()
        return "👍 Cancelled."

    # Not a confirmation response — clear stale pending and process normally
    # (User moved on to a different question)
    return None


def has_pending() -> bool:
    """Check if there's a pending action waiting for confirmation."""
    return _load_pending() is not None


def clear_expired() -> None:
    """Clear expired pending actions. Called by watcher on each poll."""
    _load_pending()  # Side effect: clears if expired


# ── Prompt Builder ──────────────────────────────────────────────────────────

def _build_confirmation_prompt(tool_name: str, inputs: dict) -> str:
    """Build a human-readable confirmation prompt for a destructive action."""

    if tool_name == "create_calendar_event":
        title = inputs.get("title", "Untitled")
        date_str = inputs.get("date_str", "")
        hour = inputs.get("start_hour", 12)
        minute = inputs.get("start_min", 0)
        duration = inputs.get("duration_hours", 1.0)
        calendar = inputs.get("calendar_name", "Home")

        time_str = f"{hour}:{minute:02d}"
        # Convert to 12h for readability
        if hour == 0:
            time_str = f"12:{minute:02d} AM"
        elif hour < 12:
            time_str = f"{hour}:{minute:02d} AM"
        elif hour == 12:
            time_str = f"12:{minute:02d} PM"
        else:
            time_str = f"{hour - 12}:{minute:02d} PM"

        dur_str = f"{duration}h" if duration != 1.0 else "1 hour"
        return (
            f"Create event on {calendar} calendar?\n"
            f"📅 {title}\n"
            f"🕐 {date_str} at {time_str} ({dur_str})\n\n"
            f"Reply yes or no."
        )

    elif tool_name == "create_reminder":
        title = inputs.get("title", "")
        notes = inputs.get("notes", "")
        deadline = inputs.get("deadline", "")
        list_name = inputs.get("list_name", "Reminders")

        prompt = f"Create reminder in {list_name}?\n📝 {title}"
        if deadline:
            prompt += f"\n📅 Due: {deadline}"
        if notes:
            prompt += f"\n💬 {notes}"
        prompt += "\n\nReply yes or no."
        return prompt

    elif tool_name == "schedule_timed_reminder":
        text = inputs.get("text", "")
        minutes = inputs.get("minutes", 0)
        return (
            f"Set a timed alert?\n"
            f"⏰ \"{text}\" in {minutes} minutes\n\n"
            f"Reply yes or no."
        )

    else:
        # Generic fallback for any future destructive tool
        return (
            f"Execute {tool_name}?\n"
            f"Inputs: {json.dumps(inputs, indent=2)}\n\n"
            f"Reply yes or no."
        )
