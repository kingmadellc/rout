"""
Tool format converters: Anthropic <-> OpenAI/Ollama.

Rout's agent loop speaks Anthropic format (tool_use blocks with IDs).
Ollama/Qwen speaks OpenAI format (function calling).
This module bridges the gap cleanly.
"""

import json
import uuid


# ── Anthropic -> OpenAI (for sending tool definitions to Ollama) ─────────────

def anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert Anthropic tool definitions to OpenAI/Ollama format.

    Anthropic format:
        {"name": "web_search", "description": "...", "input_schema": {...}}

    OpenAI/Ollama format:
        {"type": "function", "function": {"name": "web_search", "description": "...", "parameters": {...}}}
    """
    openai_tools = []
    for tool in tools:
        schema = tool.get("input_schema", {})
        # Ollama/Qwen requires "required" field even if empty
        if "required" not in schema:
            schema = {**schema, "required": []}
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": schema,
            },
        })
    return openai_tools


# ── OpenAI response -> Anthropic response (for agent loop consumption) ────────

def _generate_tool_use_id() -> str:
    """Generate a tool_use ID compatible with Anthropic's format."""
    return f"toolu_{uuid.uuid4().hex[:24]}"


def ollama_response_to_anthropic(ollama_result: dict) -> dict:
    """Convert an Ollama /api/chat response to Anthropic Messages API format.

    Handles three cases:
    1. Pure text response (no tool calls)
    2. Tool calls only (no text)
    3. Mixed: text + tool calls

    Ollama tool_calls format:
        message.tool_calls = [
            {"function": {"name": "web_search", "arguments": {"query": "..."}}}
        ]

    Anthropic format:
        content = [
            {"type": "text", "text": "Let me search..."},
            {"type": "tool_use", "id": "toolu_xxx", "name": "web_search", "input": {"query": "..."}}
        ]
    """
    message = ollama_result.get("message", {})
    content_blocks = []

    # Extract text content
    text = str(message.get("content", "")).strip()
    if text:
        content_blocks.append({"type": "text", "text": text})

    # Extract tool calls
    tool_calls = message.get("tool_calls")
    has_tool_calls = bool(tool_calls)

    if has_tool_calls:
        for tc in tool_calls:
            func = tc.get("function", {})
            name = func.get("name", "")
            arguments = func.get("arguments", {})

            # Arguments might be a JSON string or already a dict
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (json.JSONDecodeError, TypeError):
                    # Log instead of silently dropping tool arguments
                    import logging
                    logging.getLogger("rout.tool_format").warning(
                        f"Tool '{name}' returned invalid JSON arguments: {arguments[:200]}"
                    )
                    arguments = {"_parse_error": f"Invalid JSON from model: {arguments[:100]}"}

            content_blocks.append({
                "type": "tool_use",
                "id": _generate_tool_use_id(),
                "name": name,
                "input": arguments,
            })

    # Determine stop_reason
    stop_reason = "tool_use" if has_tool_calls else "end_turn"

    return {
        "content": content_blocks,
        "stop_reason": stop_reason,
        "provider": "local",
    }


# ── Anthropic messages -> OpenAI messages (for conversation history) ──────────

def anthropic_messages_to_openai(messages: list[dict], system_prompt: str = "") -> list[dict]:
    """Convert Anthropic conversation messages to OpenAI/Ollama chat format.

    Handles:
    - Simple text messages (user/assistant)
    - Image content blocks (converted to text placeholder)
    - Tool use blocks in assistant messages
    - Tool result blocks in user messages
    """
    openai_messages = []

    # System prompt as first message
    if system_prompt:
        openai_messages.append({"role": "system", "content": system_prompt})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")

        # Simple string content
        if isinstance(content, str):
            openai_messages.append({"role": role, "content": content})
            continue

        # List content (Anthropic multi-block format)
        if isinstance(content, list):
            # Check if this is a tool_result array (user turn after tool_use)
            if content and isinstance(content[0], dict) and content[0].get("type") == "tool_result":
                # Convert each tool_result to an OpenAI tool message
                for block in content:
                    if block.get("type") == "tool_result":
                        openai_messages.append({
                            "role": "tool",
                            "content": str(block.get("content", "")),
                            "tool_call_id": block.get("tool_use_id", ""),
                        })
                continue

            # Check if assistant message contains tool_use blocks
            has_tool_use = any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in content
            )

            if has_tool_use and role == "assistant":
                # Build assistant message with tool_calls
                text_parts = []
                tool_calls = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", _generate_tool_use_id()),
                            "type": "function",
                            "function": {
                                "name": block["name"],
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        })

                assistant_msg = {
                    "role": "assistant",
                    "content": "\n".join(text_parts).strip() or None,
                    "tool_calls": tool_calls,
                }
                openai_messages.append(assistant_msg)
                continue

            # Regular multi-block content (text + images)
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "image":
                        text_parts.append("[Image]")
                elif isinstance(block, str):
                    text_parts.append(block)

            openai_messages.append({
                "role": role,
                "content": "\n".join(text_parts).strip() or "(empty)",
            })
            continue

        # Fallback
        openai_messages.append({"role": role, "content": str(content or "")})

    return openai_messages
