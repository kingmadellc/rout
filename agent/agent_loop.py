"""
Rout agent loop — multi-turn tool-use engine.

Replaces the single-shot Claude call in general_handlers.py.
This is the brain of Rout.

Flow:
  User message → build context (memory + history)
    → Claude API call with tools
    → LOOP:
        tool_use response → safety gate check → execute tool → append result → call again
        text response → return to user
    → Max 5 iterations

Design decisions:
  - Max 5 iterations: prevents runaway loops. Real tasks complete in 2-3.
  - Tool results capped at 2000 chars: prevents context explosion.
  - Provider failover preserved: wraps existing failover logic.
  - Memory injected as system context: vector-retrieved, not full dump.
  - Safety gate: destructive tools require user confirmation before execution.
    When a destructive tool is blocked, the loop returns a confirmation prompt
    and the pending action is stored for the next user message.
"""

import json
import re
import subprocess
import yaml
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from agent import tool_registry, safety_gate, providers
from agent.tools import memory_tools


# ── Config ──────────────────────────────────────────────────────────────────

MAX_ITERATIONS = 5

def _load_config() -> dict:
    """Load personal config from config.yaml."""
    for candidate in [
        Path(__file__).parent.parent / "config.yaml",
        Path.home() / ".config/imsg-watcher/config.yaml",
    ]:
        if candidate.exists():
            with open(candidate) as f:
                return yaml.safe_load(f) or {}
    return {}

_CONFIG = _load_config()
_PATHS = _CONFIG.get("paths", {})
IMSG = _PATHS.get("imsg", "/opt/homebrew/bin/imsg")
HISTORY_LIMIT = _CONFIG.get("watcher", {}).get("history_limit", 10)


# ── Context ─────────────────────────────────────────────────────────────────

@dataclass
class AgentContext:
    """Context passed through the agent loop."""
    chat_id: int = 1
    sender_name: str = ""
    is_group: bool = False
    attachment_paths: list = field(default_factory=list)
    images: list = field(default_factory=list)


# ── Public Interface ────────────────────────────────────────────────────────

def run(message: str, context: AgentContext) -> str:
    """
    Execute the agent loop for a single user message.

    Returns the final text response to send back to the user.
    If a destructive tool is invoked, returns a confirmation prompt instead.
    """
    # Build conversation messages
    history = _load_chat_history(context.chat_id, message)
    messages = list(history)

    # Build current user message (with images if present)
    if context.images:
        content = []
        for img_data, img_type in context.images:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": img_type, "data": img_data},
            })
        content.append({"type": "text", "text": message})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": message})

    # Get tools
    tools = tool_registry.get_tool_definitions()

    # Build system prompt (with message-relevant memory retrieval)
    system = _build_system_prompt(message)

    # Max tokens from config
    max_tokens = int(_CONFIG.get("anthropic", {}).get("max_tokens", 512) or 512)

    # Agent loop
    for iteration in range(MAX_ITERATIONS):
        response = _call_claude(system, messages, tools, max_tokens, context)

        if response is None:
            return "Something went wrong — I couldn't process that. Try again?"

        stop_reason = response.get("stop_reason", "end_turn")

        # ── Text response: done ──────────────────────────────────────────
        if stop_reason == "end_turn":
            return _extract_text(response)

        # ── Tool use: execute tools and loop ─────────────────────────────
        if stop_reason == "tool_use":
            assistant_content = response.get("content", [])
            tool_results = []
            safety_blocked = False
            confirmation_prompt = None

            for block in assistant_content:
                if block.get("type") != "tool_use":
                    continue

                tool_name = block["name"]
                tool_inputs = block.get("input", {})
                tool_id = block["id"]

                # Safety gate check
                is_safe, prompt = safety_gate.check(tool_name, tool_inputs)

                if not is_safe:
                    # Destructive tool blocked — return confirmation prompt
                    safety_blocked = True
                    confirmation_prompt = prompt
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": "[Action requires user confirmation. Awaiting response.]",
                    })
                    break
                else:
                    # Safe tool — execute immediately
                    result = tool_registry.execute_tool(tool_name, tool_inputs)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result,
                    })

            if safety_blocked and confirmation_prompt:
                # Return the confirmation prompt as the response.
                # The pending action is stored by safety_gate.check().
                # On next user message, the watcher will call
                # safety_gate.check_pending() first.
                return confirmation_prompt

            # Append assistant turn + tool results, then loop
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})

            continue

        # ── Unknown stop reason ──────────────────────────────────────────
        return _extract_text(response)

    # Exhausted iterations
    return "I wasn't able to complete that. Try rephrasing?"


