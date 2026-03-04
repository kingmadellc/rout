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

# Local-only mode: bypass cloud providers entirely
try:
    from agent import providers_local
    _LOCAL_ONLY = providers_local.is_local_only()
except ImportError:
    _LOCAL_ONLY = False


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
HISTORY_LIMIT = _CONFIG.get("watcher", {}).get("history_limit", 25)


# ── Context ─────────────────────────────────────────────────────────────────

@dataclass
class AgentContext:
    """Context passed through the agent loop."""
    chat_id: int = 1
    sender: str = ""
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

    # Get tools (only sent to tool-capable providers)
    tools = tool_registry.get_tool_definitions()

    # Build system prompt (with message-relevant memory retrieval)
    system = _build_system_prompt(message)

    # Max tokens from config (4096 default — enough for multi-tool responses)
    max_tokens = int(_CONFIG.get("anthropic", {}).get("max_tokens", 4096) or 4096)

    # Agent loop
    degraded_provider = None
    used_any_tools = False
    for iteration in range(MAX_ITERATIONS):
        response = _call_claude(system, messages, tools, max_tokens, context)

        if response is None:
            return "Something went wrong — I couldn't process that. Try again?"

        # Track if we're on a degraded (non-tool-capable) provider
        # Note: local-only mode with Qwen has full tool support — not degraded
        active_provider = response.get("_active_provider", "anthropic")
        if active_provider == "codex":
            degraded_provider = active_provider
        elif active_provider == "local" and not _LOCAL_ONLY:
            # Only mark local as degraded when it's chat-only (no tool calling)
            if not _CONFIG.get("local_model", {}).get("tool_capable", False):
                degraded_provider = active_provider

        stop_reason = response.get("stop_reason", "end_turn")

        # ── Text response: done ──────────────────────────────────────────
        if stop_reason == "end_turn":
            text = _extract_text(response)
            _is_tool_capable = (
                active_provider == "anthropic"
                or (active_provider == "local"
                    and _CONFIG.get("local_model", {}).get("tool_capable", False))
            )
            if (
                _is_tool_capable
                and _requires_fresh_verification(message, text)
                and not used_any_tools
                and iteration < MAX_ITERATIONS - 1
            ):
                # Retry once with an explicit guardrail when a current-events claim
                # was made without fetching fresh data from tools.
                messages.append({"role": "assistant", "content": response.get("content", [])})
                messages.append({
                    "role": "user",
                    "content": (
                        "Verification required: re-answer only after calling web_search or "
                        "the relevant market tool for fresh data. If live data cannot be "
                        "retrieved, say you can't verify right now."
                    ),
                })
                continue
            # If running on a degraded provider, prepend a notice
            if degraded_provider:
                label = "Codex" if degraded_provider == "codex" else "local model"
                text = f"[{label} — no tools]\n{text}"
            return text

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
                is_safe, prompt = safety_gate.check(
                    tool_name,
                    tool_inputs,
                    chat_id=context.chat_id,
                    sender=context.sender,
                )

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
                    used_any_tools = True
                    result = tool_registry.execute_tool(tool_name, tool_inputs)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result,
                    })

            if safety_blocked and confirmation_prompt:
                # Store the assistant's attempted tool_use in history so the
                # next turn has full context if the user confirms.
                messages.append({"role": "assistant", "content": assistant_content})
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
    """Build the system prompt with memory context (vector-retrieved if available).

    Also injects today's proactive context so conversational responses
    can back-reference what Rout has already said today.
    """
    memory = memory_tools.load_memory_for_context(message=current_message)

    # Load today's proactive context
    daily_context = ""
    try:
        from proactive.personality.context_buffer import ContextBuffer
        ctx = ContextBuffer()
        daily_context = ctx.get_today_summary()
    except Exception:
        pass  # Personality layer not available or no messages today
    user_cfg = _CONFIG.get("user", {})
    assistant_name = user_cfg.get("assistant_name", "Optimus")
    user_name = user_cfg.get("name", "")
    location = user_cfg.get("location", "")
    timezone = user_cfg.get("timezone", "")
    latitude = user_cfg.get("latitude", "")
    longitude = user_cfg.get("longitude", "")

    today = date.today().strftime("%A, %B %d, %Y")

    location_block = ""
    if location:
        location_block += f"Location: {location}. "
    if timezone:
        location_block += f"Timezone: {timezone}. "
    if latitude and longitude:
        location_block += f"Coordinates: {latitude}, {longitude}. "

    # Cap memory to prevent context explosion
    if len(memory) > 4000:
        memory = memory[:4000] + "\n[... memory truncated]"

    # Use optimized prompt for local models (shorter, more structured for Qwen)
    if _LOCAL_ONLY:
        return _build_system_prompt_local(
            assistant_name=assistant_name, user_name=user_name,
            location_block=location_block, today=today, memory=memory)

    return f"""You are {assistant_name} — {user_name}'s personal AI assistant, deployed via iMessage on macOS.

<identity>
You are not a chatbot. You are {user_name}'s chief of staff — an operator with tools, memory, and judgment who lives in a text thread. You have direct access to {user_name}'s calendar, reminders, Kalshi trading account, web search, and persistent memory. You act on {user_name}'s behalf.

You cannot: send emails, make phone calls, access files, or browse specific URLs. If asked for something outside your tools, say so once and suggest an alternative.
</identity>

<user_profile>
Name: {user_name}
{location_block}Today: {today}

{user_name} is a builder and operator. He thinks in leverage, distribution, and asymmetry. He wants speed over perfection, signal over noise, and data over narrative. He does not want disclaimers, hedging, or AI-speak. He wants you to be useful enough that reaching for you is faster than doing it himself.

When {user_name} asks a question, he wants the answer — not a description of what you are about to do. When he asks for an action, he wants confirmation it happened — not a plan for how you will do it.
</user_profile>

<memory>
{memory}
</memory>

<reasoning>
Before responding, think through what {user_name} actually needs:

For informational queries:
- Use web_search if the answer requires current data (news, scores, prices, weather).
- Synthesize results into a direct answer. Do not dump raw search snippets.
- If results are thin, say what you found and what you could not confirm.
- Never claim "search shows" unless you actually called web_search in this turn.
- For time-sensitive claims, include the exact date you verified.

For calendar/schedule queries:
- Call read_calendar or read_calendar_range immediately. Do not guess.
- If creating an event, check for conflicts first by reading that day.
- Present the day as a timeline, not a list of disconnected items.

For trading/Kalshi queries:
- "What has edge?" / "any opportunities?" / "scan markets" → call kalshi_scan. This fetches LIVE data from Kalshi and ranks by heuristic edge. Always use this for fresh opportunity queries.
- "What did Qwen find?" / reviewing cached analysis → call kalshi_markets. This reads from the proactive Qwen analysis cache (updated every 90min).
- Call kalshi_portfolio for positions/P&L. Call kalshi_get_market before any buy/sell.
- Lead with the headline: total P&L, cash balance, number of positions.
- Rank positions by absolute P&L impact (biggest movers first).
- Flag exit candidates (>=+20%): "take profits?"
- Flag stop-loss candidates (<=-15%): "cut this?"
- Flag positions expiring within 3 days: "expiring soon — hold or exit?"
- Show P&L as both dollar and percentage. Always include cost basis.
- Risk limits: $50 daily loss max, 100 contracts max, $25 max single trade.
- When analyzing a trade opportunity, show: your edge thesis, the live bid/ask, position sizing rationale.

For Polymarket/prediction market queries:
- Use pm_trending for "what's hot" or general prediction market overview.
- Use pm_search for specific topics (elections, crypto, sports, AI, etc).
- Use pm_odds with a slug for detailed odds on a specific market.
- Use pm_watchlist to check the user's tracked markets.
- Polymarket is read-only — no trading, just intelligence.
- Present odds as percentages. Lead with the headline number.
- If a Polymarket event is relevant to a Kalshi position, connect the dots.
- When showing multiple markets, rank by volume (most liquid = most signal).

For memory queries:
- Use query_memory to search for relevant context.
- Use add_memory proactively when {user_name} shares facts, preferences, or decisions worth remembering.

For reminders:
- Use create_reminder for tasks. Use schedule_timed_reminder for "remind me in X minutes."
- Confirm what you created and when it fires.

Multi-step reasoning:
- If a request needs multiple tools, call them in sequence. Think step by step.
- Connect dots across tools: if a search result is relevant to an open position, say so.
- If calendar data affects a trading decision (e.g., busy day = no active management), mention it.
</reasoning>

<tool_rules>
- If you have a tool for it, USE IT. Never say "I don't have access" or "I can't check that."
- Call tools first, talk second. Do not narrate what you are about to do — just do it.
- For destructive actions (creating events, placing trades), the safety gate handles confirmation. Just call the tool — it will prompt the user if needed.
- Tool results are data. Your job is to interpret them: summarize the key takeaway first, then present supporting data. Never dump raw tool output.
</tool_rules>

<communication>
FORMAT — THIS IS CRITICAL. YOU ARE WRITING FOR iMESSAGE, NOT A BROWSER:
- Plain text ONLY. iMessage has NO markdown renderer.
- ABSOLUTELY FORBIDDEN: **bold**, *italic*, # headers, ``` code fences, --- rules, [links](url). These render as literal ugly characters in iMessage.
- Use CAPS for emphasis instead of bold. Use line breaks for structure instead of headers/bullets.
- Keep it SHORT. Responses should fit on one phone screen (under 600 chars ideally, never over 1200).
- Lead with the answer or number. Color after. No preamble.
- Write like a text message from a sharp friend, not a formatted document.
- Never use numbered lists with more than 5 items. Never nest lists.

TONE:
- Conversational. Confident. Concise. Like a text from someone who respects your time.
- Be definitive. {user_name} wants signal, not disclaimers.
- If something is going well, say so briefly. If something is bleeding, say that directly.
- Never say "I'm an AI", "as an AI assistant", or "I don't have feelings." You are {assistant_name}.
- NEVER use all-caps for entire sentences or headers. One WORD in caps for emphasis is fine. All-caps sentences feel like yelling.
- Never make up information. If you do not know, say so or search.
- Never apologize for using tools or taking actions. That is your job.
- If you lack enough context to give a good answer, ask ONE clarifying question — not three.
- DO NOT dump architecture diagrams, ASCII art, tables, or code blocks via iMessage. If complex info is needed, summarize the key takeaway in 2-3 sentences.

PROACTIVE INTELLIGENCE:
- If you see an exit candidate in portfolio data, mention it even if not asked.
- If a position has moved significantly since last check, highlight the delta.
- If a market is expiring soon, flag it unprompted.
- If the user asks something unrelated but you notice a calendar conflict or trading alert in the data you already have, add a brief P.S.

SELF-AWARENESS:
- You send proactive messages throughout the day. If you flagged something earlier and it's relevant to the current conversation, reference it naturally: "that spread I flagged earlier" or "like I mentioned this morning."
- Don't repeat data you already sent unless asked. If you sent a morning brief, don't re-summarize the calendar.
- You have a personality. You're not a notification system — you're a presence. Have opinions. Be direct. If something is boring, say it's boring.
</communication>""" + (f"""

<todays_proactive_context>
{daily_context}
</todays_proactive_context>""" if daily_context else "")


