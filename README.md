# Rout

Rout turns iMessage into a personal AI interface running on your Mac.
It watches configured chats, routes commands, and sends responses back through Messages.

## Quick Start

```bash
git clone https://github.com/kingmadellc/rout.git
cd rout
./setup.sh
```

`setup.sh` does two things:
1. Runs the interactive setup wizard (`setup.py`)
2. Installs launchd plist files in `~/Library/LaunchAgents/`

Then start the watcher:

```bash
./start_watcher.sh
```

Or run foreground mode:

```bash
python3 comms/imsg_watcher.py
```

## What Setup Configures

Setup writes and secures:
- `~/.openclaw/config.yaml` (chmod 600)
- `~/.openclaw/MEMORY.md`
- `~/.openclaw/logs/`
- `~/.openclaw/state/`
- `~/.openclaw/keys/`

It also collects:
- Name, phone number, location
- Coordinates and timezone from Open-Meteo geocoding
- Personality (`casual`, `professional`, `minimal`)
- Chat IDs and reply handles
- Anthropic API key
- Optional Kalshi keys

## Commands

| Command | Description |
|---|---|
| `ping` | Connectivity test |
| `help` | List registered commands |
| `status` | Runtime status snapshot |
| `doctor` | Installation/runtime diagnostics |
| `memory: view` | Show memory file (trimmed) |
| `memory: add <note>` | Append a durable note to memory |
| `memory: clear CONFIRM` | Reset memory file |
| `kalshi: portfolio` | Portfolio summary (optional) |
| `kalshi: positions` | Position details (optional) |
| `kalshi: markets <query>` | Market search (optional) |
| `kalshi: cache` | Cache freshness (optional) |
| any other text | Routed to Claude |

## Features

- Multi-turn Claude chat with personal memory context
- Weather via Open-Meteo
- Calendar read/write via AppleScript
- Reminder creation via AppleScript
- Image attachment analysis (Claude vision)
- Web search fallback via DuckDuckGo instant answer API
- Structured JSONL audit log
- Circuit breaker for runaway send loops

## Background Service (launchd)

Installed by `./setup.sh`:
- `~/Library/LaunchAgents/com.rout.imsg-watcher.plist`
- `~/Library/LaunchAgents/com.rout.kalshi-monitor.plist`

Manage watcher:

```bash
./start_watcher.sh
./stop_watcher.sh
```

## Configuration

Primary config file: `~/.openclaw/config.yaml`

Key sections:
- `user`: profile, location, timezone, personality
- `chats`: personal/group chat IDs
- `chat_handles`: chat_id -> `[handle, type]`
- `known_senders`: sender mapping for logs/context
- `anthropic_api_key`: Claude key
- `kalshi`: optional trading config
- `paths`: optional overrides for `python` and `imsg`

Example template: `config.yaml.example`

## File Layout

```text
setup.py                    Interactive setup wizard
setup.sh                    Setup + launchd plist installer
comms/imsg_watcher.py       Main watcher loop
handlers/core_handlers.py   help/status/ping/doctor
handlers/general_handlers.py Claude routing + utilities
handlers/memory_handlers.py Memory management commands
handlers/kalshi_handlers.py Optional Kalshi commands
trading/kalshi_client.py    Kalshi API client + risk limits
sdk/command_contract.py     Typed command contract helpers
docs/PLUGIN_SDK.md          Plugin development guide
kalshi_exit_monitor.py      Hourly exit monitor
imsg_commands.yaml          Command registry
launchd/*.plist             launchd templates
```

## Logs

```bash
tail -f ~/.openclaw/logs/imsg_watcher.log
tail -f ~/.openclaw/logs/imsg_audit.jsonl
```

## Requirements

- macOS (Messages, Calendar, Reminders automation)
- Python 3.9+
- [`imsg`](https://github.com/nicholasstephan/imsg): `brew install imsg`
- Anthropic API key

Optional Kalshi features may require additional Python packages depending on environment.

## Troubleshooting

Run:

```bash
python3 comms/imsg_watcher.py
```

Then text `doctor` to your configured chat for a check list.

## Security

- Config is locked to user-only permissions
- Secrets are ignored by git
- AppleScript-bound fields are sanitized
- Circuit breaker throttles outbound flood behavior
