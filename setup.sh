#!/bin/bash
# setup.sh — runs setup wizard and installs launchd plist templates.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

render_plist() {
  local src="$1"
  local dst="$2"

  sed \
    -e "s|INSTALL_DIR|$SCRIPT_DIR|g" \
    -e "s|/Users/REPLACE_WITH_YOUR_USERNAME|$HOME|g" \
    "$src" > "$dst"
  chmod 644 "$dst"
}

echo ""
echo "Starting Rout setup wizard..."
echo ""

cd "$SCRIPT_DIR"
python3 setup.py

mkdir -p "$LAUNCH_AGENTS_DIR"

render_plist \
  "$SCRIPT_DIR/launchd/com.rout.imsg-watcher.plist" \
  "$LAUNCH_AGENTS_DIR/com.rout.imsg-watcher.plist"

# Optional: proactive agent plist (morning briefings, meeting reminders)
if [ -f "$SCRIPT_DIR/launchd/com.rout.proactive-agent.plist" ]; then
  render_plist \
    "$SCRIPT_DIR/launchd/com.rout.proactive-agent.plist" \
    "$LAUNCH_AGENTS_DIR/com.rout.proactive-agent.plist"
  echo ""
  echo "Setup wizard complete."
  echo "Installed launchd plists:"
  echo "  $LAUNCH_AGENTS_DIR/com.rout.imsg-watcher.plist"
  echo "  $LAUNCH_AGENTS_DIR/com.rout.proactive-agent.plist"
else
  echo ""
  echo "Setup wizard complete."
  echo "Installed launchd plists:"
  echo "  $LAUNCH_AGENTS_DIR/com.rout.imsg-watcher.plist"
fi
echo ""
echo "Start the watcher:"
echo "  ./start_watcher.sh"
echo ""
