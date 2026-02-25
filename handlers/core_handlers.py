"""
Core/system handlers: help, status, ping.
These run regardless of any optional integrations.
"""

import subprocess
import yaml
import json
import time
from pathlib import Path


def help_command(args: str = "") -> str:
    """List all available commands grouped by bot"""
    try:
        config_path = Path(__file__).parent.parent / "imsg_commands.yaml"
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        commands = config.get('commands', {})
        grouped = {}
        for cmd_key, details in commands.items():
            if ':' in cmd_key:
                bot, cmd = cmd_key.split(':', 1)
            else:
                bot, cmd = 'system', cmd_key
            grouped.setdefault(bot, [])
            desc = details.get('desc', 'No description')
            grouped[bot].append(f"  {cmd}: {desc}")

        lines = ["Available commands:\n"]
        for bot in sorted(grouped.keys()):
            lines.append(f"🤖 {bot}:")
            lines.extend(grouped[bot])
            lines.append("")

        return "\n".join(lines)
    except Exception as e:
        return f"Failed to load help: {e}"


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _provider_status_line() -> str:
    try:
        now = int(time.time())
        state_path = Path.home() / ".openclaw" / "state" / "provider_failover.json"
        hint_path = Path(__file__).parent.parent / "logs" / "provider_status.json"

        active = "anthropic"
        anthropic_until = 0
        if state_path.exists():
            with open(state_path, "r") as f:
                data = json.load(f) or {}
            active = str(data.get("active_provider", "anthropic")).strip().lower() or "anthropic"
            providers = data.get("providers", {}) if isinstance(data, dict) else {}
            anthropic = providers.get("anthropic", {}) if isinstance(providers, dict) else {}
            anthropic_until = int(anthropic.get("cooldown_until", 0) or 0)

        source = "heuristic"
        if hint_path.exists():
            with open(hint_path, "r") as f:
                hint = json.load(f) or {}
            if str(hint.get("provider", "")).strip().lower() == "anthropic":
                source = str(hint.get("source", "heuristic")).strip() or "heuristic"
                anthropic_until = max(anthropic_until, int(hint.get("cooldown_eta", 0) or 0))

        remaining = max(0, anthropic_until - now)
        if remaining > 0:
            return f"Provider: {active} | Anthropic cooldown ~{_format_duration(remaining)} (source: {source})"
        return f"Provider: {active} | Anthropic ready"
    except Exception:
        return "Provider: unknown"


def status_command(args: str = "") -> str:
    """Check watcher + provider status"""
    try:
        result = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        stdout = result.stdout
        running = ("/rout/comms/imsg_watcher.py" in stdout) or ("imsg_command_watcher" in stdout)
        watcher_status = "✅ Running" if running else "⚠️ Not detected"
        return f"Watcher: {watcher_status}\n{_provider_status_line()}"
    except Exception as e:
        return f"Status check failed: {e}"


def ping_command(args: str = "") -> str:
    """Simple connectivity test"""
    return "🏓 Pong!"


def doctor_command(args: str = "", **kwargs) -> str:
    """Run installation and runtime diagnostics."""
    checks = []

    # 1. imsg CLI
    try:
        r = subprocess.run(["imsg", "--version"], capture_output=True, text=True, timeout=5)
        checks.append(f"✅ imsg CLI: {r.stdout.strip() or 'installed'}")
    except FileNotFoundError:
        checks.append("❌ imsg CLI: not found (brew install imsg)")
    except Exception as e:
        checks.append(f"⚠️ imsg CLI: {e}")

    # 2. Config
    config_path = Path(__file__).parent.parent / "config.yaml"
    if config_path.exists():
        checks.append("✅ config.yaml: present")
    else:
        checks.append("❌ config.yaml: missing (copy from config.yaml.example)")

    # 3. Memory file
    memory_path = Path.home() / ".openclaw" / "MEMORY.md"
    if memory_path.exists():
        line_count = len(memory_path.read_text(encoding="utf-8").splitlines())
        checks.append(f"✅ MEMORY.md: {line_count} lines")
    else:
        checks.append("⚠️ MEMORY.md: not found (will be created on first use)")

    # 4. API key
    try:
        from agent.providers import _get_api_key
        key = _get_api_key()
        checks.append("✅ Anthropic API key: configured" if key else "❌ Anthropic API key: not set")
    except Exception:
        checks.append("⚠️ Anthropic API key: could not check")

    # 5. Tool registry
    try:
        from agent.tool_registry import get_tool_definitions
        tool_count = len(get_tool_definitions())
        checks.append(f"✅ Tool registry: {tool_count} tools loaded")
    except Exception as e:
        checks.append(f"❌ Tool registry: {e}")

    # 6. Ollama (optional)
    try:
        r = subprocess.run(["ollama", "--version"], capture_output=True, text=True, timeout=5)
        checks.append(f"✅ Ollama: {r.stdout.strip() or 'installed'}")
    except FileNotFoundError:
        checks.append("⚙️ Ollama: not installed (optional — local LLM fallback)")
    except Exception:
        checks.append("⚙️ Ollama: could not check")

    # 7. Watcher status
    try:
        r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        running = "/rout/comms/imsg_watcher.py" in r.stdout or "imsg_command_watcher" in r.stdout
        checks.append("✅ Watcher: running" if running else "⚠️ Watcher: not detected")
    except Exception:
        checks.append("⚠️ Watcher: could not check")

    return "Rout Doctor\n" + "\n".join(checks)
