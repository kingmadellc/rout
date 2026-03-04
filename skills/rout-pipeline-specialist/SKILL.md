---
name: rout-pipeline-specialist
description: "Expert on Rout's iMessage delivery pipeline: BlueBubbles Socket.IO transport, message routing, group chat handling, markdown stripping, and the full path from inbound message to outbound response. Use this skill when debugging message delivery issues, working on transport reliability, fixing group chat problems, modifying outbound formatting, or anything involving BlueBubbles, Socket.IO, message dispatch, or iMessage behavior. Trigger on: 'message not delivering', 'group chat broken', 'BlueBubbles issue', 'transport problem', 'formatting wrong in iMessage', 'push not working', 'Socket.IO', or any message pipeline question."
---

# Rout Message Pipeline Specialist

You are the expert on Rout's complete message delivery pipeline — from the moment an iMessage arrives to the moment a response goes out. You understand every layer, every failure mode, and every workaround that's been battle-tested.

## The Pipeline (End to End)

```
[iMessage arrives]
    ↓
[BlueBubbles server receives]
    ↓
[Socket.IO push event → imsg-watcher]    ← PRIMARY transport
    or
[Polling fallback]                         ← SUSPENDED, not deleted
    ↓
[Message parsing + dedup]
    ↓
[Command dispatch (imsg_commands.yaml)]
    or
[Claude agent dispatch (general_handlers.py)]
    ↓
[Handler executes → returns string]
    ↓
[Markdown stripping layer]
    ↓
[BlueBubbles API → send response]
    ↓
[iMessage delivered]
```

## Transport Layer: BlueBubbles Socket.IO

### How Push Transport Works

Rout connects to BlueBubbles via Socket.IO for real-time message delivery.

**Connection setup:**
- BlueBubbles server runs on Mac Mini (always-on)
- Socket.IO client in imsg-watcher connects on startup
- Auth via BB server password in config
- Reconnection is automatic with exponential backoff

**Critical fixes already shipped:**
- **sys.path fix for launchd** — When launched via launchd, Python's sys.path doesn't include the project root. The watcher prepends it on startup. If a new handler can't import, this is the first thing to check.
- **Socket.IO auth** — BB requires auth on the Socket.IO handshake, not just REST calls. The `password` param goes in the connection options.
- **Cross-transport dedup** — If both push and polling were somehow active, messages could be processed twice. Dedup uses message GUID + timestamp as the key.
- **Proof-of-life guard** — Periodic check that the Socket.IO connection is actually alive, not just "connected" in a stale state.

### Failure Modes (Transport)

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| No messages arriving at all | Socket.IO disconnected | Check `launchctl list | grep rout`, restart watcher |
| Messages arrive but delayed 30-60s | Fell back to polling | Check BB server logs, restart Socket.IO connection |
| Duplicate responses | Dedup cache cleared (restart) | Check dedup window, ensure GUID-based dedup is active |
| Connection drops every few hours | BB server memory pressure | Restart BB server, check Mac Mini resources |
| Works in DM, fails in group | Group chat routing bug | Check handler signature (see Group Chat section) |

### Polling Transport (Suspended)

Polling is NOT deleted — it's suspended. The code still exists as a fallback. If Socket.IO push is down for an extended period, polling can be reactivated by changing the transport config. But under normal operation, polling should stay off to avoid:
- 30-60s latency
- Battery/resource drain from continuous API calls
- Dedup complexity with two active transports

## Message Dispatch

### Command Routing

When a message arrives, dispatch works in this order:

1. **Exact command match** — Check `imsg_commands.yaml` for the first word(s)
2. **Keyword detection** — Check `general_handlers.py` keyword lists
3. **Claude agent fallback** — If no command or keyword matches, pass to Claude with full tool definitions

This means: a message like "cb portfolio" hits the exact command path. A message like "what's my crypto portfolio looking like?" hits keyword detection (which routes to the same handler). A message like "tell me about quantum computing" falls through to Claude.

### Handler Signature (Critical)

```python
async def handle_something(message: str, chat_guid: str = None, **kwargs) -> str:
```

**The `chat_guid` parameter is non-negotiable.** The group chat fix required that every handler accept `chat_guid` because the dispatcher (`_invoke_handler`) inspects the handler's signature to decide whether to pass it. If your handler doesn't accept `chat_guid`:
- It will work in DMs (because the dispatcher skips the param)
- It will **silently fail or route wrong** in group chats

Always include `chat_guid=None` even if your handler doesn't use it.

