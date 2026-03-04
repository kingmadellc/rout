"""Proactive agent package — cron-driven outbound messages.

The feature that makes Rout feel alive. Runs on a 15-minute interval
via launchd and sends contextual messages without being asked.

Personality layer wraps all triggers:
  - Context buffer (back-references within the day)
  - Editorial voice (opinion, not just data)
  - Variable timing (rhythm, not metronome)
  - Selective silence (knows when to shut up)
  - Micro-initiations (ambient awareness)
  - Response tracking (adapts to engagement patterns)

Triggers:
  - Morning briefing (configurable time): Today's calendar + pending reminders
  - Meeting reminder (30 min before): "You have X in 30 minutes"
  - Portfolio drift: Alert on significant position moves
  - Calendar conflicts: Evening check for tomorrow's overlaps
  - Cross-platform comparator: Kalshi vs Polymarket price divergences
  - X signal scanner: DDG + local Qwen for X/Twitter market signals
  - Edge engine: Unified Kalshi + Polygon + Qwen probability analysis

Rate limit: Max 3 proactive messages per hour.
Launch: launchd plist, 15-minute interval.
"""

import atexit
import signal
import sys
from proactive.base import (
    PROACTIVE_CFG,
    MAX_MESSAGES_PER_HOUR,
    _load_state,
    _save_state,
    _acquire_lock,
    _release_lock,
    _rate_limited,
    _log,
)
from proactive.triggers.morning import check_morning_briefing
from proactive.triggers.meeting import check_meeting_reminders
from proactive.triggers.portfolio import check_portfolio_drift
from proactive.triggers.conflicts import check_calendar_conflicts
from proactive.triggers.cross_platform import check_cross_platform
from proactive.triggers.x_signals import check_x_signals
from proactive.triggers.edge_engine import check_edge_engine

# Personality layer
try:
    from proactive.personality.engine import (
        init as personality_init,
        check_micro_initiations,
        check_adjustment_suggestions,
    )
    _PERSONALITY_AVAILABLE = True
except ImportError as e:
    _PERSONALITY_AVAILABLE = False
    _log(f"[personality] Import failed: {e} — running without personality layer")


TRIGGER_MAP = {
    "morning": check_morning_briefing,
    "meeting": check_meeting_reminders,
    "portfolio": check_portfolio_drift,
    "conflicts": check_calendar_conflicts,
    "cross_platform": check_cross_platform,
    "x_signals": check_x_signals,
    "edge": check_edge_engine,
    # Legacy aliases for backward compat
    "kalshi_edge": check_edge_engine,
    "kalshi_research": check_edge_engine,
}


def _personality_enabled() -> bool:
    """Check if personality layer is enabled in config."""
    if not _PERSONALITY_AVAILABLE:
        return False
    return PROACTIVE_CFG.get("personality", {}).get("enabled", True)


def run(dry_run: bool = False, only: str = None):
    """Run proactive triggers.

    Args:
        dry_run: Show what would fire without sending.
        only: Run a single trigger by name (morning, meeting, portfolio, conflicts,
              cross_platform, x_signals, edge).
    """
    if not PROACTIVE_CFG.get("enabled", True):
        _log("Proactive agent disabled in config.")
        return

    if not _acquire_lock():
        _log("Another instance is running (lockfile exists). Exiting.")
        return

    atexit.register(_release_lock)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # atexit fires on SIGTERM

    state = _load_state()

    # Initialize personality layer
    if _personality_enabled():
        personality_init()
        _log("[personality] Initialized — editorial voice, context buffer, timing active")

    # Single trigger mode — bypass global rate limit
    if only:
        trigger_fn = TRIGGER_MAP.get(only)
        if not trigger_fn:
            _log(f"Unknown trigger '{only}'. Options: {', '.join(TRIGGER_MAP)}")
            _save_state(state)
            return
        _log(f"Running single trigger: {only}")
        trigger_fn(state, dry_run=dry_run, force=True)
        _save_state(state)
        _log("Done.")
        return

    if _rate_limited(state):
        _log(f"Rate limited ({MAX_MESSAGES_PER_HOUR}/hr). Skipping.")
        _save_state(state)
        return

    _log("Checking triggers...")

    # Run triggers in priority order
    check_morning_briefing(state, dry_run=dry_run)

    if not _rate_limited(state):
        check_meeting_reminders(state, dry_run=dry_run)

    if not _rate_limited(state):
        check_portfolio_drift(state, dry_run=dry_run)

    if not _rate_limited(state):
        check_calendar_conflicts(state, dry_run=dry_run)

    if not _rate_limited(state):
        check_cross_platform(state, dry_run=dry_run)

    if not _rate_limited(state):
        check_x_signals(state, dry_run=dry_run)

    # Edge engine runs last — it's the heaviest (Qwen analysis)
    # Doesn't respect rate limit for cache writes, only for iMessage alerts
    check_edge_engine(state, dry_run=dry_run)

    # Personality layer — micro-initiations + adjustment suggestions
    if _personality_enabled() and not _rate_limited(state):
        check_micro_initiations(state, dry_run=dry_run)
        check_adjustment_suggestions(state, dry_run=dry_run)

    _save_state(state)
    _log("Done.")


__all__ = ["run", "TRIGGER_MAP"]
