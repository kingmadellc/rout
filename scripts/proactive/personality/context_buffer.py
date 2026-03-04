"""Today's context buffer — gives Rout memory within a day.

Rout remembers what it sent today and can back-reference prior messages.
"Remember that Kalshi spread I flagged this morning? It just widened."

State stored in ~/.openclaw/state/daily_context.json, auto-resets at midnight.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


STATE_DIR = Path(
    os.environ.get("ROUT_OPENCLAW_DIR", str(Path.home() / ".openclaw"))
) / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
CONTEXT_PATH = STATE_DIR / "daily_context.json"


class ContextBuffer:
    """In-memory + persisted buffer of today's outbound messages.

    Each entry:
        {
            "trigger": "cross_platform",       # which trigger produced it
            "summary": "Kalshi/PM spread on...",  # 1-line summary
            "key_data": {"spread": 12.3, ...},    # structured data for back-ref
            "timestamp": 1700000000.0,
            "was_engaged": null                  # set by ResponseTracker later
        }
    """

    def __init__(self):
        self._entries = []
        self._date = ""
        self._load()

    def _load(self):
        """Load today's context or reset if it's a new day."""
        today = datetime.now().strftime("%Y-%m-%d")
        if CONTEXT_PATH.exists():
            try:
                with open(CONTEXT_PATH, "r") as f:
                    data = json.load(f)
                if data.get("date") == today:
                    self._entries = data.get("entries", [])
                    self._date = today
                    return
            except (json.JSONDecodeError, OSError):
                pass
        # New day — fresh buffer
        self._entries = []
        self._date = today
        self._save()

    def _save(self):
        """Persist to disk (atomic write)."""
        try:
            tmp = CONTEXT_PATH.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump({"date": self._date, "entries": self._entries}, f, indent=2)
            os.replace(str(tmp), str(CONTEXT_PATH))
        except OSError:
            pass

    def record(self, trigger: str, summary: str, key_data: dict = None):
        """Record an outbound message in today's buffer."""
        self._entries.append({
            "trigger": trigger,
            "summary": summary[:200],
            "key_data": key_data or {},
            "timestamp": time.time(),
            "was_engaged": None,
        })
        # Cap at 50 entries per day (generous ceiling)
        if len(self._entries) > 50:
            self._entries = self._entries[-50:]
        self._save()

    def find_prior(self, trigger: str = None, keyword: str = None) -> Optional[dict]:
        """Find a prior message from today matching trigger or keyword.

        Returns the most recent match, or None.
        """
        for entry in reversed(self._entries):
            if trigger and entry["trigger"] == trigger:
                return entry
            if keyword and keyword.lower() in entry["summary"].lower():
                return entry
        return None

    def back_reference(self, trigger: str, current_data: dict) -> Optional[str]:
        """Generate a back-reference phrase if we sent a related message earlier.

        Returns a phrase like "That spread I flagged this morning widened to 15%"
        or None if no prior context exists.
        """
        prior = self.find_prior(trigger=trigger)
        if not prior:
            return None

        hours_ago = (time.time() - prior["timestamp"]) / 3600
        if hours_ago < 0.5:
            return None  # Too recent — feels redundant, not referential

        # Time-of-day phrasing
        if hours_ago < 4:
            time_ref = "earlier"
        elif hours_ago < 8:
            time_ref = "this morning" if prior["timestamp"] < _noon_ts() else "earlier"
        else:
            time_ref = "this morning"

        prior_data = prior.get("key_data", {})

        # Generate contextual back-reference based on trigger type
        if trigger == "cross_platform":
            old_spread = prior_data.get("max_spread")
            new_spread = current_data.get("max_spread")
            if old_spread and new_spread:
                if abs(new_spread - old_spread) >= 2:
                    direction = "widened" if new_spread > old_spread else "tightened"
                    return (
                        f"That Kalshi/PM divergence I flagged {time_ref} "
                        f"{direction} — now {new_spread:.0f}% spread"
                    )

        elif trigger == "portfolio":
            old_pnl = prior_data.get("total_pnl")
            new_pnl = current_data.get("total_pnl")
            if old_pnl is not None and new_pnl is not None:
                delta = new_pnl - old_pnl
                if abs(delta) > 5:
                    direction = "up" if delta > 0 else "down"
                    return (
                        f"Portfolio moved since I checked {time_ref} — "
                        f"{direction} ${abs(delta):.0f}"
                    )

        elif trigger == "x_signals":
            old_topic = prior_data.get("top_topic", "")
            new_topic = current_data.get("top_topic", "")
            if old_topic and old_topic == new_topic:
                return (
                    f"More signal on {old_topic} since {time_ref}"
                )

        elif trigger == "edge":
            old_ticker = prior_data.get("top_ticker", "")
            new_ticker = current_data.get("top_ticker", "")
            old_edge = prior_data.get("top_edge")
            new_edge = current_data.get("top_edge")
            if old_ticker and old_ticker == new_ticker and old_edge and new_edge:
                direction = "grew" if new_edge > old_edge else "shrank"
                return (
                    f"That {old_ticker} edge I flagged {time_ref} "
                    f"{direction} — now {new_edge:.0f}%"
                )

        return None

    def get_today_summary(self) -> str:
        """Get a brief summary of everything Rout has said today.

        Used to inject into the system prompt for conversational responses.
        """
        if not self._entries:
            return ""

        lines = []
        for entry in self._entries:
            ts = datetime.fromtimestamp(entry["timestamp"]).strftime("%I:%M%p").lstrip("0").lower()
            lines.append(f"  {ts}: [{entry['trigger']}] {entry['summary']}")

        return "Messages you sent today:\n" + "\n".join(lines[-10:])  # Last 10

    def count_today(self) -> int:
        """How many messages Rout has sent today."""
        return len(self._entries)

    def mark_engaged(self, trigger: str, engaged: bool):
        """Mark the most recent message from a trigger as engaged/ignored."""
        for entry in reversed(self._entries):
            if entry["trigger"] == trigger and entry["was_engaged"] is None:
                entry["was_engaged"] = engaged
                self._save()
                return


def _noon_ts() -> float:
    """Today's noon as a unix timestamp."""
    now = datetime.now()
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    return noon.timestamp()
