"""Tests for agent/safety_gate.py"""

import json
import time
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.safety_gate import (
    check, check_pending, has_pending, _clear_pending,
    _save_pending, _load_pending, PENDING_ACTION_PATH,
    CONFIRM_WORDS, CANCEL_WORDS, EXPIRY_SECONDS,
)
from agent.tool_registry import SAFE, DESTRUCTIVE


def setup():
    """Clear any pending actions before each test."""
    _clear_pending()


def test_safe_tool_passes_immediately():
    setup()
    is_safe, prompt = check("read_calendar", {"date_offset_days": 0})
    assert is_safe is True
    assert prompt is None


def test_safe_tools_all_pass():
    setup()
    safe_tools = ["read_calendar", "read_calendar_range", "read_reminders",
                  "web_search", "query_memory", "add_memory"]
    for tool in safe_tools:
        is_safe, prompt = check(tool, {})
        assert is_safe is True, f"{tool} should be safe"


def test_destructive_tool_blocks():
    setup()
    is_safe, prompt = check("create_calendar_event", {
        "title": "Dentist",
        "date_str": "March 1",
        "start_hour": 14,
        "start_min": 0,
    })
    assert is_safe is False
    assert prompt is not None
    assert "Dentist" in prompt
    assert "yes" in prompt.lower() or "no" in prompt.lower()


def test_create_reminder_blocks():
    setup()
    is_safe, prompt = check("create_reminder", {
        "title": "Buy groceries",
        "list_name": "Reminders",
    })
    assert is_safe is False
    assert "Buy groceries" in prompt


def test_pending_action_stored():
    setup()
    check("create_calendar_event", {"title": "Test Event"})
    assert has_pending() is True

    pending = _load_pending()
    assert pending is not None
    assert pending["tool_name"] == "create_calendar_event"
    assert pending["tool_inputs"]["title"] == "Test Event"


def test_confirm_yes_executes():
    setup()
    # Store a pending action (we'll use read_reminders since
    # create_calendar_event would try to call AppleScript)
    _save_pending({
        "tool_name": "query_memory",  # safe tool for testing
        "tool_inputs": {"query": "test"},
        "prompt": "Test confirmation?",
        "created_at": time.time(),
    })

    result = check_pending("yes")
    assert result is not None
    assert has_pending() is False


def test_confirm_variations():
    setup()
    for word in ["yes", "y", "confirm", "do it", "go ahead", "yep", "yeah",
                 "sure", "ok", "okay", "proceed"]:
        _save_pending({
            "tool_name": "query_memory",
            "tool_inputs": {"query": "test"},
            "prompt": "Test?",
            "created_at": time.time(),
        })
        result = check_pending(word)
        assert result is not None, f"'{word}' should confirm"
        assert has_pending() is False, f"Pending should be cleared after '{word}'"


def test_cancel_variations():
    setup()
    for word in ["no", "n", "cancel", "nevermind", "nah", "nope", "stop"]:
        _save_pending({
            "tool_name": "query_memory",
            "tool_inputs": {},
            "prompt": "Test?",
            "created_at": time.time(),
        })
        result = check_pending(word)
        assert result is not None, f"'{word}' should cancel"
        assert "cancel" in result.lower() or "👍" in result
        assert has_pending() is False


def test_non_confirmation_returns_none():
    setup()
    _save_pending({
        "tool_name": "query_memory",
        "tool_inputs": {},
        "prompt": "Test?",
        "created_at": time.time(),
    })
    result = check_pending("what's the weather?")
    assert result is None  # Not a confirmation, process normally


def test_no_pending_returns_none():
    setup()
    result = check_pending("yes")
    assert result is None


def test_expired_pending_cleared():
    setup()
    _save_pending({
        "tool_name": "query_memory",
        "tool_inputs": {},
        "prompt": "Test?",
        "created_at": time.time() - EXPIRY_SECONDS - 1,
    })
    assert has_pending() is False


def test_confirmation_prompt_formatting():
    setup()
    _, prompt = check("create_calendar_event", {
        "title": "Team Meeting",
        "date_str": "February 28",
        "start_hour": 15,
        "start_min": 30,
        "duration_hours": 1.5,
        "calendar_name": "Work",
    })
    assert "Team Meeting" in prompt
    assert "February 28" in prompt
    assert "3:30 PM" in prompt
    assert "Work" in prompt
    assert "1.5h" in prompt


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✅ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