# ── System Prompt ───────────────────────────────────────────────────────────

def _build_system_prompt(current_message: str = "") -> str:
    """Build the system prompt with memory context (vector-retrieved if available)."""
    memory = memory_tools.load_memory_for_context(message=current_message)
    assistant_name = _CONFIG.get("user", {}).get("assistant_name", "Optimus")
    user_name = _CONFIG.get("user", {}).get("name", "")

    today = date.today().strftime("%A, %B %d, %Y")

    user_line = f"The user's name is {user_name}. " if user_name else ""

    return f"""You are {assistant_name}, a personal AI assistant. You communicate via iMessage.

{user_line}Today is {today}.

Guidelines:
- Be warm, concise, and natural. This is iMessage — keep responses short.
- Use tools to take actions (read calendar, create events, set reminders, search the web).
- When creating events or reminders, USE THE TOOLS — don't just describe what you'd do.
- If you need multiple tools, call them in sequence. Think step by step.
- Use <memory> for personal context about the user.
- For destructive actions (creating events, reminders), the safety gate will handle confirmation.
  Just call the tool — the system prompts the user to confirm.
- Never make up information. If you don't know, say so or search.

<memory>
{memory}
</memory>"""


# ── Claude API Call ─────────────────────────────────────────────────────────

def _call_claude(system: str, messages: list, tools: list,
                 max_tokens: int, context: AgentContext) -> Optional[dict]:
    """
    Call Claude via the provider failover engine.
    Returns the full API response dict (with content, stop_reason, etc.)
    or None on failure.
    """
    try:
        response = providers.request_with_failover(
            system_prompt=system,
            messages=messages,
            max_tokens=max_tokens,
            tools=tools,
        )
        return response
    except RuntimeError:
        # Provider errors (rate limit, auth, all providers down)
        # Let the caller handle with a friendly message.
        raise
    except Exception:
        return None


# ── Response Extraction ─────────────────────────────────────────────────────

def _extract_text(response: dict) -> str:
    """Extract text content from Claude's response."""
    content = response.get("content", [])
    texts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block["text"])
        elif isinstance(block, str):
            texts.append(block)
    return "\n".join(texts).strip() or "..."


# ── Chat History ────────────────────────────────────────────────────────────

_GARBLE_RE = re.compile(r'^[\ufffc\ufffd\u2028\u2029\x00-\x08\x0b\x0c\x0e-\x1f]+')

def _strip_garbled_prefix(text: str) -> str:
    """Remove garbled prefixes from Messages.app text."""
    return _GARBLE_RE.sub('', text).strip()


def _load_chat_history(chat_id: int, current_text: str,
                       limit: int = HISTORY_LIMIT) -> list:
    """
    Fetch recent messages from iMessage and return as Anthropic messages format.
    Oldest first. Excludes the current message being processed.
    """
    try:
        result = subprocess.run(
            [IMSG, 'history', '--chat-id', str(chat_id),
             '--limit', str(limit), '--json'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []

        raw = []
        for line in result.stdout.strip().splitlines():
            try:
                msg = json.loads(line)
                text = _strip_garbled_prefix((msg.get('text') or '').strip())
                if not text:
                    continue
                raw.append({
                    'role': 'assistant' if msg.get('is_from_me') else 'user',
                    'content': text,
                    'ts': msg.get('created_at', ''),
                })
            except Exception:
                continue

        raw.sort(key=lambda x: x['ts'])

        # Drop the most recent user message (it's the one we're processing now)
        for i in range(len(raw) - 1, -1, -1):
            if raw[i]['role'] == 'user':
                raw.pop(i)
                break

        messages = [{'role': m['role'], 'content': m['content']} for m in raw]

        # Merge consecutive same-role turns (Anthropic requires alternating)
        merged = []
        for m in messages:
            if merged and merged[-1]['role'] == m['role']:
                merged[-1]['content'] += '\n' + m['content']
            else:
                merged.append(dict(m))

        # Ensure starts with user
        while merged and merged[0]['role'] == 'assistant':
            merged.pop(0)

        return merged

    except Exception:
        return []
