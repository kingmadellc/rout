#!/bin/bash
# Stop the iMessage watcher

set -euo pipefail

LABEL="com.rout.imsg-watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "Stopping watcher..."
    if ! launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1; then
        launchctl unload "$PLIST" 2>/dev/null || true
    fi
    echo "Stopped. Run ./start_watcher.sh to restart."
else
    PIDS=$(pgrep -f "imsg_watcher" 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo "Killing watcher process(es): $PIDS"
        echo "$PIDS" | xargs kill
        echo "Stopped."
    else
        echo "Watcher not running."
    fi
fi
