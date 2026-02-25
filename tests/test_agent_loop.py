"""Tests for agent/agent_loop.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.agent_loop import (
    AgentContext, _build_system_prompt, _extract_text,
    _strip_garbled_prefix, MAX_ITERATIONS,
)


def test_agent_context_defaults():
    """AgentContext has sensible defaults."""
    ctx = AgentContext()
    assert ctx.chat_id == 1
    assert ctx.sender_name == ""
    assert ctx.is_group is False
    assert ctx.attachment_paths == []
    assert ctx.images == []


def test_agent_context_custom():
    """AgentContext accepts custom values."""
    ctx = AgentContext(
        chat_id=42,
        sender_name="TestUser",
        is_group=True,
        attachment_paths=["/tmp/test.jpg"],
    )
    assert ctx.chat_id == 42
    assert ctx.sender_name == "TestUser"
    assert ctx.is_group is True


def test_build_system_prompt_contains_memory():
    """System prompt includes memory section."""
    prompt = _build_system_prompt("test message")
    assert "<memory>" in prompt
    assert "</memory>" in prompt


def test_build_system_prompt_contains_date():
    """System prompt includes today's date."""
    prompt = _build_system_prompt()
    # Should contain a day name
    import datetime
    today = datetime.date.today().strftime("%A")
    assert today in prompt


def test_build_system_prompt_contains_guidelines():
    """System prompt includes agent behavior guidelines."""
    prompt = _build_system_prompt()
    assert "iMessage" in prompt
    assert "tools" in prompt.lower()


def test_extract_text_single_block():
    """Extract text from single text block."""
    response = {
        "content": [{"type": "text", "text": "Hello world"}],
        "stop_reason": "end_turn",
    }
    assert _extract_text(response) == "Hello world"


def test_extract_text_multiple_blocks():
    """Extract text from multiple text blocks."""
    response = {
        "content": [
            {"type": "text", "text": "Part 1"},
            {"type": "text", "text": "Part 2"},
        ],
    }
    assert "Part 1" in _extract_text(response)
    assert "Part 2" in _extract_text(response)


def test_extract_text_mixed_blocks():
    """Extract text ignoring non-text blocks."""
    response = {
        "content": [
            {"type": "tool_use", "name": "read_calendar", "id": "123", "input": {}},
            {"type": "text", "text": "Here's your calendar"},
        ],
    }
    result = _extract_text(response)
    assert "Here's your calendar" in result
    assert "tool_use" not in result


def test_extract_text_empty():
    """Extract text from empty response returns fallback."""
    response = {"content": []}
    assert _extract_text(response) == "..."


def test_strip_garbled_prefix():
    """Garbled unicode prefixes are stripped."""
    assert _strip_garbled_prefix("\ufffc\ufffdHello") == "Hello"
    assert _strip_garbled_prefix("Normal text") == "Normal text"
    assert _strip_garbled_prefix("") == ""


def test_max_iterations_constant():
    """MAX_ITERATIONS is reasonable."""
    assert MAX_ITERATIONS == 5


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
