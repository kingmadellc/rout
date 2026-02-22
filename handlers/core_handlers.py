"""Core command handlers: help, status, ping, doctor."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import yaml


OPENCLAW_DIR = Path.home() / ".openclaw"


def _workspace_root() -> Path:
    env_workspace = os.environ.get("ROUT_WORKSPACE", "").strip()
    if env_workspace:
        p = Path(env_workspace).expanduser()
        if p.exists():
            return p
    return Path(__file__).resolve().parent.parent


def _commands_config_path() -> Path:
    workspace = _workspace_root()
    workspace_cfg = workspace / "imsg_commands.yaml"
    if workspace_cfg.exists():
        return workspace_cfg
    return OPENCLAW_DIR / "hardened" / "imsg_commands.yaml"


def _load_commands_config() -> dict:
    path = _commands_config_path()
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def _find_imsg_bin() -> str:
    cfg = {}
    cfg_path = OPENCLAW_DIR / "config.yaml"
    if cfg_path.exists():
        try:
            with open(cfg_path, "r") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}

    candidates = [
        cfg.get("paths", {}).get("imsg", ""),
        shutil.which("imsg"),
        "/opt/homebrew/bin/imsg",
        "/usr/local/bin/imsg",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "imsg"


def help_command(args=None):
    """Display available commands grouped by prefix."""
    try:
        config = _load_commands_config()
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
    config_path = OPENCLAW_DIR / "config.yaml"
    if config_path.exists():
        checks.append("Config: OK")
    else:
        checks.append("Config: MISSING")

    # Commands config
    cmd_path = _commands_config_path()
    if cmd_path.exists():
        checks.append(f"Commands: OK ({cmd_path})")
    else:
        checks.append("Commands: MISSING")

    # Log freshness
    log_path = OPENCLAW_DIR / "logs" / "imsg_watcher.log"
    if log_path.exists():
        age = time.time() - log_path.stat().st_mtime
        if age < 300:
            checks.append(f"Logs: FRESH ({int(age)}s ago)")
        else:
            checks.append(f"Logs: STALE ({int(age / 60)}m ago)")
    else:
        checks.append("Logs: NO LOG FILE")

    # Circuit breaker
    cb_path = OPENCLAW_DIR / "state" / "circuit_breaker.json"
    if cb_path.exists():
        try:
            with open(cb_path, "r") as f:
                cb = yaml.safe_load(f) or {}
            cooldown_until = cb.get("cooldown_until", 0)
            trip_count = cb.get("trip_count", 0)
            now = time.time()
            if cooldown_until > now:
                checks.append(f"Circuit Breaker: OPEN (trip #{trip_count})")
            else:
                checks.append("Circuit Breaker: CLOSED")
        except Exception:
            checks.append("Circuit Breaker: ERROR READING")
    else:
        checks.append("Circuit Breaker: CLOSED (default)")

    # Handlers directory
    handlers_path = _workspace_root() / "handlers"
    if handlers_path.is_dir():
        handler_files = [
            f for f in os.listdir(handlers_path)
            if f.endswith(".py") and f != "__init__.py"
        ]
        checks.append(f"Handlers: {len(handler_files)} loaded")
    else:
        checks.append("Handlers: NOT INSTALLED")

    return "System Status:\n" + "\n".join(checks)


def doctor_command(args=None):
    """Run installation and runtime diagnostics."""
    checks = []

    # Tooling
    imsg_bin = _find_imsg_bin()
    checks.append(("imsg binary", shutil.which(imsg_bin) is not None or os.path.exists(imsg_bin), imsg_bin))

    osascript = shutil.which("osascript") or "/usr/bin/osascript"
    checks.append(("osascript", os.path.exists(osascript), osascript))

    # Config
    config_path = OPENCLAW_DIR / "config.yaml"
    config_ok = False
    config_details = str(config_path)
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                cfg = yaml.safe_load(f) or {}
            config_ok = isinstance(cfg, dict)
            if not cfg.get("anthropic_api_key"):
                config_details += " (missing anthropic_api_key)"
        except Exception as e:
            config_details += f" (parse error: {e})"
    checks.append(("config.yaml", config_ok, config_details))

    # Commands
    cmd_path = _commands_config_path()
    commands_ok = False
    cmd_details = str(cmd_path)
    if cmd_path.exists():
        try:
            with open(cmd_path, "r") as f:
                data = yaml.safe_load(f) or {}
            commands_ok = isinstance(data.get("commands"), dict)
            cmd_details += f" ({len(data.get('commands', {}))} commands)"
        except Exception as e:
            cmd_details += f" (parse error: {e})"
    checks.append(("command registry", commands_ok, cmd_details))

    # Handlers
    handlers_dir = _workspace_root() / "handlers"
    handler_files = []
    if handlers_dir.is_dir():
        handler_files = [
            f for f in os.listdir(handlers_dir)
            if f.endswith(".py") and f != "__init__.py"
        ]
    checks.append(("handler modules", len(handler_files) > 0, f"{handlers_dir} ({len(handler_files)} files)"))

    # Memory
    memory_path = OPENCLAW_DIR / "MEMORY.md"
    checks.append(("memory file", memory_path.exists(), str(memory_path)))

    # Compose report
    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)

    lines = [f"Rout Doctor: {passed}/{total} checks passed", ""]
    for label, ok, details in checks:
        icon = "PASS" if ok else "FAIL"
        lines.append(f"{icon} | {label}: {details}")

    if passed < total:
        lines.append("")
        lines.append("Fix failing items, then run 'doctor' again.")

    return "\n".join(lines)


def ping_command(args=None):
    """Simple connectivity check."""
    return "Pong!"
