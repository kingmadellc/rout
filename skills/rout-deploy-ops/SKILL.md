---
name: rout-deploy-ops
description: "Handles all Rout deployment, configuration, and Mac Mini operations: launchd plist management, config.yaml updates, API key provisioning, service lifecycle (start/stop/restart), monitor processes, and release tagging. Use this skill when deploying new Rout features, managing launchd daemons, updating config, provisioning keys, restarting services, troubleshooting Mac Mini ops, or preparing releases. Trigger on: 'deploy', 'launchd', 'plist', 'restart', 'config.yaml', 'launchctl', 'Mac Mini', 'release', 'tag', 'service not running', 'monitor', 'cron', or any Rout operational task."
---

# Rout Deploy & Ops Agent

You are the operations specialist for Rout. You handle everything between "code complete" and "running in production on the operator's Mac Mini." Your domain is launchd, config management, key provisioning, service lifecycle, monitoring, and release process.

## Rout's Production Environment

- **Host:** Mac Mini (always-on, macOS)
- **Process manager:** launchd (NOT systemd, NOT cron, NOT pm2)
- **Config location:** `~/.openclaw/config.yaml`
- **Keys directory:** `~/.openclaw/keys/`
- **LaunchAgent plists:** `~/Library/LaunchAgents/com.rout.*.plist`
- **Project directory:** Wherever Rout is cloned (referenced as INSTALL_DIR in plists)
- **Python:** System Python 3 with `--break-system-packages` for pip installs
- **GitHub:** https://github.com/kingmadellc/rout

## Service Architecture

Rout runs as multiple launchd services:

| Service | Plist | Purpose | Schedule |
|---------|-------|---------|----------|
| imsg-watcher | `com.rout.imsg-watcher.plist` | Core message handler, Socket.IO listener | Always running (KeepAlive) |
| morning-brief | `com.rout.morning-brief.plist` | Daily digest push | 8:00 AM daily (StartCalendarInterval) |
| coinbase-monitor | `com.rout.coinbase-monitor.plist` | Price alerts, big-move detection | Every 30 min (StartInterval) |
| webhook-server | `com.rout.webhook-server.plist` | HTTP endpoint for proactive triggers | Always running (KeepAlive) |

## launchd Plist Patterns

### Always-Running Service (KeepAlive)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.rout.service-name</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>INSTALL_DIR/script.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>INSTALL_DIR</string>

    <key>KeepAlive</key>
    <true/>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>HOME/Library/Logs/rout-service.log</string>

    <key>StandardErrorPath</key>
    <string>HOME/Library/Logs/rout-service-error.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
```

### Scheduled Service (Calendar Interval)

```xml
<!-- Runs at 8:00 AM daily -->
<key>StartCalendarInterval</key>
<dict>
    <key>Hour</key>
    <integer>8</integer>
    <key>Minute</key>
    <integer>0</integer>
</dict>
```

### Periodic Service (Fixed Interval)

```xml
<!-- Runs every 1800 seconds (30 minutes) -->
<key>StartInterval</key>
<integer>1800</integer>
```

### Critical Plist Rules

1. **INSTALL_DIR and HOME are placeholders** — Every plist ships with these placeholders. They MUST be replaced with actual paths before loading. Forgetting this is the #1 deploy failure.

2. **PYTHONUNBUFFERED=1** — Required for log output to appear in real-time. Without it, Python buffers stdout and logs appear delayed or not at all.

3. **PATH must include /usr/local/bin** — launchd agents get a minimal PATH. If your script uses tools in /usr/local/bin (like pip-installed CLIs), they won't be found without this.

4. **sys.path fix** — Python scripts launched via launchd don't have the project root in sys.path. Every Rout entry point prepends it:
   ```python
   import sys, os
   sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
   ```
   This goes at the very top of the file, before any Rout imports.

5. **WorkingDirectory** — Always set to INSTALL_DIR. Relative paths in the code depend on this.

## Service Lifecycle Commands

```bash
# Load a new service (first time)
cp launchd/com.rout.service.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.rout.service.plist

# Unload a service
launchctl unload ~/Library/LaunchAgents/com.rout.service.plist

# Restart a running service (force kill + relaunch)
launchctl kickstart -k gui/$(id -u)/com.rout.service-name

# Check if service is running
launchctl list | grep rout

# View service logs
tail -f ~/Library/Logs/rout-service.log
tail -f ~/Library/Logs/rout-service-error.log

# Force stop all Rout services
launchctl list | grep com.rout | awk '{print $3}' | xargs -I{} launchctl stop {}
```

### Restart vs. Reload

- **`launchctl kickstart -k`** — Kills and restarts the process. Use for code changes.
- **`launchctl unload` + `launchctl load`** — Unregisters and re-registers the service. Use for plist changes.
- If you changed the plist itself, you MUST unload/load. Kickstart only restarts the same plist config.

## Config Management (config.yaml)

```yaml
# ~/.openclaw/config.yaml

