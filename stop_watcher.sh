#!/bin/bash
# Stop the iMessage watcher

LABEL="com.rout.imsg-watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if launchctl list "$LABEL" &>/dev/null; then
    echo "🛑 Stopping watcher..."
    launchctl unload "$PLIST" 2>/dev/null
    echo "✅ Stopped. Run ./start_watcher.sh to restart."
else
    # Kill any direct-launch process
    PIDS=$(pgrep -f "imsg_watcher" 2>/dev/null)
    if [ -n "$PIDS" ]; then
        echo "🛑 Killing watcher process(es): $PIDS"
        echo "$PIDS" | xargs kill
        echo "✅ Stopped."
    else
        echo "❌ Watcher not running."
    fi
fi
