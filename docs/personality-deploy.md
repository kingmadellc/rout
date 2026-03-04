# Personality Layer — Deploy Guide

## What Changed

Six new files in `scripts/proactive/personality/`:
- `__init__.py` — package root
- `context_buffer.py` — daily memory, back-references
- `editorial_voice.py` — opinion layer for proactive messages
- `variable_timing.py` — urgency-based send/hold decisions
- `selective_silence.py` — content quality gate + deliberate silence
- `micro_initiations.py` — ambient awareness pings
- `response_tracker.py` — engagement pattern tracking
- `engine.py` — orchestrator that wires everything together

Modified files:
- `scripts/proactive/__init__.py` — personality init + micro-initiation hooks
- `config/proactive_triggers.yaml` — personality config section added
- `agent/agent_loop.py` — daily context injected into system prompt
- `comms/imsg_watcher.py` — response tracker hook on inbound messages

New state files (auto-created on first run):
- `~/.openclaw/state/daily_context.json` — today's outbound message buffer
- `~/.openclaw/state/response_tracker.json` — engagement history

## Deploy Steps

### 1. Pull the code
```bash
cd ~/.openclaw/workspace/rout && git pull
```

### 2. Verify the new files exist
```bash
ls scripts/proactive/personality/
```
Should show: `__init__.py  context_buffer.py  editorial_voice.py  engine.py  micro_initiations.py  response_tracker.py  selective_silence.py  variable_timing.py`

### 3. Test with dry run
```bash
cd ~/.openclaw/workspace/rout && /opt/homebrew/bin/python3 scripts/proactive_agent.py --dry-run
```
Look for `[personality]` log lines. Should see:
- `[personality] Initialized — editorial voice, context buffer, timing active`
- `[personality] <trigger>: urgency=X.XX x engagement_mod=X.XX = X.XX`

### 4. Test a single trigger with personality
```bash
cd ~/.openclaw/workspace/rout && /opt/homebrew/bin/python3 scripts/proactive_agent.py --only edge --dry-run
```

### 5. Restart the proactive agent service
```bash
launchctl kickstart -k gui/$(id -u)/com.rout.proactive-agent
```

### 6. Restart the watcher (for response tracking hook)
```bash
launchctl kickstart -k gui/$(id -u)/com.rout.imsg-watcher
```

### 7. Verify services are running
```bash
launchctl list | grep rout
```

### 8. Watch the logs
```bash
tail -f ~/.openclaw/logs/proactive_agent.log | grep personality
```

## Config

All personality features are independently toggleable in `config/proactive_triggers.yaml`:

```yaml
personality:
  enabled: true
  editorial_voice: true
  context_buffer: true
  variable_timing: true
  selective_silence: true
  micro_initiations: true
  response_tracking: true
```

Set `personality.enabled: false` to completely disable — reverts to raw trigger output.

## Rollback

If anything goes wrong:
```bash
cd ~/.openclaw/workspace/rout && git checkout HEAD~1 -- scripts/proactive/__init__.py agent/agent_loop.py comms/imsg_watcher.py config/proactive_triggers.yaml
```
Then restart services:
```bash
launchctl kickstart -k gui/$(id -u)/com.rout.proactive-agent && launchctl kickstart -k gui/$(id -u)/com.rout.imsg-watcher
```

Or just disable in config (faster):
Edit `config/proactive_triggers.yaml`, set `personality.enabled: false`, and restart proactive agent.
