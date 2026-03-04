#!/usr/bin/env python3
"""
Rout Activity Writer — Writes agent activity breadcrumbs to a shared JSONL file.

Any agent invocation (launchd, Cowork, Claude Code, manual) can call this
to register its activity. The System Monitor polls this file for live status.

Schema (one JSON object per line):
    {
        "agent": "debug",           # Agent short name
        "status": "running",        # running | completed | failed | timeout
        "timestamp": "ISO8601",     # When this breadcrumb was written
        "epoch": 1709337600.0,      # Unix epoch for easy comparison
        "prompt": "Check health",   # What the agent was asked to do
        "source": "cowork",         # cowork | launchd | manual | claude-code
        "session_id": "abc123",     # Unique per-invocation (ties start→complete)
        "duration_s": null           # Seconds elapsed (set on complete/fail)
    }

Usage:
    # Mark agent as running
    python3 rout-activity-writer.py --agent debug --status running --prompt "Check health" --source cowork

    # Mark agent as completed (with duration)
    python3 rout-activity-writer.py --agent debug --status completed --session-id abc123 --duration 45

    # As a Python import
    from rout_activity_writer import write_breadcrumb
    write_breadcrumb("debug", "running", prompt="Check health", source="cowork")
"""

import argparse
import fcntl
import json
import os
import sys
import uuid
from datetime import datetime

ACTIVITY_FILE = os.path.expanduser("~/.openclaw/logs/agent-activity.jsonl")
MAX_FILE_SIZE = 512 * 1024  # 512KB — rotate when exceeded
ROTATED_FILE = ACTIVITY_FILE + ".old"


def write_breadcrumb(
    agent: str,
    status: str,
    prompt: str = None,
    source: str = "manual",
    session_id: str = None,
    duration_s: float = None,
) -> str:
    """
    Append a breadcrumb to agent-activity.jsonl.
    Returns the session_id (generated if not provided).
    """
    if not session_id:
        session_id = uuid.uuid4().hex[:12]

    now = datetime.now()
    entry = {
        "agent": agent,
        "status": status,
        "timestamp": now.isoformat(),
        "epoch": now.timestamp(),
        "prompt": prompt,
        "source": source,
        "session_id": session_id,
        "duration_s": duration_s,
    }

    line = json.dumps(entry, separators=(",", ":")) + "\n"

    # Ensure directory exists
    os.makedirs(os.path.dirname(ACTIVITY_FILE), exist_ok=True)

    # Rotate if file is too large
    try:
        if os.path.isfile(ACTIVITY_FILE) and os.path.getsize(ACTIVITY_FILE) > MAX_FILE_SIZE:
            if os.path.isfile(ROTATED_FILE):
                os.remove(ROTATED_FILE)
            os.rename(ACTIVITY_FILE, ROTATED_FILE)
    except Exception:
        pass  # Non-critical — just keep writing

    # Atomic append with file locking
    try:
        with open(ACTIVITY_FILE, "a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(line)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"WARNING: Failed to write breadcrumb: {e}", file=sys.stderr)

    return session_id


def main():
    parser = argparse.ArgumentParser(description="Write agent activity breadcrumbs")
    parser.add_argument("--agent", "-a", required=True,
                        help="Agent short name (debug, scaffolder, pipeline, deploy)")
    parser.add_argument("--status", "-s", required=True,
                        choices=["running", "completed", "failed", "timeout"],
                        help="Agent status")
    parser.add_argument("--prompt", "-p", default=None,
                        help="Task prompt (for running status)")
    parser.add_argument("--source", default="manual",
                        choices=["cowork", "launchd", "manual", "claude-code"],
                        help="Invocation source (default: manual)")
    parser.add_argument("--session-id", default=None,
                        help="Session ID (auto-generated if not provided)")
    parser.add_argument("--duration", type=float, default=None,
                        help="Duration in seconds (for completed/failed)")

    args = parser.parse_args()

    sid = write_breadcrumb(
        agent=args.agent,
        status=args.status,
        prompt=args.prompt,
        source=args.source,
        session_id=args.session_id,
        duration_s=args.duration,
    )

    print(json.dumps({"session_id": sid, "status": args.status, "agent": args.agent}))


if __name__ == "__main__":
    main()
