"""
Tool registry for Rout agent.

Defines all tools Claude can use, their JSON schemas (for the API),
their executor functions, and their safety classification.

Tool definitions are loaded from YAML files in agent/tools/ and handlers/,
while executor function mappings are kept in Python for lambdas and complex logic.
"""

from __future__ import annotations

import yaml
from pathlib import Path
from typing import Any, Callable, Dict, Literal, TypedDict

from agent.tools import calendar_tools, reminder_tools, search_tools, memory_tools

# Kalshi handlers live in handlers/ (sibling to agent/), import with path fixup
import sys as _sys
from pathlib import Path as _Path
_handlers_dir = str(_Path(__file__).resolve().parent.parent / "handlers")
if _handlers_dir not in _sys.path:
    _sys.path.insert(0, _handlers_dir)
from kalshi_handlers import (
    portfolio_command as _kalshi_portfolio,
    markets_command as _kalshi_markets,
    scan_command as _kalshi_scan,
    cache_command as _kalshi_cache,
    buy_command as _kalshi_buy,
    sell_command as _kalshi_sell,
    get_market_command as _kalshi_get_market,
    smart_sell_command as _kalshi_smart_sell,
    cancel_order_command as _kalshi_cancel_order,
    get_open_orders_command as _kalshi_open_orders,
)
from polymarket_handlers import (
    trending_command as _pm_trending,
    odds_command as _pm_odds,
    search_command as _pm_search,
    watchlist_command as _pm_watchlist,
)


# ── Type Definitions ─────────────────────────────────────────────────────────

SafetyLevel = Literal["safe", "confirm", "destructive"]


class ToolDef(TypedDict):
    """Typed definition for a tool with all required metadata."""
    description: str
    input_schema: Dict[str, Any]
    executor: Callable[..., Any]
    safety: SafetyLevel


class ToolDefYAML(TypedDict, total=False):
    """YAML tool definition structure."""
    description: str
    input_schema: Dict[str, Any]
    safety: SafetyLevel


# ── Safety Levels ────────────────────────────────────────────────────────────

SAFE: SafetyLevel = "safe"            # Read-only, no side effects — execute immediately
CONFIRM: SafetyLevel = "confirm"      # Medium-risk — execute immediately, notify user what happened
DESTRUCTIVE: SafetyLevel = "destructive"  # High-risk — block execution, require explicit confirmation


# ── Executor Function Mapping ────────────────────────────────────────────────

def _load_tool_yaml(yaml_path: Path) -> Dict[str, ToolDefYAML]:
    """Load tool definitions from a YAML file. Returns dict of {tool_name: {schema, safety}}."""
    with open(yaml_path, 'r') as f:
        data: Any = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data.get('tools', {})


def _build_tools() -> Dict[str, ToolDef]:
    """Build the full TOOLS dict by loading YAML definitions and mapping executors.

    YAML files define schema/description/safety metadata.
    Python dict _EXECUTORS maps tool names to their callable implementations.
    """
    tools_dict: Dict[str, ToolDef] = {}

    # Scan for YAML files in agent/tools/ and handlers/
    # tool_registry.py is in agent/, so parent is the rout root
    registry_dir: Path = Path(__file__).resolve().parent
    root_dir: Path = registry_dir.parent

    yaml_search_paths: list[Path] = [
        registry_dir / "tools",
        root_dir / "handlers",
    ]

    for yaml_dir in yaml_search_paths:
        if not yaml_dir.exists():
            continue
        for yaml_file in sorted(yaml_dir.glob("*_tools.yaml")):
            yaml_tools: Dict[str, ToolDefYAML] = _load_tool_yaml(yaml_file)
            for tool_name, tool_def in yaml_tools.items():
                if tool_name in _EXECUTORS:
                    # Merge YAML definition with executor
                    tools_dict[tool_name] = {
                        "description": tool_def.get("description", ""),
                        "input_schema": tool_def.get("input_schema", {}),
                        "executor": _EXECUTORS[tool_name],
                        "safety": tool_def.get("safety", DESTRUCTIVE),
                    }
                else:
                    # Tool defined in YAML but no executor — skip with warning
                    print(f"[WARNING] Tool '{tool_name}' defined in YAML but no executor found in _EXECUTORS")

    return tools_dict


# ── Executor Function Mapping (name -> callable) ──────────────────────────────