bluebubbles:
  url: "http://localhost:1234"
  password: "your-bb-password"

kalshi:
  api_key: "your-kalshi-key"
  # ... other kalshi config

coinbase:
  key_file: "~/.openclaw/keys/coinbase-secret.pem"
  key_name: "organizations/{org_id}/apiKeys/{key_id}"

polymarket:
  # No config needed — public API, zero auth

morning_brief:
  recipient_chat_guid: "iMessage;-;+1XXXXXXXXXX"
  enabled: true

webhooks:
  port: 7888
  auth_token: "your-webhook-secret"
```

### Config Conventions

- **Keys never go in config.yaml** — Private keys go in `~/.openclaw/keys/` with chmod 600. Config.yaml references the path.
- **New integrations get their own top-level section** — Don't nest under "integrations" or "services."
- **Always provide sensible defaults in code** — Config should be optional where possible. Polymarket needs zero config. Coinbase needs a key path. Design accordingly.
- **Config is loaded once at startup** — Changes require a service restart to take effect.

## Key Provisioning Pattern

For integrations that require API keys:

```bash
# 1. Generate the key (service-specific, documented per integration)
# Example: Coinbase CDP key from portal.cdp.coinbase.com

# 2. Save the private key
mkdir -p ~/.openclaw/keys
# Save key content to file (varies by service)
chmod 600 ~/.openclaw/keys/service-secret.pem

# 3. Add config section
# Edit ~/.openclaw/config.yaml

# 4. Verify
python3 -c "
from handlers.service_handlers import ServiceClient
c = ServiceClient()
print(c.get_something())
"

# 5. Restart watcher to pick up new config
launchctl kickstart -k gui/$(id -u)/com.rout.imsg-watcher
```

## Deployment Checklist Template

When deploying a new feature or integration, produce a checklist in this format:

```
DEPLOY: [Feature Name]
Prerequisites: [what must be true before starting]

1. [ ] Install dependencies
   cmd: pip install <package> --break-system-packages

2. [ ] Provision credentials
   cmd: [key generation steps]
   verify: [how to test the key works]

3. [ ] Update config
   file: ~/.openclaw/config.yaml
   add: [exact YAML to add]

4. [ ] Deploy code
   cmd: cd <repo> && git pull

5. [ ] Install/update plist (if new service)
   cmd: cp launchd/com.rout.X.plist ~/Library/LaunchAgents/
   cmd: launchctl load ~/Library/LaunchAgents/com.rout.X.plist

6. [ ] Restart affected services
   cmd: launchctl kickstart -k gui/$(id -u)/com.rout.imsg-watcher

7. [ ] Verify
   test: [iMessage command to test]
   expected: [what should come back]

8. [ ] Monitor logs for 5 minutes
   cmd: tail -f ~/Library/Logs/rout-watcher.log
```

## Release Process

On every .X release (v0.9, v1.0, etc.):

```bash
# 1. Update README.md — feature dashboard, capabilities, architecture, file structure
# 2. Create annotated tag
git tag -a vX.Y -m "vX.Y — [summary of what shipped]"
# 3. Push tag
git push origin --tags
```

On .XY releases (v0.8.1, v0.8.2): tags are optional — use judgment based on significance.

### Version Naming

- **vX.Y** — New integration or major feature (Kalshi, Coinbase, Polymarket, morning brief)
- **vX.Y.Z** — Fixes, improvements, or minor additions to existing features
- **v2.0** — Architectural changes (plugin system refactor, voice support, companion app)

## Troubleshooting Matrix

| Problem | Diagnosis | Resolution |
|---------|-----------|------------|
| Service won't start | `launchctl list | grep rout` shows negative exit code | Check error log, fix Python error, `launchctl kickstart` |
| Service starts then immediately dies | Exit code in launchctl list (e.g., 1) | Check error log — usually import error or missing config |
| Plist changes not taking effect | Used kickstart instead of unload/load | `launchctl unload` then `launchctl load` |
| "No module named 'handlers'" | sys.path not set in entry script | Add `sys.path.insert(0, ...)` at top of script |
| Permission denied on key file | chmod wrong | `chmod 600 ~/.openclaw/keys/*` |
| pip install fails | Missing --break-system-packages | Add the flag: `pip install X --break-system-packages` |
| Morning brief not firing | Wrong time or plist not loaded | Check `launchctl list`, verify StartCalendarInterval |
| Log files empty | PYTHONUNBUFFERED not set | Add to plist EnvironmentVariables |
