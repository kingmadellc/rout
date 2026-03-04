#!/usr/bin/env python3
"""Rout proactive agent — entry point. See proactive/ for implementation.

Usage:
    python3 scripts/proactive_agent.py                         # Run once (check all triggers)
    python3 scripts/proactive_agent.py --dry-run                # Show what would fire, don't send
    python3 scripts/proactive_agent.py --only morning           # Run a single trigger
    python3 scripts/proactive_agent.py --only meeting           # Only check meeting reminders
    python3 scripts/proactive_agent.py --only portfolio         # Only check portfolio drift
    python3 scripts/proactive_agent.py --only conflicts         # Only check calendar conflicts
    python3 scripts/proactive_agent.py --only cross_platform    # Only check cross-platform divergences
    python3 scripts/proactive_agent.py --only x_signals         # Only check X signal scanner
    python3 scripts/proactive_agent.py --only kalshi_edge       # Only check Kalshi edge scanner
"""

import sys
from pathlib import Path

# Setup path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from proactive import run

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    only_trigger = None
    if "--only" in sys.argv:
        idx = sys.argv.index("--only")
        if idx + 1 < len(sys.argv):
            only_trigger = sys.argv[idx + 1]
    run(dry_run=dry, only=only_trigger)
