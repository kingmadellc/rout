"""
Subagent spawning handlers for delegating work with completion notifications.
"""

import subprocess
import json
import time
from pathlib import Path


def spawn_subagent_command(args: str = "") -> str:
    """
    Spawn a subagent to work on a task and iMessage when complete.

    Usage:
      spawn: <task description>
      subagent: <task description>

    Examples:
      spawn: research the latest AI policy news and summarize
      subagent: check all my calendar events for next week and find conflicts
    """
    if not args or not args.strip():
        return "❌ Please provide a task description.\n\nUsage: spawn: <task>"

    task = args.strip()

    # Augment task to ensure iMessage notification on completion
    full_task = f"""{task}

After completing this task, send an iMessage to chat_id 1 with a concise summary of what you did and the outcome. Use the imsg CLI tool.

Example:
  imsg send --chat-id 1 "✅ Task complete: [brief summary of work done]"
"""

    try:
        # Spawn the subagent via OpenClaw CLI
        cmd = [
            "openclaw",
            "sessions:spawn",
            "--mode=run",
            "--task", full_task,
            "--timeout", "600"  # 10 minute timeout
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            return f"❌ Failed to spawn subagent:\n{result.stderr}"

        # Parse the response to get session info
        try:
            response = json.loads(result.stdout)
            session_key = response.get("childSessionKey", "unknown")
            run_id = response.get("runId", "unknown")

            return f"""✅ Subagent spawned!

Task: {task}

Session: {session_key[:20]}...
Run ID: {run_id[:8]}...

You'll get an iMessage when it's done. (Timeout: 10 min)"""
        except json.JSONDecodeError:
            # Fallback if response isn't JSON
            return f"""✅ Subagent spawned!

Task: {task}

You'll get an iMessage when it's done. (Timeout: 10 min)"""

    except subprocess.TimeoutExpired:
        return "❌ Spawn command timed out (took >30s)"
    except FileNotFoundError:
        return "❌ openclaw CLI not found - check installation"
    except Exception as e:
        return f"❌ Error spawning subagent: {e}"


def list_subagents_command(args: str = "") -> str:
    """
    List active and recent subagents.

    Usage:
      subagents
      subagents list
    """
    try:
        cmd = [
            "openclaw",
            "subagents",
            "--action=list",
            "--recent-minutes=60"
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return f"❌ Failed to list subagents:\n{result.stderr}"

        try:
            response = json.loads(result.stdout)
            active = response.get("active", [])
            recent = response.get("recent", [])

            lines = []

            if active:
                lines.append("🔄 Active subagents:")
                for sub in active:
                    label = sub.get("label", "unlabeled")
                    lines.append(f"  • {label}")
                lines.append("")

            if recent:
                lines.append("📋 Recent (last hour):")
                for sub in recent:
                    label = sub.get("label", "unlabeled")
                    status = sub.get("status", "unknown")
                    lines.append(f"  • {label} - {status}")

            if not active and not recent:
                return "No active or recent subagents."

            return "\n".join(lines)

        except json.JSONDecodeError:
            # Fallback - just return raw output
            return result.stdout or "No subagents found."

    except subprocess.TimeoutExpired:
        return "❌ List command timed out"
    except FileNotFoundError:
        return "❌ openclaw CLI not found"
    except Exception as e:
        return f"❌ Error listing subagents: {e}"
