# Rout — Personal AI Agent for macOS

Every AI assistant makes you leave what you're doing. Open a browser. Switch apps. Lose your train of thought. Rout lives in iMessage — the app you already have open. Text it. Get answers. Stay in flow.

Built on [OpenClaw](https://openclaw.ai). Runs as a launchd service on macOS.

**[→ View the project page](https://kingmadellc.github.io/rout/)**

---

## What It Looks Like

Here's a real conversation with Rout — all happening in Messages.app:

```
You:   What's on my calendar today?
Rout:  You've got 3 things today:
       • Team standup @ 9:30 AM [Work]
       • Dentist appointment @ 1:00 PM [Personal]
       • Pick up groceries @ 5:30 PM [Reminders]

You:   Move the dentist to Thursday at 2pm
Rout:  Create event on Personal calendar?
       📅 Dentist appointment
       🕐 Thursday at 2:00 PM (1 hour)

       Reply yes or no.

You:   Yes
Rout:  ✅ Done. Event created: Dentist appointment

You:   Remind me to confirm the appointment tomorrow morning
Rout:  Create reminder in Reminders?
       📝 Confirm dentist appointment

       Reply yes or no.

You:   Yes
Rout:  ✅ Done. Added to Reminders: Confirm dentist appointment
```

No app to open. No browser tab. Just a conversation in your texting app.

---

## Why Rout

- **One interface** — text it like you'd text a friend. Calendar, reminders, web search, memory, image analysis — all through natural conversation.
- **Multi-turn reasoning** — Claude doesn't just answer questions. It reasons, calls tools, observes results, and responds. Ask it to check your schedule and create an event in the same conversation.
- **Safety gate** — destructive actions (calendar writes, reminder creation) require your explicit confirmation before executing. Nothing happens behind your back.
- **Provider failover** — auto-switches Anthropic → Codex → local Ollama if a provider is rate-limited. Zero downtime.
- **Persistent memory** — Rout remembers context about you across conversations. Vector-retrieved via ChromaDB, with graceful MEMORY.md fallback.
- **Crash-proof** — launchd auto-restarts the watcher. If it goes down, it texts you.

## Capabilities

| Capability | What It Does |
|---|---|
| **Natural conversation** | Multi-turn agent loop with tool use, conversation history, and memory context |
| **Calendar** | Read today's events, read a date range, or create new events via AppleScript |
| **Reminders** | Read incomplete reminders or create new ones with optional deadlines |
| **Timed alerts** | "Remind me in 20 minutes" — sends you an iMessage when time's up |
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

> **Note:** macOS will ask you to grant Full Disk Access to Terminal (or your terminal app) so that Rout can read your iMessage database. Go to **System Settings → Privacy & Security → Full Disk Access** and add your terminal. This is required for the watcher to function.

### Configure

1. Copy `config.yaml.example` to `config.yaml` — add your chat IDs and name
2. Copy `MEMORY.md.example` to `MEMORY.md` — give your agent context about you
3. Run `imsg list --json` to find your chat ID
4. Start: `./start_watcher.sh`
5. Test: text yourself `ping` — you should get back `🏓 Pong!`
6. Run `doctor` to verify everything is connected

### Update

```bash
git pull
```

Your `config.yaml` and `MEMORY.md` are gitignored — updates are code-only.

## Commands

Text these to yourself via iMessage:

| Command | Description |
|---|---|
| `ping` | Test connectivity — should return 🏓 Pong! |
| `help` | List all commands |
| `status` | Check watcher status, config, and logs |
| `doctor` | Run installation diagnostics |
| `memory: view` | Show persistent memory |
| `memory: add <note>` | Append to memory |
| `memory: clear CONFIRM` | Reset memory |
| *(anything else)* | Routed to Claude — natural conversation with tool use |

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
  test_*.py                  Unit tests (pytest)
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

## Privacy

Rout runs entirely on your Mac. Your messages, calendar data, and memory stay on your machine. The only external calls are to your configured AI provider (Anthropic, Codex, or Ollama) for generating responses. No telemetry, no analytics, no third-party services.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).

---

<p align="center">
  Built by <a href="https://github.com/kingmadellc">KingMade LLC</a> · Powered by <a href="https://openclaw.ai">OpenClaw</a>
</p>