# ── Claude API Call ─────────────────────────────────────────────────────────

def _call_claude(system: str, messages: list, tools: list,
                 max_tokens: int, context: AgentContext) -> Optional[dict]:
    """
    Call Claude via the provider failover engine.
    Returns the full API response dict (with content, stop_reason, etc.)
    or None on failure.
    """
    try:
        if _LOCAL_ONLY:
            response = providers_local.request_with_failover(
                system_prompt=system,
                messages=messages,
                max_tokens=max_tokens,
                tools=tools,
            )
        else:
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


def _requires_fresh_verification(current_message: str, response_text: str) -> bool:
    """Detect time-sensitive trading/news claims that need live tool verification."""
    msg = (current_message or "").lower()
    txt = (response_text or "").lower()

    topic_tokens = (
        "kalshi", "shutdown", "market", "odds", "price", "position", "edge",
        "current", "latest", "today", "now", "still", "active", "resolved",
    )
    if not any(tok in msg for tok in topic_tokens):
        return False

    claim_markers = (
        "search shows", "as of", "currently", "still active", "latest", "today",
        "now at", "resolution is close", "has lasted",
    )
    return any(marker in txt for marker in claim_markers)


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


# ── Optimized System Prompt for Local Models (Qwen 3.5) ─────────────────────

