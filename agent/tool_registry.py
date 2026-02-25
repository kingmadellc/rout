"""
Tool registry for Rout agent.

Defines all tools Claude can use, their JSON schemas (for the API),
their executor functions, and their safety classification.
"""

from agent.tools import calendar_tools, reminder_tools, search_tools, memory_tools


# ── Safety Levels ────────────────────────────────────────────────────────────

SAFE = "safe"           # Read-only, no side effects
DESTRUCTIVE = "destructive"  # Creates, modifies, or deletes data — needs confirmation


# ── Tool Definitions ─────────────────────────────────────────────────────────

TOOLS = {
    "read_calendar": {
        "description": "Read calendar events for today, tomorrow, or a specific date offset. "
                       "Returns event titles, times, and calendar names.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_offset_days": {
                    "type": "integer",
                    "description": "0 for today, 1 for tomorrow, 2 for day after, etc. Default 0.",
                    "default": 0,
                },
            },
        },
        "executor": lambda **kwargs: calendar_tools.read_calendar(
            date_offset_days=kwargs.get("date_offset_days", 0)),
        "safety": SAFE,
    },

    "read_calendar_range": {
        "description": "Read calendar events for the next N days. "
                       "Use when the user asks about 'this week' or 'next few days'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to look ahead. Default 7.",
                    "default": 7,
                },
            },
        },
        "executor": lambda **kwargs: calendar_tools.read_calendar_range(
            days=kwargs.get("days", 7)),
        "safety": SAFE,
    },

    "create_calendar_event": {
        "description": "Create a new calendar event. Provide title, date, time, and optional duration.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title"},
                "date_str": {
                    "type": "string",
                    "description": "Date in natural format like 'February 28, 2026' or 'March 1 2026'",
                },
                "start_hour": {
                    "type": "integer",
                    "description": "Start hour in 24h format (e.g., 14 for 2 PM)",
                },
                "start_min": {
                    "type": "integer",
                    "description": "Start minute (0-59). Default 0.",
                    "default": 0,
                },
                "duration_hours": {
                    "type": "number",
                    "description": "Duration in hours. Default 1.0.",
                    "default": 1.0,
                },
                "calendar_name": {
                    "type": "string",
                    "description": "Calendar name (e.g., 'Home', 'Work'). Default 'Home'.",
                    "default": "Home",
                },
            },
            "required": ["title", "date_str", "start_hour"],
        },
        "executor": lambda **kwargs: calendar_tools.create_event(
            title=kwargs["title"],
            date_str=kwargs["date_str"],
            start_hour=kwargs["start_hour"],
            start_min=kwargs.get("start_min", 0),
            duration_hours=kwargs.get("duration_hours", 1.0),
            calendar_name=kwargs.get("calendar_name", "Home")),
        "safety": DESTRUCTIVE,
    },

    "read_reminders": {
        "description": "Read incomplete reminders from Apple Reminders app.",
        "input_schema": {
            "type": "object",
            "properties": {
                "list_name": {
                    "type": "string",
                    "description": "Reminder list name. Default 'Reminders'.",
                    "default": "Reminders",
                },
            },
        },
        "executor": lambda **kwargs: reminder_tools.read_reminders(
            list_name=kwargs.get("list_name", "Reminders")),
        "safety": SAFE,
    },

    "create_reminder": {
        "description": "Create a new reminder/task in Apple Reminders. "
                       "For timed alerts, use schedule_timed_reminder instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Reminder title"},
                "notes": {
                    "type": "string",
                    "description": "Optional notes or details",
                    "default": "",
                },
                "list_name": {
                    "type": "string",
                    "description": "Reminder list name. Default 'Reminders'.",
                    "default": "Reminders",
                },
                "deadline": {
                    "type": "string",
                    "description": "Due date in natural format. Optional.",
                    "default": "",
                },
            },
            "required": ["title"],
        },
        "executor": lambda **kwargs: reminder_tools.create_reminder(
            title=kwargs["title"],
            notes=kwargs.get("notes", ""),
            list_name=kwargs.get("list_name", "Reminders"),
            deadline=kwargs.get("deadline", "")),
        "safety": DESTRUCTIVE,
    },

    "web_search": {
        "description": "Search the web for current information, news, prices, scores, weather, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
        "executor": lambda **kwargs: search_tools.web_search(query=kwargs["query"]),
        "safety": SAFE,
    },

    "query_memory": {
        "description": "Search the user's persistent memory for relevant context, "
                       "preferences, and saved notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for in memory",
                    "default": "",
                },
            },
        },
        "executor": lambda **kwargs: memory_tools.query_memory(
            query=kwargs.get("query", "")),
        "safety": SAFE,
    },

    "add_memory": {
        "description": "Save a note to the user's persistent memory. "
                       "Use for preferences, facts, or context worth remembering.",
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "The note to save"},
            },
            "required": ["note"],
        },
        "executor": lambda **kwargs: memory_tools.add_memory(note=kwargs["note"]),
        "safety": SAFE,  # Low-risk write — adding memory is always safe
    },
}


def get_tool_definitions() -> list[dict]:
    """Return tool definitions in Anthropic API format."""
    return [
        {
            "name": name,
            "description": tool["description"],
            "input_schema": tool["input_schema"],
        }
        for name, tool in TOOLS.items()
    ]


def execute_tool(name: str, inputs: dict) -> str:
    """Execute a tool by name with the given inputs. Returns result string."""
    tool = TOOLS.get(name)
    if not tool:
        return f"[Unknown tool: {name}]"
    try:
        result = tool["executor"](**inputs)
        # Cap result length to prevent context explosion
        MAX_TOOL_RESULT = 2000
        if len(str(result)) > MAX_TOOL_RESULT:
            truncation_suffix = "\n[... truncated]"
            result = str(result)[:MAX_TOOL_RESULT - len(truncation_suffix)] + truncation_suffix
        return str(result)
    except Exception as e:
        return f"[Tool error ({name}): {e}]"


def get_safety_level(name: str) -> str:
    """Get the safety level for a tool."""
    tool = TOOLS.get(name)
    return tool["safety"] if tool else SAFE
