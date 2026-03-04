#!/usr/bin/env python3
"""
Rout Agent Runner — Executes specialist agents and delivers results via iMessage.

This is the execution bridge between your skill knowledge and Rout's iMessage delivery.
It runs a claude CLI session with the right skill pre-loaded, captures the output,
and pushes it through Rout's existing BlueBubbles pipeline.

Usage:
    # Run an agent with a prompt
    python3 rout-agent-runner.py --agent integration-scaffolder --prompt "Scaffold a weather API integration"

    # Run an agent and push result to iMessage
    python3 rout-agent-runner.py --agent debug-tracer --prompt "Diagnose why morning brief didn't fire" --push

    # Run with specific model
    python3 rout-agent-runner.py --agent deploy-ops --prompt "Generate deploy checklist for Coinbase" --model sonnet

    # Dry run (print output, don't push)
    python3 rout-agent-runner.py --agent debug-tracer --prompt "Check watcher health" --dry-run
"""

import argparse
import json
import os
import subprocess
import sys
import time
import yaml
from datetime import datetime
from pathlib import Path

# Import activity writer (same directory)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from importlib.util import spec_from_file_location, module_from_spec
    _writer_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rout-activity-writer.py")
    _spec = spec_from_file_location("rout_activity_writer", _writer_path)
    _writer_mod = module_from_spec(_spec)
    _spec.loader.exec_module(_writer_mod)
    write_breadcrumb = _writer_mod.write_breadcrumb
except Exception:
    # Fallback: no-op if writer not available
    def write_breadcrumb(*args, **kwargs):
        return "no-writer"

# === CONFIGURATION ===

# Agent name → skill directory mapping
AGENTS = {
    "integration-scaffolder": "rout-integration-scaffolder",
    "scaffolder": "rout-integration-scaffolder",
    "pipeline-specialist": "rout-pipeline-specialist",
    "pipeline": "rout-pipeline-specialist",
    "deploy-ops": "rout-deploy-ops",
    "deploy": "rout-deploy-ops",
    "ops": "rout-deploy-ops",
    "debug-tracer": "rout-debug-tracer",
    "debug": "rout-debug-tracer",
    "tracer": "rout-debug-tracer",
}

# Where skills live (relative to this script, or override with ROUT_SKILLS_DIR env var)
SKILLS_DIR = os.environ.get(
    "ROUT_SKILLS_DIR",
    os.path.dirname(os.path.abspath(__file__))
)

# Rout config for iMessage delivery
ROUT_CONFIG_PATH = os.path.expanduser("~/.openclaw/config.yaml")


def load_skill(agent_name: str) -> str:
    """Load the SKILL.md content for an agent."""
    skill_dir = AGENTS.get(agent_name)
    if not skill_dir:
        available = ", ".join(sorted(set(AGENTS.values())))
        raise ValueError(f"Unknown agent '{agent_name}'. Available: {available}")

    skill_path = Path(SKILLS_DIR) / skill_dir / "SKILL.md"
    if not skill_path.exists():
        raise FileNotFoundError(f"Skill file not found: {skill_path}")

    return skill_path.read_text()