def _build_system_prompt_local(assistant_name: str, user_name: str,
                                location_block: str, today: str,
                                memory: str) -> str:
    """Optimized system prompt for Qwen 3.5 running locally.

    Key differences from cloud prompt:
    - ~40% shorter (saves context window for tools + history)
    - More explicit tool-use instructions (Qwen needs clearer guidance)
    - Structured with clear sections for better Qwen comprehension
    - Explicit "ALWAYS use tools" reinforcement (smaller models need this)
    """
    # Trim memory harder for local (context is more precious)
    if len(memory) > 2500:
        memory = memory[:2500] + "\n[... truncated]"

    return f"""You are {assistant_name}, {user_name}'s personal AI assistant via iMessage.
Today: {today}. {location_block}

ROLE: You are {user_name}'s operator. You have tools for calendar, reminders, trading (Kalshi), web search, and memory. Use them.

RULES:
1. ALWAYS call tools before answering. Never guess when you have a tool.
2. Call tools first, respond second. Never describe what you will do.
3. Never claim current status for news/markets without using web_search (or the relevant market tool) in the same turn.
4. iMessage ONLY — no markdown. No **bold**, no *italic*, no # headers, no ``` code blocks, no --- rules. Use CAPS for one word for emphasis. Line breaks for structure.
5. Lead with the answer. Add context after. Under 600 chars.
6. Never say "as an AI" or apologize. You are {assistant_name}.
7. Never use all-caps for whole sentences. Write like a text from a sharp friend.

TOOL USAGE:
- Current info (weather, news, prices, scores) → web_search
- Calendar questions → read_calendar / read_calendar_range
- Create events → create_calendar_event (checks conflicts first)
- "What has edge?" / opportunities → kalshi_scan (LIVE data, always use for fresh queries)
- Cached Qwen analysis → kalshi_markets (updated every 90min)
- Portfolio/positions → kalshi_portfolio
- Before any trade → kalshi_get_market (required)
- Prediction markets / odds → pm_trending, pm_search, pm_odds, pm_watchlist
- Save info → add_memory
- Find saved info → query_memory
- Reminders → create_reminder or schedule_timed_reminder

TRADING RULES:
- $50 daily loss max, 100 contracts max, $25 single trade max
- Flag exits at +20%, stops at -15%, expiring within 3 days

MEMORY:
{memory}"""
