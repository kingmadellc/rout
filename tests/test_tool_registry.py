"""Tests for agent/tool_registry.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tool_registry import (
    get_tool_definitions, execute_tool, get_safety_level,
    SAFE, DESTRUCTIVE, TOOLS,
)


def test_all_tools_registered():
    """All expected tools exist in the registry."""
    expected = [
        "read_calendar", "read_calendar_range", "create_calendar_event",
        "read_reminders", "create_reminder", "web_search",
        "query_memory", "add_memory",
    ]
    for name in expected:
        assert name in TOOLS, f"Missing tool: {name}"


def test_tool_definitions_format():
    """Tool definitions conform to Anthropic API schema."""
    defs = get_tool_definitions()
    assert len(defs) >= 8

    for tool_def in defs:
        assert "name" in tool_def
        assert "description" in tool_def
        assert "input_schema" in tool_def
        assert isinstance(tool_def["input_schema"], dict)
        assert tool_def["input_schema"].get("type") == "object"


def test_safety_levels():
    """Correct tools are marked safe vs destructive."""
    safe_tools = ["read_calendar", "read_calendar_range", "read_reminders",
                  "web_search", "query_memory", "add_memory"]
    destructive_tools = ["create_calendar_event", "create_reminder"]

    for name in safe_tools:
        assert get_safety_level(name) == SAFE, f"{name} should be safe"

    for name in destructive_tools:
        assert get_safety_level(name) == DESTRUCTIVE, f"{name} should be destructive"


def test_unknown_tool_safety():
    """Unknown tools default to safe (they'll fail at execution anyway)."""
    assert get_safety_level("nonexistent_tool") == SAFE


def test_execute_tool_caps_result():
    """Tool results are capped at 2000 chars."""
    result = execute_tool("query_memory", {"query": "test"})
    assert len(result) <= 2000


def test_execute_unknown_tool():
    """Executing unknown tool returns error string."""
    result = execute_tool("does_not_exist", {})
    assert "error" in result.lower() or "unknown" in result.lower()


def test_tool_definitions_have_descriptions():
    """Each tool has a non-empty description."""
    defs = get_tool_definitions()
    for tool_def in defs:
        assert len(tool_def["description"]) > 10, \
            f"{tool_def['name']} description too short"


def test_every_tool_has_executor():
    """Every registered tool has a callable executor."""
    for name, tool in TOOLS.items():
        assert callable(tool["executor"]), f"{name} executor not callable"


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
