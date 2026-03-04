#!/usr/bin/env bash
#
# record_demo.sh — Automated Rout demo recorder
#
# Sends representative Rout messages in sequence while you screen-record.
# Output: Screen recording of Messages.app showing all 3 alert types.
#
# Usage:
#   bash scripts/record_demo.sh              # Full run (sends real iMessages)
#   bash scripts/record_demo.sh --dry-run    # Preview messages, don't send
#
# Prerequisites:
#   - Messages.app open with the test conversation visible
#   - Focus mode ON (silence other notifications)
#   - imsg CLI available
#
# Sequence (~60s):
#   0-3s    : Hold — clean frame
#   3-18s   : Morning brief arrives
#   20-35s  : X signal alert arrives
#   37-52s  : Kalshi edge alert arrives
#   52-60s  : Hold — viewer absorbs
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DRY_RUN=""

if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN="1"
    echo "🏃 DRY RUN — messages printed but not sent"
    echo ""
fi

# ── Demo messages ─────────────────────────────────────────────────────────────
# These represent real Rout output format. Not fake — just deterministic.

TODAY=$(date "+%A, %B %d")
SHORT_DATE=$(date "+%B %d")

MSG_MORNING="Good morning! Here's your day ($TODAY):

📅 Calendar:
No events today.

💰 Portfolio:
📈 P&L: +\$18.42 across 8 positions
💵 Cash: \$12.46  ·  Deployed: \$192.08  ·  Value: \$210.50

🔥 Dems Control House in Midterms: 10x YES @ \$38.00 → \$52.00 (+37%)
✅ Netflix Acquires Warner Bros: 10x YES @ \$8.00 → \$11.50 (+44%)
🔻 Warsh as Fed Chair by End of Year: 10x YES @ \$15.00 → \$12.00 (-20%)
➖ U.S. acquires Greenland by 2029: 120x NO @ \$96.00 → \$97.20 (+1%)"

MSG_X_SIGNAL="🔍 X signal alert:
  📈 Trump tariff executive order: White House confirms new 25% tariff on EU auto imports effective March 15, markets reacting (88% conf)
  📉 Fed rate cut hike FOMC: Fed Governor Waller signals no rate cuts until Q3 at earliest, contradicting market pricing (82% conf)"

MSG_KALSHI_EDGE="🎯 Kalshi Edge Scanner — top opportunities:

1. 📊 Will TikTok be banned by June 2026?
   Market says 34% YES | vol 12,450 | closes 91d
   YES: bid 32¢ / ask 36¢ | HIGH

2. 📊 US recession declared by end of 2026?
   Market says 28% YES | vol 8,200 | closes 305d
   YES: bid 26¢ / ask 30¢ | MEDIUM

3. 📊 Trump signs infrastructure bill by Q3?
   Market says 41% YES | vol 5,100 | closes 183d
   NO: bid 57¢ / ask 61¢ | MEDIUM"

# ── Send helper ───────────────────────────────────────────────────────────────

send_msg() {
    local label="$1"
    local msg="$2"

    echo "🔥 [$label]"

    if [[ -n "$DRY_RUN" ]]; then
        echo "--- MESSAGE ---"
        echo "$msg"
        echo "--- END ---"
        echo ""
    else
        # Write to temp file to avoid shell escaping issues
        local tmp="/tmp/rout_demo_msg_$$.txt"
        printf '%s' "$msg" > "$tmp"
        imsg send --chat-id 1 --service imessage --text "$(cat "$tmp")" 2>&1 | tail -1
        rm -f "$tmp"
    fi
}

# ── Preflight ─────────────────────────────────────────────────────────────────

echo "🎬 Rout Demo Recorder"
echo "====================="
echo ""
echo "Before starting:"
echo "  1. Messages.app open with Matt conversation visible"
echo "  2. Focus mode ON"
echo "  3. Press Cmd+Shift+5, select recording area, click Record"
echo ""

if [[ -z "$DRY_RUN" ]]; then
    echo "Press ENTER when screen recording is running..."
    read -r
    echo ""
    echo "🔴 Recording in progress. Firing messages..."
    echo ""
fi

# ── Scene 1: Hold ────────────────────────────────────────────────────────────

echo "📍 Scene 1: Clean hold (3s)"
sleep 3

# ── Scene 2: Morning Brief ───────────────────────────────────────────────────

echo "📍 Scene 2: Morning brief"
send_msg "Morning Brief" "$MSG_MORNING"
sleep 15

# ── Scene 3: X Signal Alert ─────────────────────────────────────────────────

echo "📍 Scene 3: X signal alert"
sleep 2
send_msg "X Signals" "$MSG_X_SIGNAL"
sleep 13

# ── Scene 4: Kalshi Edge ────────────────────────────────────────────────────

echo "📍 Scene 4: Kalshi edge scanner"
sleep 2
send_msg "Kalshi Edge" "$MSG_KALSHI_EDGE"
sleep 13

# ── Scene 5: Hold ────────────────────────────────────────────────────────────

echo "📍 Scene 5: Final hold (8s)"
sleep 8

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo "✅ All messages sent!"
echo ""
echo "Stop the recording: click the stop button in the menu bar"
echo "Save to: ~/Desktop/rout-demo-raw.mov"
echo ""
echo "Then frame it:"
echo "  open scripts/phone_frame.html"
echo "  Drag the .mov onto the phone"
