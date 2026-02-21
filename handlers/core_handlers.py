"""Core command handlers: help, status, ping."""

import os
import subprocess
import time
import yaml


def help_command(args=None):
    """Display available commands grouped by prefix."""
    config_path = os.path.expanduser("~/.openclaw/hardened/imsg_commands.yaml")
    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        return f"Error loading commands: {e}"

    commands = config.get("commands", {})
    groups = {}
    for key, cmd in commands.items():
        prefix = key.split(":")[0] if ":" in key else "core"
        if prefix not in groups:
            groups[prefix] = []
        trigger = cmd.get("trigger", key)
        desc = cmd.get("description", "No description")
        groups[prefix].append(f"  {trigger} — {desc}")

    lines = ["Rout Commands:", ""]
    for group, cmds in sorted(groups.items()):
        lines.append(f"[{group.upper()}]")
        lines.extend(cmds)
        lines.append("")

    lines.append("Any other message goes to Claude.")
    return "\n".join(lines)


def status_command(args=None):
    """Check system status: watcher, config, logs, circuit breaker."""
    checks = []

    # Watcher process
    try:
        result = subprocess.run(
            ["pgrep", "-f", "imsg_watcher"],
            capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            checks.append(f"Watcher: RUNNING (PID {pids[0]})")
        else:
            checks.append("Watcher: NOT RUNNING")
    except Exception:
        checks.append("Watcher: UNKNOWN")

    # Config file
    config_path = os.path.expanduser("~/.openclaw/config.yaml")
    if os.path.exists(config_path):
        checks.append("Config: OK")
    else:
        checks.append("Config: MISSING")

    # Commands config
    cmd_path = os.path.expanduser("~/.openclaw/hardened/imsg_commands.yaml")
    if os.path.exists(cmd_path):
        checks.append("Commands: OK")
    else:
        checks.append("Commands: MISSING")

    # Log freshness
    log_path = os.path.expanduser("~/.openclaw/logs/imsg_watcher.log")
    if os.path.exists(log_path):
        age = time.time() - os.path.getmtime(log_path)
        if age < 300:
            checks.append(f"Logs: FRESH ({int(age)}s ago)")
        else:
            checks.append(f"Logs: STALE ({int(age/60)}m ago)")
    else:
        checks.append("Logs: NO LOG FILE")

    # Circuit breaker
    cb_path = os.path.expanduser("~/.openclaw/hardened/.circuit_breaker")
    if os.path.exists(cb_path):
        try:
            with open(cb_path, "r") as f:
                cb = yaml.safe_load(f)
            state = cb.get("state", "unknown")
            checks.append(f"Circuit Breaker: {state.upper()}")
        except Exception:
            checks.append("Circuit Breaker: ERROR READING")
    else:
        checks.append("Circuit Breaker: CLOSED (default)")

    # Handlers directory
    handlers_path = os.path.expanduser("~/.openclaw/hardened/handlers")
    if os.path.isdir(handlers_path):
        handler_files = [f for f in os.listdir(handlers_path) if f.endswith(".py") and f != "__init__.py"]
        checks.append(f"Handlers: {len(handler_files)} loaded")
    else:
        checks.append("Handlers: NOT INSTALLED")

    return "System Status:\n" + "\n".join(checks)


def ping_command(args=None):
    """Simple connectivity check."""
    return "Pong!"
