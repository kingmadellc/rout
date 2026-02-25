# Rout Setup Notes (Working Configuration)

## Full Disk Access Requirement

**CRITICAL:** Python needs Full Disk Access to read the Messages database.

### Grant Access:
1. System Settings → Privacy & Security → Full Disk Access
2. Click `+` button
3. Add: `/opt/homebrew/bin/python3` (or your Python path)
4. Restart terminal/launchd service

### Why:
- `imsg history` reads from `~/Library/Messages/chat.db`
- macOS blocks this without Full Disk Access

## Config Setup

Rout reads from `~/.openclaw/config.yaml` (see `config.yaml.working-example`)

**Required sections:**
```yaml
chats:
  personal_id: 1
  group_ids: [2]  # add group chat IDs here

known_senders:
  "+1XXXXXXXXXX": "YourName"
  "+1XXXXXXXXXX": "PartnerName"

paths:
  imsg: "/opt/homebrew/bin/imsg"
  python: "/opt/homebrew/bin/python3"

user:
  name: "YourName"
  assistant_name: "Rout"

anthropic:
  model: "claude-sonnet-4-5"
  max_tokens: 512
```

## Authentication

Rout uses **OpenClaw's OAuth token** (not a static API key).

Token read from: `~/.openclaw/agents/main/agent/auth-profiles.json`

No config needed — automatically detected if OpenClaw is installed.

## Testing

```bash
# Check FDA
imsg history --chat-id 1 --limit 3

# Should return messages. If permission error → grant Full Disk Access

# Start watcher
launchctl load ~/Library/LaunchAgents/com.rout.imsg-watcher.plist

# Check logs
tail -f ~/.openclaw/logs/launchd-imsg-watcher.log
```

## Known Issues

- iMessage sync lag: phone → Mac can take 10-60s
- Watcher won't see new messages until they sync to Messages.app database
- Not a bot issue — purely iCloud sync timing
