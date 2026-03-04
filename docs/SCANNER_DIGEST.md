# Scanner Digest — Proactive Agent Observability

**Shipped:** March 3, 2026
**Pattern:** Same as Stage 2 materiality gate logging

## Overview

Daily digest logging system that tracks the effectiveness of proactive agent scanners. Shows how many signals are found, passed through filters, and blocked by materiality gates.

## What Was Added

### 1. Daily Digest Tracking (`proactive/base.py`)

**New functions:**
- `_load_scanner_digest()` — Loads today's digest, auto-resets at midnight
- `_save_scanner_digest()` — Atomically saves digest to `~/.openclaw/state/scanner_digest.json`
- `_record_scanner_run(scanner_name, signals_found, signals_passed, signals_blocked)` — Records a scanner run
- `_get_scanner_digest_summary()` — Returns human-readable summary for morning brief

**Data structure:**
```json
{
  "date": "2026-03-03",
  "scanners": {
    "x_signals": {
      "runs": 12,
      "signals_found": 45,
      "signals_passed": 3,
      "signals_blocked": 42,
      "last_run": "2026-03-03T08:45:12.123456"
    },
    "edge_engine": { ... },
    "cross_platform": { ... }
  }
}
```

### 2. Scanner Instrumentation

**Modified scanners:**
- `triggers/x_signals.py` — Records Stage 1 (all signals), Stage 2 (materiality gate), Stage 3 (position gate) filtering
- `triggers/edge_engine.py` — Records Qwen analysis + alert threshold filtering
- `triggers/cross_platform.py` — Records divergence detection + threshold filtering

**Logging pattern:**
Each scanner calls `_record_scanner_run()` at key decision points:
- When no signals found (0/0/0)
- When signals filtered by materiality gate (N/0/N)
- When signals filtered by position gate (N/0/N)
- When signals actually sent (N/M/N-M)

### 3. Morning Briefing Integration (`triggers/morning.py`)

Morning briefing now automatically includes scanner digest after portfolio summary:

```
Good morning! Here's your day (Monday, March 03):

📅 Calendar:
  ...

💰 Portfolio:
  ...

📊 Scanner Activity (2026-03-03):
  • X Signals: 12 runs, 45 signals found, 3 passed, 42 blocked (7% pass rate)
  • Edge Engine: 4 runs, 89 signals found, 2 passed, 87 blocked (2% pass rate)
  • Cross Platform: 6 runs, 14 signals found, 1 passed, 13 blocked (7% pass rate)
```

## Usage

### Viewing Current Digest

```bash
# JSON file (machine-readable)
cat ~/.openclaw/state/scanner_digest.json

# Summary format (human-readable)
cd ~/.openclaw/workspace/rout
python3 -c "
import sys
sys.path.insert(0, 'scripts')
from proactive.base import _get_scanner_digest_summary
print(_get_scanner_digest_summary())
"
```

### Appears Automatically In:

1. **Morning briefing** — Daily summary shows yesterday's scanner activity
2. **Proactive agent logs** — Each run logs: `[digest] x_signals: run #12, found=5, passed=1, blocked=4`

### Manual Recording (for new scanners)

When adding a new scanner, instrument it with:

```python
from proactive.base import _record_scanner_run

def check_my_scanner(state, dry_run=False, force=False):
    # Stage 1: Find signals
    signals = _find_signals()

    if not signals:
        _record_scanner_run("my_scanner", signals_found=0, signals_passed=0, signals_blocked=0)
        return False

    # Stage 2: Filter
    filtered = _apply_filters(signals)

    if not filtered:
        _record_scanner_run("my_scanner",
                          signals_found=len(signals),
                          signals_passed=0,
                          signals_blocked=len(signals))
        return False

    # Stage 3: Send
    if _send_message(...):
        _record_scanner_run("my_scanner",
                          signals_found=len(signals),
                          signals_passed=len(filtered),
                          signals_blocked=len(signals) - len(filtered))
        return True

    # Send failed
    _record_scanner_run("my_scanner",
                      signals_found=len(signals),
                      signals_passed=0,
                      signals_blocked=len(signals))
    return False
```

## Benefits

1. **Visibility** — Now you can see what the materiality gate is filtering out
2. **Tuning** — Pass rates help calibrate filter thresholds (Stage 2 confidence, alert thresholds, etc.)
3. **Debugging** — When signals stop firing, check if they're being blocked vs. not found
4. **Accountability** — Scanner effectiveness visible in daily briefing

## Pattern Match: Stage 2 Materiality Gate

This follows the same logging pattern as Stage 2 materiality gate in `x_signals.py`:

```python
_log(f"Stage 2 filter: {len(signals)} candidates → {len(filtered)} kept. Reason: {reasoning}")
```

Now aggregated across the day and surfaced in morning brief instead of buried in logs.

## Files Changed

- `scripts/proactive/base.py` — Added digest tracking infrastructure
- `scripts/proactive/triggers/x_signals.py` — Added digest recording
- `scripts/proactive/triggers/edge_engine.py` — Added digest recording
- `scripts/proactive/triggers/cross_platform.py` — Added digest recording
- `scripts/proactive/triggers/morning.py` — Added digest display
- `docs/TASKS.md` — Moved task from Backlog to Done

## State File

**Location:** `~/.openclaw/state/scanner_digest.json`
**Lifecycle:** Auto-resets at midnight (date-based)
**Backup:** None needed — ephemeral daily state

---

**Implementation:** Subagent `observability-logging`
**Completion:** March 3, 2026