_EXECUTORS: Dict[str, Callable[..., Any]] = {
    # Calendar tools
    "read_calendar": lambda **kwargs: calendar_tools.read_calendar(
        date_offset_days=kwargs.get("date_offset_days", 0)),

    "read_calendar_range": lambda **kwargs: calendar_tools.read_calendar_range(
        days=kwargs.get("days", 7)),

    "create_calendar_event": lambda **kwargs: calendar_tools.create_event(
        title=kwargs["title"],
        date_str=kwargs["date_str"],
        start_hour=kwargs["start_hour"],
        start_min=kwargs.get("start_min", 0),
        duration_hours=kwargs.get("duration_hours", 1.0),
        calendar_name=kwargs.get("calendar_name", "Home")),

    # Reminder tools
    "read_reminders": lambda **kwargs: reminder_tools.read_reminders(
        list_name=kwargs.get("list_name", "Reminders")),

    "create_reminder": lambda **kwargs: reminder_tools.create_reminder(
        title=kwargs["title"],
        notes=kwargs.get("notes", ""),
        list_name=kwargs.get("list_name", "Reminders"),
        deadline=kwargs.get("deadline", "")),

    "schedule_timed_reminder": lambda **kwargs: reminder_tools.schedule_timed_reminder(
        text=kwargs["text"],
        minutes=kwargs["minutes"],
        chat_id=kwargs.get("chat_id", 0),
        imsg_binary=kwargs.get("imsg_binary", "/opt/homebrew/bin/imsg")),

    # Search tools
    "web_search": lambda **kwargs: search_tools.web_search(query=kwargs["query"]),

    # Memory tools
    "query_memory": lambda **kwargs: memory_tools.query_memory(
        query=kwargs.get("query", "")),

    "add_memory": lambda **kwargs: memory_tools.add_memory(note=kwargs["note"]),

    # Kalshi tools
    "kalshi_portfolio": lambda **kwargs: _kalshi_portfolio(),

    "kalshi_get_market": lambda **kwargs: _kalshi_get_market(ticker=kwargs["ticker"]),

    "kalshi_markets": lambda **kwargs: _kalshi_markets(args=kwargs.get("filter", "")),

    "kalshi_scan": lambda **kwargs: _kalshi_scan(args=kwargs.get("filter", "")),

    "kalshi_cache_status": lambda **kwargs: _kalshi_cache(),

    "kalshi_buy": lambda **kwargs: _kalshi_buy(
        ticker=kwargs["ticker"], side=kwargs["side"],
        quantity=kwargs["quantity"], price_cents=kwargs["price_cents"]),

    "kalshi_sell": lambda **kwargs: _kalshi_sell(
        ticker=kwargs["ticker"], side=kwargs["side"],
        quantity=kwargs["quantity"], price_cents=kwargs["price_cents"]),

    "kalshi_smart_sell": lambda **kwargs: _kalshi_smart_sell(
        ticker=kwargs["ticker"], side=kwargs["side"],
        quantity=kwargs["quantity"]),

    "kalshi_cancel_order": lambda **kwargs: _kalshi_cancel_order(order_id=kwargs["order_id"]),

    "kalshi_open_orders": lambda **kwargs: _kalshi_open_orders(),

    # Polymarket tools
    "pm_trending": lambda **kwargs: _pm_trending(args=kwargs.get("category", "")),

    "pm_odds": lambda **kwargs: _pm_odds(slug_or_query=kwargs["slug"]),

    "pm_search": lambda **kwargs: _pm_search(query=kwargs["query"]),

    "pm_watchlist": lambda **kwargs: _pm_watchlist(),
}


# ── Tool Definitions ─────────────────────────────────────────────────────────

TOOLS: Dict[str, ToolDef] = _build_tools()

if not TOOLS:
    raise RuntimeError(
        "No tools loaded! Check that YAML files exist in agent/tools/ and handlers/ "
        "and that executors are registered in _EXECUTORS."
    )


def get_tool_definitions() -> list[Dict[str, Any]]:
    """Return tool definitions in Anthropic API format."""
    return [
        {
            "name": name,
            "description": tool["description"],
            "input_schema": tool["input_schema"],
        }
        for name, tool in TOOLS.items()
    ]


TOOL_TIMEOUT_SECONDS: int = 15


def execute_tool(name: str, inputs: Dict[str, Any]) -> str:
    """Execute a tool by name with the given inputs. Returns result string.

    Tools are executed with a timeout to prevent the agent loop from hanging
    on unresponsive AppleScript, HTTP, or API calls.
    """
    tool: ToolDef | None = TOOLS.get(name)
    if not tool:
        return f"[Unknown tool: {name}]"
    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(tool["executor"], **inputs)
            result: Any = future.result(timeout=TOOL_TIMEOUT_SECONDS)
        # Cap result length to prevent context explosion
        MAX_TOOL_RESULT: int = 4000
        result_str: str = str(result)
        if len(result_str) > MAX_TOOL_RESULT:
            truncation_suffix: str = "\n[... truncated]"
            result_str = result_str[:MAX_TOOL_RESULT - len(truncation_suffix)] + truncation_suffix
        return result_str
    except concurrent.futures.TimeoutError:
        return f"[Tool timeout ({name}): took longer than {TOOL_TIMEOUT_SECONDS}s]"
    except Exception as e:
        return f"[Tool error ({name}): {e}]"


def get_safety_level(name: str) -> SafetyLevel:
    """Get the safety level for a tool.
    Unknown tools default to DESTRUCTIVE (fail-closed) to prevent
    unregistered tools from bypassing the safety gate."""
    tool: ToolDef | None = TOOLS.get(name)
    return tool["safety"] if tool else DESTRUCTIVE
