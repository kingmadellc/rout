# Rout — iMessage AI Assistant

An iMessage bot powered by Claude that responds to natural language and structured commands. Runs on your Mac, texts back through Messages.app.

## What It Does

- **Conversation** — text anything, get a Claude response with chat history context
- **Weather** — real-time local weather via Open-Meteo (auto-geocoded to your city)
- **Image analysis** — send a photo, get a description
- **Calendar** — read events or add new ones ("add dentist Friday at 2pm")
- **Reminders** — "remind me to call the school"
- **Web search** — auto-triggered on news/current events questions
- **Kalshi trading** (optional) — portfolio, positions, market search
- **Crash recovery** — auto-restarts via launchd, texts you if it goes down

## Requirements

- macOS (uses Messages.app, Calendar.app, Reminders.app via osascript)
- [imsg CLI](https://github.com/nicholasstephan/imsg) (`brew install imsg`)
- Python 3.9+
- Anthropic API key ([get one here](https://console.anthropic.com/settings/keys))

## Quick Start

```bash
git clone https://github.com/kingmadellc/rout.git ~/.openclaw/hardened
cd ~/.openclaw/hardened
python3 setup.py
```

The setup wizard handles everything:

- Installs Python dependencies (`pyyaml`, `requests`)
- Collects your name, phone number, and location
- Geocodes your city for accurate weather
- Lets you pick a personality (casual, professional, or minimal)
- Finds your iMessage chat ID
- Validates your Anthropic API key with a live test
- Creates `~/.openclaw/config.yaml` (chmod 600, never committed)
- Creates `~/.openclaw/MEMORY.md` for persistent personal context

Then start the watcher:

```bash
python3 comms/imsg_watcher.py
```

Text yourself `ping` — you should get "Pong!" back within a few seconds.

## Commands

| Command | What it does |
|---|---|
| `ping` | Connectivity test |
| `help` | List all commands |
| `status` | Watcher status |
| `kalshi: portfolio` | Balance + positions |
| `kalshi: positions` | Detailed breakdown |
| `kalshi: markets [query]` | Search markets |
| *(anything else)* | Routed to Claude |

Weather is automatic — ask "what's the weather?" and it returns real conditions for your configured location.

## Configuration

All config lives in `~/.openclaw/config.yaml` (created by `setup.py`). Key sections:

- `user` — name, location, lat/lon, personality
- `chats` — which iMessage chat IDs to monitor
- `chat_handles` — phone number mappings for sending replies
- `anthropic_api_key` — your Claude API key
- `kalshi` — optional trading integration

To reconfigure, re-run `python3 setup.py` — it preserves existing values as defaults.

## Adding Commands

1. Create `handlers/mybot_handlers.py` with functions ending in `_command`
2. Register in `imsg_commands.yaml`
3. Restart the watcher

See `handlers/example_handlers.py` for the pattern.

## Personalizing Responses

Edit `~/.openclaw/MEMORY.md` to give Rout persistent context about you — family, preferences, projects, anything you want it to always know. This is included in every Claude conversation.

## Running as a Background Service

Use launchd to auto-start and auto-restart:

```bash
cp launchd/com.rout.imsg-watcher.plist ~/Library/LaunchAgents/
# Edit the plist to set your paths
launchctl load ~/Library/LaunchAgents/com.rout.imsg-watcher.plist
```

If Rout crashes, launchd restarts it automatically and it'll text you a heads-up.

## File Structure

```
setup.py                    Interactive setup wizard
comms/imsg_watcher.py       Main polling loop + message dispatch
handlers/
  core_handlers.py          help, status, ping
  general_handlers.py       Claude routing, weather, calendar, reminders, search
  kalshi_handlers.py        Trading commands
  example_handlers.py       Template for adding new commands
config/
  credentials.py            Credential loader (permission-checked)
trading/
  kalshi_client.py          Kalshi API client with risk limits
config.yaml.example         Config template (reference only — use setup.py)
imsg_commands.yaml          Command registry
start_watcher.sh            Start watcher
stop_watcher.sh             Stop watcher
rout-imsg-watcher           Wrapper script for launchd
launchd/                    Plist templates
```

## Logs

```bash
tail -f ~/.openclaw/logs/imsg_watcher.log
```

Structured audit logs: `~/.openclaw/logs/imsg_audit.jsonl`

## Architecture

```
iMessage → imsg CLI (polling) → imsg_watcher.py
                                  ├─ Weather keywords → Open-Meteo API
                                  ├─ Structured commands → handlers/*.py
                                  └─ Free-form text → Claude API → iMessage reply (osascript)
```
