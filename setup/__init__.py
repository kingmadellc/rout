"""
Rout Setup Wizard — interactive first-run configuration.

Supports two modes:
  Cloud  — Anthropic Claude via API (requires API key)
  Local  — Qwen via Ollama on-device (no API key, no cloud dependency)

Collects user info, validates dependencies, geocodes location,
configures the chosen provider, and writes ~/.openclaw/config.yaml.

Safe to re-run: preserves existing values as defaults.
"""

from .config import load_existing_config, write_config
from .checks import check_core_dependencies, check_cloud_dependencies, check_local_dependencies
from .ui import ok, warn, fail, ask, ask_choice
from .keys import setup_user_info, setup_location, setup_personality, setup_imessage
from .launchd import setup_ollama_autostart
from .config import setup_local_provider, setup_cloud_provider


def main():
    """Main setup wizard entry point."""
    import sys
    from pathlib import Path
    import shutil
    from .ui import BOLD, YELLOW, GREEN, RED, NC, DIM

    OPENCLAW_DIR = Path.home() / ".openclaw"
    CONFIG_FILE = OPENCLAW_DIR / "config.yaml"
    MEMORY_FILE = OPENCLAW_DIR / "MEMORY.md"
    SCRIPT_DIR = Path(__file__).resolve().parent.parent

    print(f"\n{BOLD}Rout — Setup Wizard{NC}")
    print("=" * 50)
    print(f"{DIM}This creates ~/.openclaw/config.yaml with your personal settings.{NC}")
    print(f"{DIM}Safe to re-run — your existing values become defaults.{NC}\n")

    # Ensure directories
    OPENCLAW_DIR.mkdir(parents=True, exist_ok=True)
    (OPENCLAW_DIR / "state").mkdir(exist_ok=True)
    (OPENCLAW_DIR / "logs").mkdir(exist_ok=True)
    (OPENCLAW_DIR / "keys").mkdir(exist_ok=True)

    # ── Step 1: Mode Selection ────────────────────────────────────────────────

    existing = load_existing_config()
    ex_mode = "local" if existing.get("local_only") else "cloud"

    # Detect default based on existing config
    default_mode_idx = 2 if ex_mode == "local" else 1

    mode = ask_choice(
        "How should Rout think?",
        [
            ("Cloud — Anthropic Claude via API (best quality, requires API key + internet)",
             "cloud"),
            ("Local — Qwen on-device via Ollama (free, private, no internet needed for inference)",
             "local"),
        ],
        default=default_mode_idx,
    )

    is_local = (mode == "local")

    # ── Step 2: Check Dependencies ────────────────────────────────────────────

    print(f"\n{BOLD}Checking dependencies...{NC}\n")
    core_issues, core_warnings = check_core_dependencies()

    if is_local:
        local_issues, ollama_installed = check_local_dependencies()
        all_issues = core_issues + local_issues
    else:
        cloud_issues = check_cloud_dependencies()
        all_issues = core_issues + cloud_issues

    if core_warnings:
        print()
        for w in core_warnings:
            warn(w)

    if all_issues:
        print()
        for i in all_issues:
            fail(i)
        print(f"\n{RED}Fix the issues above and re-run setup.{NC}")
        sys.exit(1)
    print()

    # Now we can import yaml
    import yaml

    ex_user = existing.get("user", {})
    ex_paths = existing.get("paths", {})
    ex_handles = existing.get("chat_handles", {})
    ex_senders = existing.get("known_senders", {})

    # ── Step 3: User Info (shared) ────────────────────────────────────────────

    name, phone = setup_user_info(existing)
    location_str, lat, lon, tz = setup_location(existing)
    personality = setup_personality(existing)

    # ── Step 4: iMessage (shared) ─────────────────────────────────────────────

    personal_chat_id, group_ids, extra_handles, extra_senders_discovered = \
        setup_imessage(existing, phone)

    # ── Step 5: Provider Setup (branched) ─────────────────────────────────────

    api_key = None
    cloud_config = None
    local_config = None
    cloud_model = None
    model_name = None

    if is_local:
        model_name, local_config = setup_local_provider(existing)
    else:
        api_key, cloud_model, cloud_config = setup_cloud_provider(existing)

    # ── Step 6: Build Config ──────────────────────────────────────────────────

    imsg_path = shutil.which("imsg") or "/opt/homebrew/bin/imsg"

    # Chat handles
    chat_handles = {int(personal_chat_id): [phone, "buddy"]}
    for k, v in extra_handles.items():
        if k not in chat_handles:
            chat_handles[k] = v
    if ex_handles:
        for k, v in ex_handles.items():
            k_int = int(k)
            if k_int not in chat_handles:
                chat_handles[k_int] = v

    # Known senders
    known_senders = {phone: name}
    for k, v in extra_senders_discovered.items():
        if k not in known_senders:
            known_senders[k] = v
    if ex_senders:
        for k, v in ex_senders.items():
            if k not in known_senders:
                known_senders[k] = v

    config = {
        "user": {
            "name": name,
            "location": location_str,
            "latitude": round(lat, 4),
            "longitude": round(lon, 4),
            "timezone": tz,
            "personality": personality,
            "assistant_name": ex_user.get("assistant_name", "Rout"),
        },
        "chats": {
            "personal_id": int(personal_chat_id),
            "group_ids": group_ids,
        },
        "chat_handles": chat_handles,
        "known_senders": known_senders,

        # Mode flag
        "local_only": is_local,

        "paths": {
            "python": ex_paths.get("python", sys.executable),
            "imsg": ex_paths.get("imsg", imsg_path if imsg_path else "imsg"),
        },
    }

    # Provider-specific config
    if is_local:
        config["local_model"] = local_config
        config["codex"] = {"enabled": False}
        config["anthropic"] = {"max_tokens": 4096}
    else:
        config["anthropic_api_key"] = api_key
        config["anthropic"] = cloud_config
        config["codex"] = existing.get("codex", {"enabled": True, "timeout_seconds": 120})
        # Include local_model config if it existed (as fallback)
        ex_local = existing.get("local_model", {})
        if ex_local.get("enabled"):
            config["local_model"] = ex_local

    # Preserve optional integrations from existing config
    for section in ["bluebubbles", "coinbase", "kalshi", "watcher"]:
        if section in existing:
            config[section] = existing[section]

    if "watcher" not in config:
        config["watcher"] = {"history_limit": 10}

    # ── Step 7: Write Config ──────────────────────────────────────────────────

    write_config(config, CONFIG_FILE, is_local, cloud_model, model_name)

    # ── Step 8: Memory File ───────────────────────────────────────────────────

    brain_label = model_name if is_local else (cloud_model or "Claude")
    if not MEMORY_FILE.exists():
        MEMORY_FILE.write_text(f"""# Rout Memory — long-term context
# Add anything you want Rout to always know about you.
# This file is read on every message and included in context.

## About Me
- Name: {name}
- Location: {location_str}

## Notes
- (add anything: family, preferences, projects, pets, etc.)
""")
        ok(f"Created {MEMORY_FILE} — edit to add personal context")
    else:
        ok("MEMORY.md already exists")

    # ── Step 9: Make Scripts Executable ────────────────────────────────────────

    for script in ["start_watcher.sh", "stop_watcher.sh", "rout-imsg-watcher", "setup.sh", "setup_local.sh"]:
        p = SCRIPT_DIR / script
        if p.exists():
            p.chmod(0o755)

    # ── Summary ───────────────────────────────────────────────────────────────

    print(f"\n{'=' * 50}")
    print(f"{GREEN}{BOLD}Setup complete!{NC}\n")
    print(f"  Mode:    {BOLD}{'LOCAL' if is_local else 'CLOUD'}{NC}")
    if is_local:
        print(f"  Model:   {model_name}")
        print(f"  Host:    http://localhost:11434")
    else:
        print(f"  Model:   {cloud_model}")
        print(f"  API:     Anthropic")
    print(f"  Config:  {CONFIG_FILE}")
    print(f"  Memory:  {MEMORY_FILE}")

    print(f"\n{BOLD}Start Rout:{NC}")
    print(f"  cd {SCRIPT_DIR}")
    print(f"  python3 comms/imsg_watcher.py")

    print(f"\n{BOLD}Test it:{NC}")
    print(f"  Text yourself: ping")

    if is_local:
        print(f"\n{BOLD}Switch to cloud mode later:{NC}")
        print(f"  python3 setup.py  → choose Cloud")
    else:
        print(f"\n{BOLD}Switch to local mode later:{NC}")
        print(f"  python3 setup.py  → choose Local")

    print()


__all__ = ["main"]
