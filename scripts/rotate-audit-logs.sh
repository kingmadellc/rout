#!/usr/bin/env bash
# =============================================================================
# rotate-audit-logs.sh — Purge old audit logs containing message previews
# =============================================================================
# 7-day retention on imsg_audit.jsonl, 30-day on trades.jsonl.
# Run via launchd daily or manually.
#
# USAGE:
#   bash scripts/rotate-audit-logs.sh
#   bash scripts/rotate-audit-logs.sh --dry-run
# =============================================================================

set -euo pipefail

OPENCLAW_DIR="${ROUT_OPENCLAW_DIR:-$HOME/.openclaw}"
LOG_DIR="$OPENCLAW_DIR/logs"
RETENTION_DAYS=7
DRY_RUN=false

if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

AUDIT_LOG="$LOG_DIR/imsg_audit.jsonl"
if [[ -f "$AUDIT_LOG" ]]; then
    CUTOFF=$(date -v-${RETENTION_DAYS}d +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -d "$RETENTION_DAYS days ago" +%Y-%m-%dT%H:%M:%S)
    BEFORE=$(wc -l < "$AUDIT_LOG" | tr -d ' ')

    if $DRY_RUN; then
        REMOVE=$(python3 -c "
import json, sys
cutoff = '$CUTOFF'
count = 0
for line in open('$AUDIT_LOG'):
    try:
        ts = json.loads(line).get('timestamp', '')
        if ts < cutoff:
            count += 1
    except: pass
print(count)
")
        echo "[DRY RUN] imsg_audit.jsonl: $BEFORE total lines, $REMOVE would be purged (older than $RETENTION_DAYS days)"
    else
        python3 -c "
import json, tempfile, os, shutil

cutoff = '$CUTOFF'
src = '$AUDIT_LOG'
kept = removed = 0

with tempfile.NamedTemporaryFile(mode='w', dir='$LOG_DIR', delete=False, suffix='.tmp') as tmp:
    tmpname = tmp.name
    for line in open(src):
        try:
            ts = json.loads(line).get('timestamp', '')
            if ts >= cutoff:
                tmp.write(line)
                kept += 1
            else:
                removed += 1
        except:
            tmp.write(line)
            kept += 1

shutil.move(tmpname, src)
os.chmod(src, 0o600)
print(f'imsg_audit.jsonl: kept {kept}, purged {removed} (older than $RETENTION_DAYS days)')
"
    fi
else
    echo "No imsg_audit.jsonl found at $AUDIT_LOG"
fi

TRADES_LOG="$LOG_DIR/trades.jsonl"
if [[ -f "$TRADES_LOG" ]]; then
    TRADES_CUTOFF=$(date -v-30d +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -d "30 days ago" +%Y-%m-%dT%H:%M:%S)
    BEFORE=$(wc -l < "$TRADES_LOG" | tr -d ' ')

    if $DRY_RUN; then
        echo "[DRY RUN] trades.jsonl: $BEFORE total lines (30-day retention)"
    else
        python3 -c "
import json, tempfile, os, shutil

cutoff = '$TRADES_CUTOFF'
src = '$TRADES_LOG'
kept = removed = 0

with tempfile.NamedTemporaryFile(mode='w', dir='$LOG_DIR', delete=False, suffix='.tmp') as tmp:
    tmpname = tmp.name
    for line in open(src):
        try:
            ts = json.loads(line).get('timestamp', '')
            if ts >= cutoff:
                tmp.write(line)
                kept += 1
            else:
                removed += 1
        except:
            tmp.write(line)
            kept += 1

shutil.move(tmpname, src)
os.chmod(src, 0o600)
print(f'trades.jsonl: kept {kept}, purged {removed} (older than 30 days)')
"
    fi
fi

for logfile in "$LOG_DIR"/*.log; do
    [[ -f "$logfile" ]] || continue
    SIZE=$(stat -f%z "$logfile" 2>/dev/null || stat -c%s "$logfile")
    if (( SIZE > 5242880 )); then
        if $DRY_RUN; then
            echo "[DRY RUN] $(basename "$logfile"): $(( SIZE / 1024 ))KB — would rotate"
        else
            mv "$logfile" "${logfile}.old"
            touch "$logfile"
            chmod 600 "$logfile"
            echo "Rotated $(basename "$logfile") ($(( SIZE / 1024 ))KB)"
        fi
    fi
done

echo "Done."
