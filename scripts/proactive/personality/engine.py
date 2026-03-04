"""Personality engine — the orchestrator.

Wraps every proactive message through the full personality pipeline:
1. Context buffer check (back-reference?)
2. Selective silence check (boring enough to skip?)
3. Variable timing check (urgent enough to send now?)
4. Response tracker modifier (user engages with this type?)
5. Editorial voice (add opinion)
6. Context buffer record (remember what we said)
7. Response tracker record (mark as pending engagement)
8. Micro-initiation check (ambient awareness)

This replaces the direct _send_message() calls in trigger modules.
Triggers still detect conditions — but personality decides HOW and WHETHER to speak.
"""

import time
from datetime import datetime
from typing import Optional

from proactive.personality.context_buffer import ContextBuffer
from proactive.personality.editorial_voice import editorialize
from proactive.personality.variable_timing import (
    compute_urgency, should_send_now, record_send as record_timing_send,
)
from proactive.personality.selective_silence import (
    should_stay_silent, silence_message, should_send_silence_message,
    record_silence,
)
from proactive.personality.micro_initiations import (
    get_micro_initiation, record_micro_initiation,
)
from proactive.personality.response_tracker import ResponseTracker
from proactive.base import _send_message, _record_send, _log, _is_duplicate_message


# ── Singletons (initialized once per proactive_agent.py run) ─────────────────

_context_buffer: Optional[ContextBuffer] = None
_response_tracker: Optional[ResponseTracker] = None


def init():
    """Initialize personality singletons. Call once at start of proactive run."""
    global _context_buffer, _response_tracker
    _context_buffer = ContextBuffer()
    _response_tracker = ResponseTracker()
    # Check for expired engagements from previous run
    _response_tracker.check_expired_engagements()


def get_context_buffer() -> ContextBuffer:
    global _context_buffer
    if _context_buffer is None:
        _context_buffer = ContextBuffer()
    return _context_buffer


def get_response_tracker() -> ResponseTracker:
    global _response_tracker
    if _response_tracker is None:
        _response_tracker = ResponseTracker()
    return _response_tracker


def personality_send(
    trigger: str,
    message: str,
    data: dict,
    state: dict,
    dry_run: bool = False,
    bypass_personality: bool = False,
) -> bool:
    """The personality-aware send function.

    Replaces direct _send_message() calls in trigger modules.

    Args:
        trigger: Trigger name (cross_platform, portfolio, etc.)
        message: The raw message text from the trigger
        data: Structured data from the trigger (for editorial + context)
        state: Proactive agent state dict (mutable)
        dry_run: If True, log but don't send
        bypass_personality: If True, skip all personality (for forced/manual runs)

    Returns:
        True if a message was sent, False if suppressed.
    """
    ctx = get_context_buffer()
    tracker = get_response_tracker()

    if bypass_personality:
        if _send_message(message, dry_run=dry_run):
            _record_send(state, message)
            ctx.record(trigger, message[:200], data)
            return True
        return False

    # ── 1. Selective silence check ────────────────────────────────
    if should_stay_silent(trigger, data, state):
        _log(f"[personality] {trigger}: content too thin — staying silent")

        # Should we send a deliberate silence message?
        if should_send_silence_message(trigger, state):
            silence_msg = silence_message(trigger, data)
            if silence_msg:
                if not _is_duplicate_message(state, silence_msg):
                    _log(f"[personality] Sending silence message for {trigger}")
                    if _send_message(silence_msg, dry_run=dry_run):
                        record_silence(trigger, state)
                        _record_send(state, silence_msg)
                        ctx.record(trigger + "_silence", silence_msg[:200])
                        record_timing_send(state)
                        return True
        return False

    # ── 2. Compute urgency ────────────────────────────────────────
    urgency = compute_urgency(trigger, data)

    # ── 3. Apply engagement modifier ──────────────────────────────
    engagement_mod = tracker.get_urgency_modifier(trigger)
    adjusted_urgency = urgency * engagement_mod
    _log(
        f"[personality] {trigger}: urgency={urgency:.2f} "
        f"x engagement_mod={engagement_mod:.2f} "
        f"= {adjusted_urgency:.2f}"
    )

    # ── 4. Variable timing check ──────────────────────────────────
    if not should_send_now(adjusted_urgency, state):
        _log(f"[personality] {trigger}: timing says hold (urgency {adjusted_urgency:.2f})")
        return False

    # ── 5. Back-reference from context buffer ─────────────────────
    back_ref = ctx.back_reference(trigger, data)
    if back_ref:
        message = f"{back_ref}\n\n{message}"
        _log(f"[personality] Added back-reference: {back_ref[:60]}...")

    # ── 6. Editorial voice ────────────────────────────────────────
    try:
        message = editorialize(trigger, data, message)
    except Exception as e:
        _log(f"[personality] editorialize failed for {trigger}: {e} — sending raw message")

    # ── 7. Dedup check ────────────────────────────────────────────
    if _is_duplicate_message(state, message):
        _log(f"[personality] {trigger}: duplicate detected — skipping")
        return False

    # ── 8. Send ───────────────────────────────────────────────────
    if _send_message(message, dry_run=dry_run):
        _record_send(state, message)
        record_timing_send(state)
        ctx.record(trigger, message[:200], data)
        tracker.record_send(trigger)

        # Record state for stale-detection in selective_silence
        if trigger == "cross_platform":
            state["last_divergence_market"] = data.get("market_key", "")
            state["last_divergence_spread"] = data.get("max_spread", 0)
        elif trigger == "x_signals":
            state["last_x_signal_topics"] = data.get("topics", [])

        # Mark trigger as having sent today
        today = datetime.now().strftime("%Y-%m-%d")
        state[f"last_{trigger}_sent_date"] = today

        _log(f"[personality] {trigger}: sent with personality")
        return True

    return False


def check_micro_initiations(state: dict, dry_run: bool = False) -> bool:
    """Check if a micro-initiation should be sent.

    Called at the end of each proactive run, after all triggers.
    """
    ctx = get_context_buffer()

    micro = get_micro_initiation(state)
    if micro is None:
        return False

    if _is_duplicate_message(state, micro):
        return False

    _log(f"[personality] Micro-initiation: {micro[:60]}...")

    if _send_message(micro, dry_run=dry_run):
        record_micro_initiation(state, micro)
        _record_send(state, micro)
        record_timing_send(state)
        ctx.record("micro", micro[:200])
        return True

    return False


def check_adjustment_suggestions(state: dict, dry_run: bool = False) -> bool:
    """Check if we should suggest dialing back a trigger type.

    Based on long-term engagement data.
    """
    tracker = get_response_tracker()

    for trigger in ["cross_platform", "portfolio", "x_signals", "edge", "morning"]:
        suggestion = tracker.should_suggest_adjustment(trigger)
        if suggestion:
            _log(f"[personality] Suggesting adjustment for {trigger}")
            if _send_message(suggestion, dry_run=dry_run):
                _record_send(state, suggestion)
                return True

    return False
