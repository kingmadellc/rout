#!/bin/bash
# Start the iMessage watcher via launchd (auto-restart + survives reboots)

set -euo pipefail

LABEL="com.rout.imsg-watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LOG_PATH="$HOME/.openclaw/logs/imsg_watcher.log"

if [ ! -f "$PLIST" ]; then
    echo "Plist not installed. Run: ./setup.sh"
    exit 1
fi

mkdir -p "$HOME/.openclaw/logs"

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "Watcher already running (managed by launchd)"
    echo "To stop: ./stop_watcher.sh"
    exit 1
fi

echo "Starting iMessage watcher..."
if ! launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1; then
    # Fallback for older launchctl behavior
    launchctl load "$PLIST"
fi
sleep 2

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "Watcher started"
    echo "  Auto-restarts on crash"
    echo "  Starts automatically on login"
    echo "  Logs: tail -f $LOG_PATH"
else
    echo "launchd start may have failed. Check: tail -f $LOG_PATH"
fi