def load_rout_config() -> dict:
    """Load Rout's config for iMessage delivery."""
    try:
        with open(ROUT_CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def run_agent(agent_name: str, prompt: str, model: str = "sonnet",
              rout_dir: str = None, timeout: int = 300) -> str:
    """
    Run a claude CLI session with skill context pre-loaded.

    Returns the agent's text output.
    """
    skill_content = load_skill(agent_name)

    # Build the system prompt with the skill baked in
    system_prompt = f"""You are a Rout development specialist agent. You have deep knowledge
of Rout's architecture loaded below. Follow these instructions precisely.

{skill_content}

IMPORTANT: You are running in automated mode. Be concise, actionable, and output-ready.
No conversational pleasantries. Just the work product."""

    # Build the claude CLI command
    # --dangerously-skip-permissions is required for autonomous/headless execution
    # so agents can actually run bash commands, read files, etc. without human approval.
    # Safe here because: prompts are controlled by us, runs against our own codebase.
    # Use absolute path for claude CLI — required for launchd and bash contexts
    # where PATH may not include /usr/local/bin
    claude_bin = "/usr/local/bin/claude"
    if not os.path.exists(claude_bin):
        # Fallback to PATH lookup
        import shutil
        claude_bin = shutil.which("claude") or "claude"

    cmd = [
        claude_bin,
        "-p",  # print mode (non-interactive)
        "--dangerously-skip-permissions",
        "--system-prompt", system_prompt,
        "--model", model,
        "--output-format", "text",
    ]

    # If we have a Rout project directory, give claude access
    if rout_dir:
        cmd.extend(["--add-dir", rout_dir])

    # Prompt goes via stdin (not positional arg) to avoid CLI parsing
    # issues when --system-prompt is very large
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=rout_dir or os.getcwd()
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or "Unknown error"
            return f"AGENT ERROR ({agent_name}): {error_msg}"

        return result.stdout.strip()

    except subprocess.TimeoutExpired:
        return f"AGENT TIMEOUT ({agent_name}): Exceeded {timeout}s"
    except FileNotFoundError:
        return "ERROR: 'claude' CLI not found. Install Claude Code first."


def push_to_imessage(message: str, config: dict = None) -> bool:
    """
    Send agent output via Rout's BlueBubbles pipeline.
    Uses the same iMessage delivery that Rout uses for morning briefs.
    """
    import requests

    if not config:
        config = load_rout_config()

    bb_config = config.get("bluebubbles", {})
    bb_url = bb_config.get("url", "http://localhost:1234")
    bb_password = bb_config.get("password", "")

    brief_config = config.get("morning_brief", {})
    chat_guid = brief_config.get("recipient_chat_guid", "")

    if not bb_password or not chat_guid:
        print("WARNING: BlueBubbles not configured. Skipping iMessage push.", file=sys.stderr)
        return False

    # Truncate if too long for iMessage readability
    if len(message) > 2000:
        message = message[:1950] + "\n\n... (truncated, full output in logs)"

    try:
        resp = requests.post(
            f"{bb_url}/api/v1/message/text",
            json={
                "chatGuid": chat_guid,
                "message": message,
                "method": "apple-script"
            },
            params={"password": bb_password},
            timeout=10
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"WARNING: iMessage push failed: {e}", file=sys.stderr)
        return False


def format_agent_output(agent_name: str, prompt: str, output: str) -> str:
    """Format the agent output for iMessage delivery."""
    timestamp = datetime.now().strftime("%H:%M")
    header = f"[Rout Agent: {agent_name} @ {timestamp}]"
    return f"{header}\n\n{output}"


def main():
    parser = argparse.ArgumentParser(description="Run Rout specialist agents")
    parser.add_argument("--agent", "-a", required=True,
                       help="Agent to run (integration-scaffolder, pipeline-specialist, deploy-ops, debug-tracer)")
    parser.add_argument("--prompt", "-p", required=True,
                       help="Task prompt for the agent")
    parser.add_argument("--push", action="store_true",
                       help="Push result to iMessage via Rout")
    parser.add_argument("--dry-run", action="store_true",
                       help="Print output without pushing to iMessage")
    parser.add_argument("--model", "-m", default="sonnet",
                       help="Claude model to use (default: sonnet)")
    parser.add_argument("--rout-dir", default=None,
                       help="Path to Rout project directory")
    parser.add_argument("--timeout", "-t", type=int, default=300,
                       help="Timeout in seconds (default: 300)")
    parser.add_argument("--log", action="store_true",
                       help="Write output to log file")

    args = parser.parse_args()

    # Normalize agent name for breadcrumbs (strip prefixes like "integration-")
    # Map to canonical short names used by the monitor
    _agent_canonical = {
        "integration-scaffolder": "scaffolder", "scaffolder": "scaffolder",
        "pipeline-specialist": "pipeline", "pipeline": "pipeline",
        "deploy-ops": "deploy", "deploy": "deploy", "ops": "deploy",
        "debug-tracer": "debug", "debug": "debug", "tracer": "debug",
    }
    canonical = _agent_canonical.get(args.agent, args.agent)

    # Write "running" breadcrumb
    session_id = write_breadcrumb(
        agent=canonical,
        status="running",
        prompt=args.prompt,
        source="launchd",
    )

    # Run the agent
    print(f"Running agent: {args.agent}...", file=sys.stderr)
    start_time = time.time()
    output = run_agent(
        agent_name=args.agent,
        prompt=args.prompt,
        model=args.model,
        rout_dir=args.rout_dir,
        timeout=args.timeout
    )
    elapsed = round(time.time() - start_time, 1)

    # Determine final status from output
    if output.startswith("AGENT ERROR"):
        final_status = "failed"
    elif output.startswith("AGENT TIMEOUT"):
        final_status = "timeout"
    elif output.startswith("ERROR:"):
        final_status = "failed"
    else:
        final_status = "completed"

    # Write completion breadcrumb
    write_breadcrumb(
        agent=canonical,
        status=final_status,
        prompt=args.prompt,
        source="launchd",
        session_id=session_id,
        duration_s=elapsed,
    )

    # Format for delivery
    formatted = format_agent_output(args.agent, args.prompt, output)

    # Always print to stdout
    print(formatted)

    # Log if requested
    if args.log:
        log_dir = Path.home() / "Library" / "Logs"
        log_file = log_dir / f"rout-agent-{args.agent}.log"
        with open(log_file, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"Time: {datetime.now().isoformat()}\n")
            f.write(f"Prompt: {args.prompt}\n")
            f.write(f"{'='*60}\n")
            f.write(formatted)
            f.write("\n")

    # Push to iMessage if requested
    if args.push and not args.dry_run:
        success = push_to_imessage(formatted)
        if success:
            print("Pushed to iMessage.", file=sys.stderr)
        else:
            print("iMessage push failed. Output printed above.", file=sys.stderr)


if __name__ == "__main__":
    main()
