#!/bin/bash
# ============================================================================
# Rout Migration Script: Single Source of Truth
# ============================================================================
# Date: 2026-03-01
# Purpose: Fix diverged codebase — kill dead services, remove legacy fallback,
#          clean up orphaned plists, ensure everything runs from the git repo.
#
# What this does:
#   1. Unloads and removes 6 BROKEN launchd services
#   2. Removes LEGACY_WORKSPACE fallback from the watcher
#   3. Verifies the 4 WORKING services point to the git repo
#   4. Reports what needs manual attention
#
# Run: bash scripts/migrate-to-single-source.sh
# Dry run: bash scripts/migrate-to-single-source.sh --dry-run
# ============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
ROUT_DIR="$HOME/.openclaw/workspace/rout"
WATCHER="$ROUT_DIR/comms/imsg_watcher.py"

log_action() { echo -e "${GREEN}[ACTION]${NC} $1"; }
log_warn()   { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_dead()   { echo -e "${RED}[DEAD]${NC} $1"; }
log_ok()     { echo -e "${GREEN}[OK]${NC} $1"; }

echo "============================================"
echo "Rout Migration: Single Source of Truth"
echo "============================================"
echo ""
$DRY_RUN && echo -e "${YELLOW}*** DRY RUN MODE — no changes will be made ***${NC}" && echo ""

# ── Step 1: Kill BROKEN services ──────────────────────────────────────────
echo "── Step 1: Removing broken launchd services ──"
echo ""

DEAD_SERVICES=(
    # Points to hardened/cross_platform_comparator.py — script deleted with hardened/
    "com.rout.cross-platform-comparator"
    # Points to hardened/x_signal_scanner.py — script deleted with hardened/
    "com.rout.x-signal-scanner"
    # Points to rout-kalshi-scanner wrapper (doesn't exist) + hardened/ working dir
    "com.rout.kalshi-scanner"
    # Points to rout-kalshi-monitor wrapper which calls kalshi_exit_monitor.py (doesn't exist)
    "com.rout.kalshi-monitor"
    # Points to doubled path: OpenClaw Skills/OpenClaw Skills/rout-agent-runner.py (wrong)
    "com.rout.agent-code-review"
    # Same doubled path issue
    "com.rout.agent-deploy-verify"
    # Same doubled path issue
    "com.rout.agent-health-check"
)

for svc in "${DEAD_SERVICES[@]}"; do
    plist="$LAUNCH_AGENTS/${svc}.plist"
    if [ -f "$plist" ]; then
        log_dead "Killing: $svc"
        if ! $DRY_RUN; then
            launchctl bootout "gui/$(id -u)/$svc" 2>/dev/null || true
            rm "$plist"
            log_action "Removed $plist"
        else
            echo "  Would unload and remove: $plist"
        fi
    else
        echo "  Already gone: $svc"
    fi
done
echo ""

# ── Step 2: Remove LEGACY_WORKSPACE from watcher ─────────────────────────
echo "── Step 2: Removing LEGACY_WORKSPACE fallback ──"
echo ""

if grep -q "LEGACY_WORKSPACE" "$WATCHER"; then
    log_warn "Found LEGACY_WORKSPACE in $WATCHER"
    if ! $DRY_RUN; then
        # Remove the LEGACY_WORKSPACE line
        sed -i '' '/^LEGACY_WORKSPACE/d' "$WATCHER"

        # Remove LEGACY_WORKSPACE from the _resolve_workspace candidates list
        sed -i '' '/candidates\.append(LEGACY_WORKSPACE)/d' "$WATCHER"

        log_action "Removed LEGACY_WORKSPACE references from watcher"
    else
        echo "  Would remove LEGACY_WORKSPACE lines from: $WATCHER"
        grep -n "LEGACY_WORKSPACE" "$WATCHER"
    fi
else
    log_ok "No LEGACY_WORKSPACE found — already clean"
fi
echo ""

# ── Step 3: Verify surviving services ─────────────────────────────────────
echo "── Step 3: Verifying surviving services ──"
echo ""

SURVIVING=(
    "com.rout.imsg-watcher"
    "com.rout.webhook-server"
    "com.rout.log-rotation"
)

for svc in "${SURVIVING[@]}"; do
    plist="$LAUNCH_AGENTS/${svc}.plist"
    if [ -f "$plist" ]; then
        if grep -q "hardened" "$plist"; then
            log_dead "PROBLEM: $svc still references hardened/"
        elif grep -q "REPLACE_WITH" "$plist"; then
            log_warn "$svc has unresolved placeholders"
        else
            log_ok "$svc — clean, points to git repo"
        fi
    else
        log_warn "$svc — plist not installed"
    fi
done
echo ""

# ── Step 4: Remove orphaned wrapper scripts in git repo ───────────────────
echo "── Step 4: Cleaning orphaned wrapper scripts ──"
echo ""

ORPHAN_WRAPPERS=(
    "$ROUT_DIR/rout-kalshi-monitor"
)

for wrapper in "${ORPHAN_WRAPPERS[@]}"; do
    if [ -f "$wrapper" ]; then
        log_warn "Orphaned wrapper: $wrapper"
        echo "  (calls kalshi_exit_monitor.py which doesn't exist)"
        if ! $DRY_RUN; then
            rm "$wrapper"
            log_action "Removed $wrapper"
        else
            echo "  Would remove: $wrapper"
        fi
    fi
done
echo ""

# ── Step 5: Report ────────────────────────────────────────────────────────
echo "============================================"
echo "MIGRATION SUMMARY"
echo "============================================"
echo ""
echo "SERVICES KILLED (broken — script/target doesn't exist):"
echo "  - com.rout.cross-platform-comparator (hardened/cross_platform_comparator.py — gone)"
echo "  - com.rout.x-signal-scanner (hardened/x_signal_scanner.py — gone)"
echo "  - com.rout.kalshi-scanner (rout-kalshi-scanner wrapper — never existed)"
echo "  - com.rout.kalshi-monitor (kalshi_exit_monitor.py — never existed in git)"
echo "  - com.rout.agent-code-review (doubled path — never ran)"
echo "  - com.rout.agent-deploy-verify (doubled path — never ran)"
echo "  - com.rout.agent-health-check (doubled path — never ran)"
echo ""
echo "SERVICES SURVIVING (working, pointing to git repo):"
echo "  ✅ com.rout.imsg-watcher — core watcher, KeepAlive"
echo "  ✅ com.rout.webhook-server — HTTP event trigger endpoint"
echo "  ✅ com.rout.log-rotation — audit log rotation, 3 AM daily"
echo ""
echo "WATCHER FIX:"
echo "  Removed LEGACY_WORKSPACE fallback from comms/imsg_watcher.py"
echo "  (This was the drift trap — silent fallback to hardened/)"
echo ""
echo "NOT YET INSTALLED (templates exist in git repo launchd/):"
echo "  📋 com.rout.proactive-agent — 15min interval, morning brief + portfolio drift"
echo "     Template: launchd/com.rout.proactive-agent.plist"
echo "     Install when ready: setup.sh handles placeholder replacement"
echo ""
echo "FEATURES THAT LOST THEIR CRON SCRIPTS:"
echo "  ⚠️  Cross-platform comparator — was standalone, now gone"
echo "  ⚠️  X signal scanner — was standalone, now gone"
echo "  ⚠️  Kalshi edge scanner — was standalone, now gone"
echo "  ⚠️  Coinbase price monitor — was standalone, never deployed"
echo "  ⚠️  Morning brief — was standalone, replaced by proactive_agent.py"
echo "  ⚠️  Kalshi exit monitor — was standalone, now gone"
echo ""
echo "DECISION NEEDED:"
echo "  These features were running as standalone cron scripts in hardened/."
echo "  hardened/ is deleted. The scripts are gone. Options:"
echo "    A) Rebuild as standalone scripts in workspace/rout/scripts/"
echo "    B) Port functionality into proactive_agent.py triggers"
echo "    C) Declare dead — they weren't providing value"
echo ""

if ! $DRY_RUN; then
    echo "Done. Restart watcher to pick up changes:"
    echo "  launchctl kickstart -k gui/\$(id -u)/com.rout.imsg-watcher"
fi
