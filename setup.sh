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

# Install all service plists via render_plist (replaces placeholders with real paths)
INSTALLED=("$LAUNCH_AGENTS_DIR/com.rout.imsg-watcher.plist")

for svc in proactive-agent webhook-server kalshi-monitor; do
  src="$SCRIPT_DIR/launchd/com.rout.${svc}.plist"
  dst="$LAUNCH_AGENTS_DIR/com.rout.${svc}.plist"
  if [ -f "$src" ]; then
    render_plist "$src" "$dst"
    INSTALLED+=("$dst")
  fi
done

echo ""
echo "Setup wizard complete."
echo "Installed launchd plists:"
for p in "${INSTALLED[@]}"; do
  echo "  $p"
done
echo ""
echo "Start the watcher:"
echo "  ./start_watcher.sh"
echo ""
