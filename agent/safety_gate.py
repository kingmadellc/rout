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
import re
import time
from pathlib import Path
from typing import Optional, Tuple

from agent.tool_registry import get_safety_level, execute_tool, SAFE, CONFIRM, DESTRUCTIVE


# ── Config ──────────────────────────────────────────────────────────────────

PENDING_ACTION_PATH = Path.home() / ".openclaw" / "state" / "pending_action.json"
EXPIRY_SECONDS = 3600  # 1 hour

# Confirmation patterns (case-insensitive, exact match after strip)
CONFIRM_WORDS = {"yes", "y", "confirm", "do it", "go ahead", "yep", "yeah",
                 "sure", "ok", "okay", "yea", "proceed", "approved", "send it",
                 "go for it", "definitely", "absolutely"}

CANCEL_WORDS = {"no", "n", "cancel", "nevermind", "never mind", "nah",
                "nope", "stop", "don't", "dont", "skip", "abort"}

POLITE_TAIL_WORDS = {"please", "pls", "thanks", "thank", "you", "now"}

# ── Prompt Templates ────────────────────────────────────────────────────────

PROMPT_TEMPLATES = {
    "create_calendar_event": (
        "Create event on {calendar_name} calendar?\n"
        "📅 {title}\n"
        "🕐 {date_str} at {time_str} ({dur_str})\n\n"
        "Reply yes or no."
    ),
    "create_reminder": (
        "Create reminder in {list_name}?\n"
        "📝 {title}{deadline_line}{notes_line}\n\n"
        "Reply yes or no."
    ),
    "schedule_timed_reminder": (
        "Set a timed alert?\n"
        '⏰ "{text}" in {minutes} minutes\n\n'
        "Reply yes or no."
    ),
    "kalshi_smart_sell": (
        "Sell {quantity}x {side} on {name}?\n"
        "This will sell at the live bid for instant fill.\n\n"
        "Reply yes or no."
    ),
    "kalshi_buy": (
        "Buy {quantity}x {side} on {name} at {price_cents}¢?\n"
        "Total cost: ${cost:.2f}\n\n"
        "Reply yes or no."
    ),
    "kalshi_sell": (
        "Sell {quantity}x {side} on {name} at {price_cents}¢?\n"
        "Est. proceeds: ${proceeds:.2f}\n\n"
        "Reply yes or no."
    ),
    "kalshi_cancel_order": (
        "Cancel order {order_id}?\n\n"
        "Reply yes or no."
    ),
}


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

def _normalize_sender(sender: str) -> str:
    """Normalize sender for stable matching across transports.

    Preserves full digit sequence to prevent cross-user collisions
    in group chats (e.g., +1-555-123-4567 vs +2-555-123-4567).
    """
    s = (sender or "").strip()
    if not s:
        return ""
    if "@" in s:
        return s.lower()
    digits = re.sub(r"\D", "", s)
    if digits:
        return f"phone:{digits}"
    return s.lower()


def _normalize_confirmation_text(text: str) -> str:
    """Normalize confirmation text for robust yes/no parsing."""
    normalized = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    return " ".join(normalized.split())


def _matches_intent(normalized: str, intents: set[str]) -> bool:
    """Exact match, or intent + polite tail (e.g. 'yes please')."""
    if normalized in intents:
        return True

    for intent in intents:
        prefix = f"{intent} "
        if normalized.startswith(prefix):
            tail = normalized[len(prefix):].strip()
            if tail and all(tok in POLITE_TAIL_WORDS for tok in tail.split()):
                return True
    return False


def check(tool_name: str, tool_inputs: dict, chat_id: Optional[int] = None,
          sender: str = "") -> Tuple[bool, Optional[str]]:
    """
    Check if a tool call is safe to execute immediately.

    Returns:
        (True, None)   — SAFE or CONFIRM: execute now. CONFIRM tools are logged.
        (False, prompt) — DESTRUCTIVE: prompt the user for confirmation.
    """
    safety = get_safety_level(tool_name)

    if safety == SAFE:
        return (True, None)

    if safety == CONFIRM:
        # Execute immediately but log for audit trail
        _log_confirm_action(tool_name, tool_inputs)
        return (True, None)

    # DESTRUCTIVE: block and require confirmation
    prompt = _build_confirmation_prompt(tool_name, tool_inputs)

    _save_pending({
        "tool_name": tool_name,
        "tool_inputs": tool_inputs,
        "prompt": prompt,
        "chat_id": chat_id,
        "sender": _normalize_sender(sender),
        "created_at": time.time(),
    })

    return (False, prompt)


