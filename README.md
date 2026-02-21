<p align="center">
  <img src=".github/rout-logo.png" alt="Rout" width="120" />
</p>

<h1 align="center">Rout</h1>

<p align="center">
  <strong>Your AI assistant, native to iMessage.</strong><br>
  Text it like a friend. It texts back with Claude.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#what-it-does">Features</a> ·
  <a href="#adding-commands">Extend It</a> ·
  <a href="#architecture">Architecture</a>
</p>

---

Rout turns iMessage into a personal AI interface. It runs on your Mac, watches for incoming texts, and responds using Claude — with full conversation memory, native macOS integrations, and a plugin system for adding your own capabilities.

No app to install. No new interface to learn. Just text.

## Quick Start

```bash
git clone https://github.com/kingmadellc/rout.git
cd rout
python3 setup.py
```

The setup wizard walks you through everything in ~2 minutes:

- Installs dependencies
- Collects your name, phone, and city (auto-geocoded for weather)
- Discovers your iMessage conversations automatically
- Lets you pick a personality — casual, professional, or minimal
- Validates your Anthropic API key with a live test
- Writes a secure config (`chmod 600`, never committed)

Then start it:

```bash
python3 comms/imsg_watcher.py
```

Text yourself **ping** — you should get "Pong!" back in seconds.

## What It Does

**Conversation** — text anything, get a Claude response with chat history and personal memory context.

**Weather** — ask "what's the weather?" and get real conditions for your city, powered by Open-Meteo. No API key needed.

**Calendar** — "what's on my calendar this week?" reads your events. "Add dentist Friday at 2pm" creates one. Uses Calendar.app directly via AppleScript.

**Reminders** — "remind me to call the school" creates a native Reminder.

**Image analysis** — send a photo, get a Claude vision description.

**Web search** — auto-triggered on news and current events questions via DuckDuckGo.

**Personal memory** — edit `MEMORY.md` with anything you want Rout to always know about you — family, preferences, projects. It's included in every conversation.

**Crash recovery** — runs as a launchd service. If it goes down, it restarts automatically and texts you a heads-up.

## Requirements

| Requirement | Notes |
|---|---|
| **macOS** | Uses Messages.app, Calendar.app, Reminders.app via osascript |
| **Python 3.9+** | Pre-installed on modern macOS |
| **[imsg CLI](https://github.com/nicholasstephan/imsg)** | `brew install imsg` |
| **Anthropic API key** | [Get one here](https://console.anthropic.com/settings/keys) |

## Commands

| Command | What it does |
|---|---|
| `ping` | Connectivity test |
| `help` | List all commands |
| `status` | Watcher + system health check |
| *(anything else)* | Routed to Claude with full context |

Everything that isn't a registered command goes to Claude — including follow-up questions, image analysis, calendar requests, and weather. Rout detects intent automatically.

## Personalizing Rout

### Personality

During setup, choose how Rout talks to you:

- **Casual** — short, witty, straight talk (default)
- **Professional** — polished but concise
- **Minimal** — terse, just the facts

### Memory

Edit `MEMORY.md` to give Rout persistent context:

```markdown
## About Me
- Name: Matt
- Location: Portland, OR
- Work: Building AI products

## Family
- Dog named Pixel, loves walks at 7am

## Preferences
- Coffee over tea, always
- Prefer metric for weather? No, Fahrenheit
```

This file is loaded into every conversation. Rout will reference it naturally.

## Adding Commands

Rout has a plugin system. Add new capabilities in three steps:

1. **Create a handler** — add `handlers/mybot_handlers.py` with functions ending in `_command`
2. **Register it** — add an entry in `imsg_commands.yaml`
3. **Restart** — the watcher picks it up automatically

Example handler:

```python
def hello_command(args: str = "") -> str:
    """Say hello"""
    name = args.strip() if args.strip() else "friend"
    return f"Hello, {name}!"
```

Register in `imsg_commands.yaml`:

```yaml
mybot:hello:
  trigger: "mybot: hello"
  description: "Say hello"
  handler: "mybot_handlers.hello_command"
```

See `handlers/example_handlers.py` for the full pattern.

### Optional: Kalshi Trading

Rout includes an optional integration with [Kalshi](https://kalshi.com) prediction markets — portfolio tracking, position monitoring, and market search via iMessage. Enable it during setup or in `config.yaml`. Includes client-level risk limits, trade audit logging, and an hourly exit monitor with P&L alerts.

## Running as a Background Service

For always-on operation, use macOS launchd:

```bash
cp launchd/com.rout.imsg-watcher.plist ~/Library/LaunchAgents/
# Edit the plist to set your paths
launchctl load ~/Library/LaunchAgents/com.rout.imsg-watcher.plist
```

Rout will auto-start on login, auto-restart on crash, and text you if it goes down.

## Configuration

All config lives in `~/.openclaw/config.yaml` (created by the setup wizard, `chmod 600`). Key sections:

| Section | What it controls |
|---|---|
| `user` | Name, location, coordinates, personality |
| `chats` | Which iMessage conversations to monitor |
| `chat_handles` | Phone number / Apple ID mappings for replies |
| `anthropic_api_key` | Your Claude API key |
| `kalshi` | Optional trading integration |

To reconfigure, re-run `python3 setup.py` — existing values become defaults.

## Architecture

```
iMessage → imsg CLI (polling) → imsg_watcher.py
                                  ├─ Weather keywords    → Open-Meteo API
                                  ├─ Calendar/Reminders  → AppleScript → native apps
                                  ├─ Registered commands → handlers/*.py
                                  └─ Everything else     → Claude API → iMessage reply
```

## File Structure

```
setup.py                    Interactive setup wizard
comms/imsg_watcher.py       Main polling loop + message dispatch
handlers/
  core_handlers.py          help, status, ping
  general_handlers.py       Claude routing, weather, calendar, reminders, search
  kalshi_handlers.py        Trading commands (optional)
  example_handlers.py       Template for adding new commands
config/
  credentials.py            Credential manager (permission-checked)
trading/
  kalshi_client.py          Kalshi API client with risk limits
config.yaml.example         Config template
imsg_commands.yaml          Command registry
MEMORY.md.example           Personal memory template
launchd/                    macOS service plists
```

## Logs

```bash
# Watcher log
tail -f ~/.openclaw/logs/imsg_watcher.log

# Structured audit log
cat ~/.openclaw/logs/imsg_audit.jsonl
```

## Security

- Config file is `chmod 600` — readable only by you
- API keys are never committed (`.gitignore`)
- Private keys stored in `~/.openclaw/keys/` with strict permissions
- AppleScript inputs are sanitized to prevent injection
- Circuit breaker prevents runaway message loops (exponential backoff)

---

<p align="center">
  Built by <a href="https://github.com/kingmadellc">KingMade</a>
</p>
