"""Response-aware behavior — adapts to what Matt engages with.

Tracks whether Matt replies to/engages with different types of alerts.
Over time, Rout learns:
- "He always ignores Kalshi edge alerts" → dial those back
- "He replies fast to crypto signals" → prioritize those
- "He never responds to morning briefs" → skip or simplify

This is the highest-leverage lifelike behavior: adapting to USER behavior
without being told. After 2 weeks of data, Rout should proactively say:
"You've been ignoring edge alerts — want me to dial those back?"

State stored in ~/.openclaw/state/response_tracker.json
"""

import json
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional


STATE_DIR = Path(
    os.environ.get("ROUT_OPENCLAW_DIR", str(Path.home() / ".openclaw"))
) / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
TRACKER_PATH = STATE_DIR / "response_tracker.json"


class ResponseTracker:
    """Tracks engagement patterns per trigger type.

    Each trigger has:
        {
            "sends": 15,           # total proactive messages sent
            "engagements": 8,      # times user replied within window
            "ignores": 7,          # times user did NOT reply within window
            "avg_response_time_s": 420,  # average time to reply (seconds)
            "last_send_ts": 1700000000,
            "last_engagement_ts": 1700000000,
            "engagement_rate": 0.53,     # computed: engagements / sends
        }
    """

    ENGAGEMENT_WINDOW = 3600  # 1 hour — if user replies within this, it's engagement

    def __init__(self):
        self._data = {}
        self._load()

    def _load(self):
        if TRACKER_PATH.exists():
            try:
                with open(TRACKER_PATH, "r") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def _save(self):
        try:
            tmp = TRACKER_PATH.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=2)
            os.replace(str(tmp), str(TRACKER_PATH))
        except OSError:
            pass

    def record_send(self, trigger: str):
        """Record that Rout sent a proactive message for this trigger."""
        if trigger not in self._data:
            self._data[trigger] = {
                "sends": 0, "engagements": 0, "ignores": 0,
                "avg_response_time_s": 0, "last_send_ts": 0,
                "last_engagement_ts": 0, "engagement_rate": 0.0,
                "pending_engagement_check": False,
            }

        entry = self._data[trigger]
        entry["sends"] += 1
        entry["last_send_ts"] = time.time()
        entry["pending_engagement_check"] = True
        self._recompute_rate(trigger)
        self._save()

    def record_user_message(self):
        """Called when any user message comes in.

        Checks all triggers that are pending engagement check.
        If user replied within the engagement window, count it.
        """
        now = time.time()
        changed = False

        for trigger, entry in self._data.items():
            if not entry.get("pending_engagement_check"):
                continue

            last_send = entry.get("last_send_ts", 0)
            elapsed = now - last_send

            if elapsed <= self.ENGAGEMENT_WINDOW:
                # User replied within window — engagement!
                entry["engagements"] += 1
                entry["last_engagement_ts"] = now

                # Update rolling average response time
                old_avg = entry.get("avg_response_time_s", 0)
                old_count = entry.get("engagements", 1)
                if old_count > 1:
                    entry["avg_response_time_s"] = (
                        (old_avg * (old_count - 1) + elapsed) / old_count
                    )
                else:
                    entry["avg_response_time_s"] = elapsed

                entry["pending_engagement_check"] = False
                changed = True

        if changed:
            for trigger in self._data:
                self._recompute_rate(trigger)
            self._save()

    def check_expired_engagements(self):
        """Called periodically to mark sends as 'ignored' if the window passed.

        If a send happened > ENGAGEMENT_WINDOW ago and no reply came, it's an ignore.
        """
        now = time.time()
        changed = False

        for trigger, entry in self._data.items():
            if not entry.get("pending_engagement_check"):
                continue

            last_send = entry.get("last_send_ts", 0)
            if now - last_send > self.ENGAGEMENT_WINDOW:
                entry["ignores"] += 1
                entry["pending_engagement_check"] = False
                changed = True

        if changed:
            for trigger in self._data:
                self._recompute_rate(trigger)
            self._save()

    def _recompute_rate(self, trigger: str):
        """Recompute engagement rate for a trigger."""
        entry = self._data.get(trigger, {})
        sends = entry.get("sends", 0)
        if sends > 0:
            entry["engagement_rate"] = entry.get("engagements", 0) / sends
        else:
            entry["engagement_rate"] = 0.0

    def get_engagement_rate(self, trigger: str) -> float:
        """Get the engagement rate for a trigger (0.0-1.0)."""
        entry = self._data.get(trigger, {})
        return entry.get("engagement_rate", 0.5)  # Default: assume 50%

    def get_urgency_modifier(self, trigger: str) -> float:
        """Get a multiplier for urgency based on engagement history.

        High engagement rate → boost urgency (user wants these)
        Low engagement rate → dampen urgency (user ignores these)

        Returns a multiplier: 0.5 to 1.5
        """
        entry = self._data.get(trigger, {})
        sends = entry.get("sends", 0)

        if sends < 5:
            return 1.0  # Not enough data — neutral

        rate = entry.get("engagement_rate", 0.5)

        if rate >= 0.7:
            return 1.3  # User loves these — boost
        elif rate >= 0.5:
            return 1.1  # Decent engagement
        elif rate >= 0.3:
            return 0.9  # Below average
        elif rate >= 0.1:
            return 0.7  # User mostly ignores
        else:
            return 0.5  # User never engages — nearly suppress

    def should_suggest_adjustment(self, trigger: str) -> Optional[str]:
        """Check if engagement is so low we should ask the user.

        Returns a suggestion message, or None.
        "You've been ignoring edge alerts — want me to dial those back?"

        Only triggers after 10+ sends with <20% engagement.
        Only suggests once per trigger.
        """
        entry = self._data.get(trigger, {})
        sends = entry.get("sends", 0)
        rate = entry.get("engagement_rate", 0.5)
        already_suggested = entry.get("adjustment_suggested", False)

        if already_suggested:
            return None

        if sends < 10:
            return None  # Not enough data

        if rate >= 0.2:
            return None  # Engagement is fine

        # Format trigger name for display
        trigger_name = {
            "cross_platform": "Kalshi/PM divergence alerts",
            "portfolio": "portfolio drift alerts",
            "x_signals": "X signal alerts",
            "edge": "edge scan alerts",
            "morning": "morning briefs",
            "conflicts": "calendar conflict alerts",
            "meeting": "meeting reminders",
        }.get(trigger, f"{trigger} alerts")

        entry["adjustment_suggested"] = True
        self._save()

        return (
            f"Noticed you rarely respond to {trigger_name} "
            f"({sends} sent, you engaged with {entry.get('engagements', 0)}). "
            f"Want me to dial those back or change what I flag?"
        )

    def get_stats(self) -> dict:
        """Get a summary of all engagement stats."""
        return dict(self._data)

    def get_stats_summary(self) -> str:
        """Human-readable summary for debugging."""
        if not self._data:
            return "No engagement data yet."

        lines = ["Engagement stats:"]
        for trigger, entry in sorted(self._data.items()):
            sends = entry.get("sends", 0)
            rate = entry.get("engagement_rate", 0)
            avg_time = entry.get("avg_response_time_s", 0)
            lines.append(
                f"  {trigger}: {sends} sent, "
                f"{rate:.0%} engagement, "
                f"{avg_time/60:.0f}min avg reply time"
            )
        return "\n".join(lines)