def _log_confirm_action(tool_name: str, inputs: dict) -> None:
    """Log a CONFIRM-tier action for audit trail."""
    try:
        log_path = PENDING_ACTION_PATH.parent / "confirm_actions.jsonl"
        entry = {
            "tool": tool_name,
            "inputs": {k: str(v)[:100] for k, v in inputs.items()},
            "timestamp": time.time(),
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def check_pending(text: str, chat_id: Optional[int] = None, sender: str = "") -> Optional[str]:
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

    pending_chat_id = pending.get("chat_id")
    if pending_chat_id is not None and chat_id is not None and pending_chat_id != chat_id:
        return None

    pending_sender = pending.get("sender", "")
    if pending_sender and _normalize_sender(sender) != pending_sender:
        return None

    normalized = _normalize_confirmation_text(text)

    # Check for confirmation
    if _matches_intent(normalized, CONFIRM_WORDS):
        tool_name = pending["tool_name"]
        tool_inputs = pending["tool_inputs"]
        try:
            result = execute_tool(tool_name, tool_inputs)
        except Exception as e:
            # Re-save pending so user can retry on failure
            _save_pending(pending)
            return f"Action failed: {e}\nReply yes to retry, or no to cancel."
        _clear_pending()
        return result

    # Check for cancellation
    if _matches_intent(normalized, CANCEL_WORDS):
        _clear_pending()
        return "👍 Cancelled."

    # Not a confirmation response — clear stale pending and process normally.
    # This avoids executing an old destructive action on a later "yes".
    _clear_pending()
    return None


def has_pending() -> bool:
    """Check if there's a pending action waiting for confirmation."""
    return _load_pending() is not None


def clear_expired() -> None:
    """Clear expired pending actions. Called by watcher on each poll."""
    _load_pending()  # Side effect: clears if expired


# ── Prompt Builder ──────────────────────────────────────────────────────────

def _prepare_template_vars(tool_name: str, inputs: dict) -> dict:
    """
    Extract and transform inputs into template-ready variables.
    Handles time formatting, ticker resolution, and derived values.
    """
    vars_dict = {}

    if tool_name == "create_calendar_event":
        vars_dict["title"] = inputs.get("title", "Untitled")
        vars_dict["date_str"] = inputs.get("date_str", "")
        vars_dict["calendar_name"] = inputs.get("calendar_name", "Home")

        hour = inputs.get("start_hour", 12)
        minute = inputs.get("start_min", 0)
        duration = inputs.get("duration_hours", 1.0)

        # Convert to 12h for readability
        if hour == 0:
            time_str = f"12:{minute:02d} AM"
        elif hour < 12:
            time_str = f"{hour}:{minute:02d} AM"
        elif hour == 12:
            time_str = f"12:{minute:02d} PM"
        else:
            time_str = f"{hour - 12}:{minute:02d} PM"
        vars_dict["time_str"] = time_str

        dur_str = f"{duration}h" if duration != 1.0 else "1 hour"
        vars_dict["dur_str"] = dur_str

    elif tool_name == "create_reminder":
        vars_dict["title"] = inputs.get("title", "")
        vars_dict["list_name"] = inputs.get("list_name", "Reminders")

        deadline = inputs.get("deadline", "")
        deadline_line = f"\n📅 Due: {deadline}" if deadline else ""
        vars_dict["deadline_line"] = deadline_line

        notes = inputs.get("notes", "")
        notes_line = f"\n💬 {notes}" if notes else ""
        vars_dict["notes_line"] = notes_line

    elif tool_name == "schedule_timed_reminder":
        vars_dict["text"] = inputs.get("text", "")
        vars_dict["minutes"] = inputs.get("minutes", 0)

    elif tool_name == "kalshi_smart_sell":
        ticker = inputs.get("ticker", "?")
        side = inputs.get("side", "?").upper()
        vars_dict["quantity"] = inputs.get("quantity", 0)
        vars_dict["side"] = side

        # Resolve ticker name
        try:
            from handlers.kalshi_handlers import TICKER_NAMES
            name = TICKER_NAMES.get(ticker, ticker)
        except Exception:
            name = ticker
        vars_dict["name"] = name

    elif tool_name == "kalshi_buy":
        ticker = inputs.get("ticker", "?")
        side = inputs.get("side", "?").upper()
        qty = inputs.get("quantity", 0)
        price = inputs.get("price_cents", 0)
        cost = qty * price / 100.0

        vars_dict["quantity"] = qty
        vars_dict["side"] = side
        vars_dict["price_cents"] = price
        vars_dict["cost"] = cost

        # Resolve ticker name
        try:
            from handlers.kalshi_handlers import TICKER_NAMES
            name = TICKER_NAMES.get(ticker, ticker)
        except Exception:
            name = ticker
        vars_dict["name"] = name

    elif tool_name == "kalshi_sell":
        ticker = inputs.get("ticker", "?")
        side = inputs.get("side", "?").upper()
        qty = inputs.get("quantity", 0)
        price = inputs.get("price_cents", 0)
        proceeds = qty * price / 100.0

        vars_dict["quantity"] = qty
        vars_dict["side"] = side
        vars_dict["price_cents"] = price
        vars_dict["proceeds"] = proceeds

        # Resolve ticker name
        try:
            from handlers.kalshi_handlers import TICKER_NAMES
            name = TICKER_NAMES.get(ticker, ticker)
        except Exception:
            name = ticker
        vars_dict["name"] = name

    elif tool_name == "kalshi_cancel_order":
        vars_dict["order_id"] = inputs.get("order_id", "?")

    return vars_dict


def _build_confirmation_prompt(tool_name: str, inputs: dict) -> str:
    """Build a human-readable confirmation prompt for a destructive action."""

    if tool_name in PROMPT_TEMPLATES:
        template = PROMPT_TEMPLATES[tool_name]
        vars_dict = _prepare_template_vars(tool_name, inputs)
        return template.format(**vars_dict)
    else:
        # Generic fallback for any future destructive tool
        return (
            f"Execute {tool_name}?\n"
            f"Inputs: {json.dumps(inputs, indent=2)}\n\n"
            f"Reply yes or no."
        )