### **kwargs Convention

The `**kwargs` catch-all is required because the dispatcher may pass additional context in future versions (sender info, thread data, etc.). Handlers that don't accept `**kwargs` will break when new context is added.

## Group Chat Specifics

Group chat was the hardest problem in Rout's history. Key things to know:

- **chat_guid format** — DMs: `iMessage;-;+1XXXXXXXXXX` / Group: `iMessage;+;chat<hash>`
- **Handler inspection** — The dispatcher uses `inspect.signature()` to check if a handler accepts `chat_guid`. If it does, the GUID is passed. If not, it's omitted. This is why the signature matters.
- **Response routing** — Responses go to the `chat_guid` they came from. In groups, this sends to the group. If `chat_guid` is wrong or missing, the response goes to the wrong chat or nowhere.
- **@mentions** — BlueBubbles doesn't parse @mentions the same way native iMessage does. If building @-mention features, parse the raw message text yourself.

## Outbound Formatting

### Markdown Stripping Layer

iMessage doesn't render markdown. All outbound messages pass through a stripping layer:

```python
def strip_markdown(text: str) -> str:
    """Remove markdown formatting for clean iMessage display."""
    # Bold: **text** or __text__ → text
    # Italic: *text* or _text_ → text
    # Headers: # Header → Header
    # Code blocks: ```code``` → code
    # Inline code: `code` → code
    # Links: [text](url) → text (url)
    # Lists: - item → item (with indent preserved)
```

Key behaviors:
- **Links are preserved** but reformatted: `[Click here](https://url)` becomes `Click here (https://url)`
- **Bullet points** keep their indentation but lose the `-` or `*` prefix (replaced with a dash + space)
- **Code blocks** lose the triple backticks but keep the content
- **This layer runs on ALL outbound messages** — from handlers, from Claude, from morning brief. No exceptions.

If a response looks wrong in iMessage, check:
1. Is the handler returning markdown? (It shouldn't need to, since the strip layer handles it)
2. Is the strip layer mangling something? (Check edge cases with nested formatting)
3. Is iMessage doing its own thing? (Some Unicode characters render differently)

### Message Length Limits

iMessage has no hard character limit, but:
- Messages over ~2000 chars get truncated in notification previews
- Very long messages (10K+) can cause BB to fail silently
- Morning brief should stay under 1500 chars for readability
- If a response will be long, consider splitting into multiple messages with a short delay between them

## BlueBubbles API Reference

### Sending a Message

```python
import requests

def send_message(chat_guid: str, text: str, bb_url: str, bb_password: str):
    resp = requests.post(
        f"{bb_url}/api/v1/message/text",
        json={
            "chatGuid": chat_guid,
            "message": text,
            "method": "apple-script"  # or "private-api"
        },
        params={"password": bb_password},
        timeout=10
    )
    return resp.json()
```

**Method options:**
- `apple-script` — More reliable, works on all macOS versions, slight delay
- `private-api` — Faster, can do reactions/typing indicators, requires BB Private API setup

### Reading Messages

```python
def get_messages(chat_guid: str, limit: int = 25):
    resp = requests.get(
        f"{bb_url}/api/v1/chat/{chat_guid}/message",
        params={"password": bb_password, "limit": limit, "sort": "DESC"},
        timeout=10
    )
    return resp.json()["data"]
```

### Socket.IO Events

| Event | Direction | Payload | Notes |
|-------|----------|---------|-------|
| `new-message` | Server → Client | Message object | Primary trigger for inbound processing |
| `updated-message` | Server → Client | Message object | Delivery receipts, reactions |
| `typing-indicator` | Server → Client | Chat GUID + state | If using Private API |
| `group-name-change` | Server → Client | Chat GUID + name | Group metadata updates |

The `new-message` event is the only one Rout currently processes. The others are available for future features.

## Debugging Checklist

When a message pipeline issue is reported:

1. **Is the watcher running?** `launchctl list | grep rout`
2. **Is BB server up?** `curl http://localhost:1234/api/v1/ping?password=<pw>`
3. **Is Socket.IO connected?** Check watcher logs for connection status
4. **Is the message arriving at the watcher?** Add logging at the Socket.IO event handler
5. **Is the command being matched?** Check dispatch logs for the parsed command
6. **Is the handler executing?** Check for handler-level errors in logs
7. **Is the response being sent?** Check BB API response for send failures
8. **Is the response arriving in iMessage?** Check BB server logs for outbound delivery

Work through these in order. The bug is almost always in steps 2-5.
