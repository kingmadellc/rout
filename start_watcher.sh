#!/bin/bash
# Start the iMessage watcher via launchd (auto-restart + survives reboots)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.rout.imsg-watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if launchctl list "$LABEL" &>/dev/null; then
    echo "❌ Watcher already running (managed by launchd)"
    echo "   To stop: ./stop_watcher.sh"
    exit 1
fi

if [ ! -f "$PLIST" ]; then
    echo "❌ Plist not installed. Run: ./setup.sh first"
    exit 1
fi

echo "🚀 Starting iMessage watcher..."
launchctl load "$PLIST"
sleep 2

if launchctl list "$LABEL" &>/dev/null; then
    echo "✅ Watcher started"
    echo "   ♻️  Auto-restarts on crash"
    echo "   🔁 Starts automatically on login"
    echo "   📝 Logs: tail -f $SCRIPT_DIR/imsg_watcher.log"
else
    echo "⚠️  launchd load may have failed — check: tail -f $SCRIPT_DIR/imsg_watcher.log"
fi
