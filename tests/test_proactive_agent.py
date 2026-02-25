"""Tests for scripts/proactive_agent.py"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.proactive_agent import (
    _load_state, _save_state, _rate_limited, _record_send,
    _parse_upcoming_events, MAX_MESSAGES_PER_HOUR,
    PROACTIVE_STATE_PATH,
)


def test_load_empty_state():
    """Loading state when no file exists returns defaults."""
    state = _load_state()
    assert "sent_timestamps" in state
    assert "last_briefing_date" in state
    assert "reminded_events" in state


def test_rate_limit_not_hit():
    """Rate limiter allows sends when under limit."""
    state = {"sent_timestamps": [], "last_briefing_date": "", "reminded_events": []}
    assert _rate_limited(state) is False


def test_rate_limit_hit():
    """Rate limiter blocks when at limit."""
    now = time.time()
    state = {
        "sent_timestamps": [now - i for i in range(MAX_MESSAGES_PER_HOUR)],
        "last_briefing_date": "",
        "reminded_events": [],
    }
    assert _rate_limited(state) is True


def test_rate_limit_old_timestamps_cleared():
    """Old timestamps (>1hr) are cleaned."""
    old = time.time() - 7200  # 2 hours ago
    state = {
        "sent_timestamps": [old, old - 1, old - 2],
        "last_briefing_date": "",
        "reminded_events": [],
    }
    assert _rate_limited(state) is False
    assert len(state["sent_timestamps"]) == 0


def test_record_send():
    """Recording a send adds timestamp."""
    state = {"sent_timestamps": []}
    _record_send(state)
    assert len(state["sent_timestamps"]) == 1
    assert time.time() - state["sent_timestamps"][0] < 5


def test_parse_upcoming_events_empty():
    """No events parsed from empty string."""
    result = _parse_upcoming_events("")
    assert result == []


def test_parse_upcoming_events_no_match():
    """No events parsed when times don't match lookahead."""
    result = _parse_upcoming_events("All day event - Vacation", lookahead_minutes=30)
    assert result == []


def test_state_round_trip():
    """State can be saved and loaded."""
    test_state = {
        "sent_timestamps": [time.time()],
        "last_briefing_date": "2026-02-24",
        "reminded_events": ["2026-02-24:Meeting:10:00"],
    }
    _save_state(test_state)
    loaded = _load_state()
    assert loaded["last_briefing_date"] == "2026-02-24"
    assert len(loaded["reminded_events"]) == 1


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
