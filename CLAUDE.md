# CLAUDE.md — Project Intelligence for Rout

> This file is read by AI agents (Claude Code, Codex, etc.) before working on the codebase.
> It encodes architecture decisions, conventions, and constraints that prevent wasted effort.

## What Is Rout

Rout is a personal AI agent accessible via iMessage. It reads your calendar, creates events, manages reminders, searches the web, and remembers context about you — all through natural conversation in Messages.app.

Built on [OpenClaw](https://openclaw.ai). Runs as a launchd service on macOS.

## Architecture (v2 — Post-Rebuild)

```
iMessage → imsg CLI (polls SQLite) → imsg_watcher.py → command dispatch
                                                          │
                                          ┌───────────────┤
                                          │               │
                                    Structured        Free-form text
                                    commands              │
                                      │                   ▼
                                      │           agent_loop.py
                                      │               │
                                      │         ┌─────┼─────┐
                                      │         │     │     │
                                      │      Claude  Tool   Safety
                                      │       API    Use    Gate
                                      │              │
                                      │         ┌────┼────┐
                                      │         │    │    │
                                      │      calendar reminders memory
                                      │      read/write  create  query/add
                                      │
                                 ┌────┼────┐
                                 │    │    │
                              help status memory:view
                              ping doctor memory:add
```

### Key Runtime Files

| File | Role | Notes |
|---|---|---|
| `comms/imsg_watcher.py` | Main polling loop, message dispatch, send path | The beating heart. Infrastructure only. |
| `agent/agent_loop.py` | Multi-turn tool-use loop | The brain. All intelligence flows here. |
| `agent/safety_gate.py` | Confirmation flow for destructive actions | Blocks calendar writes, reminder creation until user confirms. |
| `agent/tool_registry.py` | Tool definitions + dispatch | Central registry mapping tool names to executors + safety levels. |
| `agent/providers.py` | Provider failover engine | Anthropic → Codex → Ollama with cooldown state. |
| `agent/tools/calendar_tools.py` | Calendar.app read/write via AppleScript | |
| `agent/tools/reminder_tools.py` | Reminders.app via AppleScript | |
| `agent/tools/memory_tools.py` | Memory query/add (vector + MEMORY.md fallback) | |
| `agent/tools/search_tools.py` | DuckDuckGo web search | |
| `handlers/general_handlers.py` | Thin wrapper — routes to agent_loop | Was 1,939 lines. Now ~210. |
| `handlers/core_handlers.py` | help, status, ping, doctor | Low risk |
| `handlers/memory_handlers.py` | Memory commands (view, add, clear) | |
| `memory/vector_store.py` | ChromaDB + Ollama embeddings | Graceful fallback to MEMORY.md |
| `memory/memory_migrator.py` | MEMORY.md → ChromaDB migration | Safe to re-run |
| `scripts/proactive_agent.py` | Cron-driven outbound (morning briefing, meeting reminders) | |
| `imsg_commands.yaml` | Command registry — maps triggers to handlers | |
| `config.yaml` | User config (gitignored) | Never commit |

### How It Works

1. User texts a message via iMessage
2. `imsg_watcher.py` polls via `imsg history --chat-id <id> --limit 20 --json`
3. Message ID dedup prevents reprocessing
4. `parse_command()` checks against `imsg_commands.yaml` registry
5. Match → call registered handler function
6. No match → fall through to `general_handlers.claude_command`
7. `claude_command` checks for pending safety confirmations first
8. If no confirmation pending → `agent_loop.run()`:
   - Loads chat history + memory context
   - Calls Claude API with tool definitions
   - Claude can call tools (read calendar, create events, search web, etc.)
   - Safety gate intercepts destructive tools → asks user to confirm
   - Loop continues until Claude returns text (max 5 iterations)
9. Response sent via osascript (primary) with imsg CLI fallback

### Agent Loop Design

The agent loop replaces the old single-shot Q&A with an iterative tool-use engine:

```
User message → build context (memory + history) → Claude API call with tools
  → LOOP:
      tool_use response → safety gate check → execute tool → append result → call again
      text response → return to user
  → Max 5 iterations
```

Key decisions:
- **Max 5 iterations** — prevents runaway loops. Real tasks complete in 2-3.
- **Tool results capped at 2000 chars** — prevents context explosion.
- **Safety gate** — `create_calendar_event` and `create_reminder` always require "yes/no".
- **Memory injected per-query** — vector-retrieved relevant context, not full dump.

### Safety Gate

Destructive actions require user confirmation:

```
Claude calls create_calendar_event → safety gate stores pending action
  → Returns confirmation prompt: "Create 'Dentist' on Thu at 2 PM? Reply yes/no."
  → User texts "yes" → execute pending action
  → User texts "no" → discard
  → 1 hour expiry on pending actions
```

State: `~/.openclaw/state/pending_action.json` — one pending action at a time.

### Provider Failover

Automatic switching: Anthropic → Codex → Ollama

```
Try Anthropic → rate limited? → cooldown + try Codex
Codex fails? → try local Ollama
All down? → friendly error message
```

State: `~/.openclaw/state/provider_failover.json`
Auth: `~/.openclaw/agents/main/agent/auth-profiles.json`

### Vector Memory

Optional upgrade over MEMORY.md:
- **ChromaDB** — local, file-based vector store
- **Ollama embeddings** — `nomic-embed-text` model
- **Graceful degradation** — if ChromaDB or Ollama unavailable, falls back to MEMORY.md
- **Migration** — `python -m memory.memory_migrator`

## Conventions

- **Python 3.10+** — f-strings, type hints, dataclasses
- **No pip requirements.txt** — stdlib + optional ChromaDB. All tool executors use AppleScript or HTTP.
- **Flat imports** — `from agent.tools.calendar_tools import read_calendar`
- **Tools return strings** — every tool function takes kwargs and returns a string result
- **Errors in brackets** — `[Tool error (name): message]`
- **Config** — `config.yaml` (user, gitignored) + `imsg_commands.yaml` (committed)
- **State** — `~/.openclaw/state/` for runtime state files
- **Logs** — `~/.openclaw/logs/` for audit + runtime logs

## What Not To Do

- Don't put business logic in `imsg_watcher.py` — it's infrastructure
- Don't add new keyword lists in general_handlers — use tool definitions in `tool_registry.py`
- Don't bypass the safety gate for destructive actions
- Don't commit `config.yaml` or any file with API keys/tokens
- Don't import from `handlers/` within `agent/` — dependency flows one way: handlers → agent
- Don't use `imsg_watcher.py` to send messages directly — always go through the handler's `_send_to_chat`
