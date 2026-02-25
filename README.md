# Rout — Personal AI Agent for macOS

Rout is a personal AI agent that lives in your iMessage. It reads your calendar, creates events, manages reminders, searches the web, and remembers context about you — all through natural conversation in Messages.app.

Built on [OpenClaw](https://openclaw.ai). Runs as a launchd service on macOS.

---

## Why Rout

Most AI assistants are chat windows you have to go to. Rout meets you where you already are — your texting app. No new apps, no browser tabs, no context switching.

- **One interface** — text it like you'd text a friend.
- **Multi-turn tool use** — Claude reasons, calls tools, observes results, and responds. Not single-shot Q&A.
- **Safety gate** — destructive actions (calendar writes, reminder creation) require your explicit confirmation.
- **Provider failover** — auto-switches Anthropic → Codex → local Ollama. Zero downtime.
- **Persistent memory** — vector-retrieved context via ChromaDB, with graceful MEMORY.md fallback.
- **Crash-proof** — launchd auto-restarts the watcher and texts you if it goes down.

## Capabilities

| Capability | What It Does |
|---|---|
| **Natural conversation** | Multi-turn agent loop with tool use, history, and memory context |
| **Calendar** | Read today's events, read a date range, or create new events via AppleScript |
| **Reminders** | Read incomplete reminders or create new ones with optional deadlines |
| **Web search** | DuckDuckGo search for news, scores, weather, current events |
| **Image analysis** | Send a photo, get a description and analysis |
| **Memory** | Query, add, or clear persistent context (vector store + markdown backup) |
| **Proactive messages** | Morning briefing and meeting reminders via cron (opt-in) |
| **Provider failover** | Anthropic → Codex → Ollama with persistent cooldown tracking |

## Quick Start

### Requirements

- Mac running macOS 12+ (always-on recommended)
- [OpenClaw](https://openclaw.ai) installed (`npm install -g openclaw`)
- Python 3.10+
- [imsg CLI](https://github.com/nicholasstephan/imsg) (`brew install imsg`)
- Claude Pro/API **or** Codex Pro+ account
- *(Optional)* [Ollama](https://ollama.com) for local LLM fallback and vector embeddings
- *(Optional)* [ChromaDB](https://www.trychroma.com/) for vector memory (`pip install chromadb`)

### Install

```bash
git clone https://github.com/kingmadellc/rout.git
cd rout
chmod +x setup.sh && ./setup.sh
```

### Configure

1. Copy `config.yaml.example` to `config.yaml` — add your chat IDs and name
2. Copy `MEMORY.md.example` to `MEMORY.md` — give your agent context about you
3. Run `imsg list --json` to find your chat ID
4. Start: `./start_watcher.sh`
5. Test: text yourself `ping`

### Update

```bash
git pull
```

Your `config.yaml` and `MEMORY.md` are gitignored — updates are code-only.

## Commands

Text these to yourself via iMessage:

| Command | Description |
|---|---|
| `ping` | Test connectivity |
| `help` | List all commands |
| `status` | Check watcher status |
| `doctor` | Run installation diagnostics |
| `memory: view` | Show persistent memory |
| `memory: add <note>` | Append to memory |
| *(anything else)* | Routed to the agent loop — Claude with tool use |

## Architecture

```
iMessage → imsg CLI → imsg_watcher.py → command dispatch
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

The watcher is handler-agnostic. All intelligence lives in `agent/`. The core loop never changes — you only add capabilities.

## File Structure

```
agent/
  agent_loop.py              Multi-turn tool-use loop (the brain)
  safety_gate.py             Confirmation flow for destructive actions
  tool_registry.py           Tool definitions, schemas, dispatch
  providers.py               Provider failover engine
  tools/
    calendar_tools.py        Calendar.app read/write via AppleScript
    reminder_tools.py        Reminders.app via AppleScript
    memory_tools.py          Memory query/add (vector + MEMORY.md)
    search_tools.py          DuckDuckGo web search
comms/
  imsg_watcher.py            Main polling loop + message dispatch
handlers/
  general_handlers.py        Thin wrapper routing to agent_loop
  core_handlers.py           help, status, ping, doctor
  memory_handlers.py         Memory view, add, clear commands
memory/
  vector_store.py            ChromaDB + Ollama embeddings
  memory_migrator.py         One-time MEMORY.md → vector migration
scripts/
  proactive_agent.py         Cron-driven morning briefing + meeting reminders
config/
  *.py                       Config loading and validation
tests/
  test_*.py                  57 unit tests (pytest)
imsg_commands.yaml           Command registry — maps triggers to handlers
config.yaml.example          Config template (copy → config.yaml)
MEMORY.md.example            Memory template (copy → MEMORY.md)
setup.sh                     One-time setup
start_watcher.sh             Start via launchd
stop_watcher.sh              Stop watcher
```

## Development

```bash
# Run tests
python -m pytest tests/ -v

# Compile check
python3 -m py_compile comms/imsg_watcher.py handlers/*.py agent/*.py agent/tools/*.py
```

## Build Your Own Tool

Every capability is a tool in the registry. Adding one takes 3 steps:

**1. Create a tool module**

```python
# agent/tools/weather_tools.py

def get_forecast(city: str = "San Francisco") -> str:
    """Get weather forecast for a city."""
    # Your logic here
    return f"Weather for {city}: 72°F, sunny"
```

**2. Register it in `agent/tool_registry.py`**

```python
"get_forecast": {
    "description": "Get weather forecast for a city",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name"},
        },
    },
    "executor": lambda **kwargs: weather_tools.get_forecast(
        city=kwargs.get("city", "San Francisco")),
    "safety": SAFE,
},
```

**3. Restart**

```bash
./stop_watcher.sh && ./start_watcher.sh
```

Claude now knows about your tool and will call it when relevant.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).

---

<p align="center">
  Built by <a href="https://github.com/kingmadellc">KingMade LLC</a> · Powered by <a href="https://openclaw.ai">OpenClaw</a>
</p>
