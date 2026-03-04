"""Personality layer — makes Rout feel alive.

Six systems that transform Rout from a notification pipeline
into something that feels like a presence.

1. Context Buffer    — remembers what it said today, back-references prior messages
2. Editorial Voice   — adds opinion/tone to data, not just reporting
3. Variable Timing   — rhythm instead of cron, urgency-responsive
4. Selective Silence  — knows when NOT to talk, earns trust through restraint
5. Micro-Initiations — ambient awareness, non-data-driven pings
6. Response Tracking  — adapts to what Matt engages with vs. ignores
"""

from proactive.personality.context_buffer import ContextBuffer
from proactive.personality.editorial_voice import editorialize
from proactive.personality.variable_timing import should_send_now, compute_urgency
from proactive.personality.selective_silence import should_stay_silent, silence_message
from proactive.personality.micro_initiations import get_micro_initiation
from proactive.personality.response_tracker import ResponseTracker

__all__ = [
    "ContextBuffer",
    "editorialize",
    "should_send_now",
    "compute_urgency",
    "should_stay_silent",
    "silence_message",
    "get_micro_initiation",
    "ResponseTracker",
]
