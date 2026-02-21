#!/usr/bin/env python3
"""
Rout Setup Wizard — interactive first-run configuration.

Collects user info, validates dependencies, geocodes location,
tests the API key, and writes ~/.openclaw/config.yaml.

Safe to re-run: preserves existing values as defaults.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

OPENCLAW_DIR = Path.home() / ".openclaw"
CONFIG_FILE = OPENCLAW_DIR / "config.yaml"
MEMORY_FILE = OPENCLAW_DIR / "MEMORY.md"
SCRIPT_DIR = Path(__file__).resolve().parent

# ── Colors ────────────────────────────────────────────────────────────────────

GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
RED = "\033[0;31m"
BOLD = "\033[1m"
DIM = "\033[2m"
NC = "\033[0m"


def ok(msg):
    print(f"{GREEN}  ✓ {msg}{NC}")


def warn(msg):
    print(f"{YELLOW}  ! {msg}{NC}")


def fail(msg):
    print(f"{RED}  ✗ {msg}{NC}")


def ask(prompt, default="", validate=None, required=False):
    """Prompt user for input with optional default and validation."""
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{BOLD}{prompt}{suffix}:{NC} ").strip()
        if not val and default:
            val = default
        if required and not val:
            warn("This field is required.")
            continue
        if validate and val:
            err = validate(val)
            if err:
                warn(err)
                continue
        return val


def ask_choice(prompt, options, default=1):
    """Prompt user to pick from numbered options."""
    print(f"\n{BOLD}{prompt}{NC}")
    for i, (label, _) in enumerate(options, 1):
        marker = " (default)" if i == default else ""
        print(f"  {i}. {label}{marker}")
    while True:
        val = input(f"{BOLD}Choose [1-{len(options)}]:{NC} ").strip()
        if not val:
            return options[default - 1][1]
        try:
            idx = int(val)
            if 1 <= idx <= len(options):
                return options[idx - 1][1]
        except ValueError:
            pass
        warn(f"Enter a number 1-{len(options)}")


# ── Validators ────────────────────────────────────────────────────────────────

def validate_phone(val):
    if not re.match(r"^\+\d{10,15}$", val):
        return "Use E.164 format: +1XXXXXXXXXX"
    return None


def validate_api_key(val):
    if not val.startswith("sk-"):
        return "Anthropic keys start with sk-"
    if len(val) < 30:
        return "That key looks too short"
    if "XXXX" in val:
        return "That looks like a placeholder"
    return None


# ── Geocoding ─────────────────────────────────────────────────────────────────

def geocode(location_str):
    """Geocode a city name to lat/lon using Open-Meteo (free, no key)."""
    try:
        import requests
        resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location_str, "count": 3, "language": "en", "format": "json"},
            timeout=10,
        )
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None, None, None

        # If only one result or first is a strong match, use it
        r = results[0]
        display = f"{r.get('name', '')}, {r.get('admin1', '')}, {r.get('country', '')}"
        return r["latitude"], r["longitude"], display

    except Exception:
        return None, None, None


# ── Dependency checks ─────────────────────────────────────────────────────────

def check_dependencies():
    """Check for required tools. Returns list of issues."""
    issues = []

    # Python packages
    for pkg, import_name in [("pyyaml", "yaml"), ("requests", "requests")]:
        try:
            __import__(import_name)
        except ImportError:
            print(f"  Installing {pkg}...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", pkg, "--quiet", "--break-system-packages"],
                capture_output=True,
            )
            try:
                __import__(import_name)
                ok(f"{pkg} installed")
            except ImportError:
                issues.append(f"Failed to install {pkg}")

    # imsg CLI
    imsg_path = shutil.which("imsg") or "/opt/homebrew/bin/imsg"
    if os.path.isfile(imsg_path) and os.access(imsg_path, os.X_OK):
        ok(f"imsg found at {imsg_path}")
    else:
        issues.append("imsg CLI not found — install with: brew install imsg")

    return issues


# ── API key test ──────────────────────────────────────────────────────────────

def test_api_key(key):
    """Send a minimal request to verify the API key works."""
    try:
        import requests
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Say ok"}],
            },
            timeout=15,
        )
        if resp.status_code == 200:
            return True, None
        elif resp.status_code == 401:
            return False, "Invalid key (401 Unauthorized)"
        elif resp.status_code == 403:
            return False, "Key doesn't have API access (403 Forbidden)"
        else:
            return False, f"API returned {resp.status_code}: {resp.text[:100]}"
    except Exception as e:
        return False, f"Connection error: {e}"


# ── Load existing config ──────────────────────────────────────────────────────

def load_existing_config():
    """Load existing config for defaults, if any."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        import yaml
        with open(CONFIG_FILE) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


# ── Main wizard ───────────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}Rout — Setup Wizard{NC}")
    print("=" * 40)
    print(f"{DIM}This creates ~/.openclaw/config.yaml with your personal settings.{NC}")
    print(f"{DIM}Safe to re-run — your existing values become defaults.{NC}\n")

    # Ensure directories
    OPENCLAW_DIR.mkdir(parents=True, exist_ok=True)
    (OPENCLAW_DIR / "state").mkdir(exist_ok=True)
    (OPENCLAW_DIR / "logs").mkdir(exist_ok=True)
    (OPENCLAW_DIR / "keys").mkdir(exist_ok=True)

    # Check deps
    print(f"{BOLD}Checking dependencies...{NC}")
    issues = check_dependencies()
    if issues:
        for i in issues:
            fail(i)
        print(f"\n{RED}Fix the issues above and re-run setup.{NC}")
        sys.exit(1)
    print()

    # Now we can import yaml
    import yaml

    existing = load_existing_config()
    ex_user = existing.get("user", {})
    ex_chats = existing.get("chats", {})
    ex_senders = existing.get("known_senders", {})
    ex_handles = existing.get("chat_handles", {})

    # ── Your info ─────────────────────────────────────────────────────────────

    print(f"{BOLD}About you{NC}")
    print("-" * 30)

    name = ask("Your first name", default=ex_user.get("name", ""), required=True)

    phone_default = ""
    if ex_senders:
        phone_default = list(ex_senders.keys())[0]
    phone = ask(
        "Your phone number (E.164 format, e.g. +15551234567)",
        default=phone_default,
        validate=validate_phone,
        required=True,
    )

    # ── Location ──────────────────────────────────────────────────────────────

    print(f"\n{BOLD}Location{NC}")
    print("-" * 30)

    location_str = ask(
        "Your city and state (e.g. Portland, OR)",
        default=ex_user.get("location", ""),
        required=True,
    )

    # Geocode
    lat = ex_user.get("latitude")
    lon = ex_user.get("longitude")
    print(f"  Geocoding {location_str}...")
    geo_lat, geo_lon, geo_display = geocode(location_str)
    if geo_lat is not None:
        lat, lon = geo_lat, geo_lon
        ok(f"Found: {geo_display} ({lat:.4f}, {lon:.4f})")
    elif lat and lon:
        ok(f"Using saved coordinates ({lat}, {lon})")
    else:
        warn("Couldn't geocode — weather will use approximate coordinates")
        lat, lon = 40.7128, -74.0060  # Seattle fallback

    # ── Personality ───────────────────────────────────────────────────────────

    personality = ask_choice(
        "How should Rout talk to you?",
        [
            ("Casual friend — short, witty, straight talk", "casual"),
            ("Professional assistant — polished but concise", "professional"),
            ("Minimal — terse, just the facts", "minimal"),
        ],
        default=1,
    )

    # ── iMessage setup ────────────────────────────────────────────────────────

    print(f"\n{BOLD}iMessage setup{NC}")
    print("-" * 30)

    # Auto-discover chats via imsg CLI
    discovered_chats = []
    imsg_path = shutil.which("imsg") or "/opt/homebrew/bin/imsg"
    try:
        result = subprocess.run(
            [imsg_path, "chats", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                try:
                    discovered_chats.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass

    personal_chat_id = ex_chats.get("personal_id", 1)
    group_ids = []
    extra_handles = {}
    extra_senders = {}

    if discovered_chats:
        # Show chats as a friendly list
        print(f"\n  {BOLD}Your iMessage conversations:{NC}\n")
        for i, c in enumerate(discovered_chats[:15], 1):
            cid = c.get("chat_id", c.get("id", "?"))
            display = c.get("display_name", "")
            identifier = c.get("chat_identifier", "")
            participants = c.get("participants", [])

            # Build a friendly label
            if display:
                label = display
            elif identifier and identifier.startswith("+"):
                label = identifier
            elif participants:
                label = ", ".join(str(p) for p in participants[:3])
            else:
                label = identifier or f"Chat {cid}"

            # Tag type
            is_group = c.get("is_group", False) or len(participants) > 1
            tag = f"{DIM}(group){NC}" if is_group else f"{DIM}(1:1){NC}"

            print(f"    {CYAN}{i:2d}.{NC} {label}  {tag}")

        # Select personal chat
        print(f"\n  {DIM}Select your personal 1:1 chat (where Rout texts you back){NC}")
        personal_pick = ask("Enter the number from the list above", default="1")
        try:
            idx = int(personal_pick) - 1
            if 0 <= idx < len(discovered_chats):
                pc = discovered_chats[idx]
                personal_chat_id = pc.get("chat_id", pc.get("id", 1))
                # Auto-set chat handle from identifier
                identifier = pc.get("chat_identifier", "")
                if identifier:
                    extra_handles[int(personal_chat_id)] = [identifier, "buddy"]
            else:
                personal_chat_id = 1
        except ValueError:
            personal_chat_id = 1

        # Select group chats (optional)
        print(f"\n  {DIM}Select group chats to monitor (Rout will respond in these too){NC}")
        group_pick = ask(
            "Enter numbers from the list (comma-separated, or blank for none)",
            default="",
        )
        if group_pick.strip():
            for num in group_pick.split(","):
                num = num.strip()
                try:
                    idx = int(num) - 1
                    if 0 <= idx < len(discovered_chats):
                        gc = discovered_chats[idx]
                        gid = gc.get("chat_id", gc.get("id"))
                        if gid and int(gid) != int(personal_chat_id):
                            group_ids.append(int(gid))
                            # Set handle for group chat
                            g_ident = gc.get("chat_identifier", "")
                            if g_ident:
                                extra_handles[int(gid)] = [g_ident, "chat"]
                            # Add participants to known senders
                            for p in gc.get("participants", []):
                                p_str = str(p)
                                if p_str.startswith("+") and p_str != phone:
                                    p_name = gc.get("display_name", p_str)
                                    extra_senders[p_str] = p_name
                except (ValueError, TypeError):
                    pass

    else:
        # Fallback: manual entry if imsg chats doesn't work
        warn("Couldn't auto-discover chats. You can enter IDs manually.")
        print(f"  {DIM}Run `imsg chats --json` to see your chat IDs{NC}")

        chat_id_default = str(ex_chats.get("personal_id", 1))
        personal_chat_id = ask(
            "Your personal chat ID (usually 1)",
            default=chat_id_default,
        )
        try:
            personal_chat_id = int(personal_chat_id)
        except ValueError:
            personal_chat_id = 1

        group_input = ask(
            "Group chat IDs to monitor (comma-separated, or blank for none)",
            default=",".join(str(g) for g in ex_chats.get("group_ids", [])),
        )
        if group_input:
            for g in group_input.split(","):
                g = g.strip()
                if g.isdigit():
                    group_ids.append(int(g))

    # ── API key ───────────────────────────────────────────────────────────────

    print(f"\n{BOLD}Anthropic API key{NC}")
    print("-" * 30)
    print(f"  {DIM}Get one at: https://console.anthropic.com/settings/keys{NC}")

    # Check existing
    existing_key = ""
    for k in ["anthropic_api_key"]:
        if existing.get(k, "") and "XXXX" not in str(existing[k]):
            existing_key = existing[k]
            break
    for section in ["claude", "anthropic"]:
        nested = existing.get(section, {})
        if isinstance(nested, dict):
            for k in ["api_key"]:
                v = nested.get(k, "")
                if v and "XXXX" not in str(v):
                    existing_key = v
                    break

    if existing_key:
        masked = f"{existing_key[:8]}...{existing_key[-4:]}"
        print(f"  {DIM}Current key: {masked}{NC}")
        keep = ask("Keep existing key? (Y/n)", default="y")
        if keep.lower().startswith("y"):
            api_key = existing_key
        else:
            api_key = ask("Paste your API key", validate=validate_api_key, required=True)
    else:
        api_key = ask("Paste your API key", validate=validate_api_key, required=True)

    # Test the key
    print("  Testing API key...")
    success, err = test_api_key(api_key)
    if success:
        ok("API key is valid")
    else:
        warn(f"Key test failed: {err}")
        proceed = ask("Save anyway? (y/N)", default="n")
        if not proceed.lower().startswith("y"):
            print("Setup cancelled. Re-run when you have a valid key.")
            sys.exit(1)

    # ── Kalshi (optional) ─────────────────────────────────────────────────────

    print(f"\n{BOLD}Optional integrations{NC}")
    print("-" * 30)

    kalshi_enabled = ask("Enable Kalshi prediction markets? (y/N)", default="n")
    kalshi_cfg = existing.get("kalshi", {})
    if kalshi_enabled.lower().startswith("y"):
        kalshi_key_id = ask(
            "Kalshi API key ID",
            default=kalshi_cfg.get("api_key_id", kalshi_cfg.get("key_id", "")),
        )
        kalshi_key_file = ask(
            "Kalshi private key filename (in ~/.openclaw/keys/)",
            default=kalshi_cfg.get("private_key_file", "kalshi-private.key"),
        )
        kalshi_cfg = {
            "enabled": True,
            "api_key_id": kalshi_key_id,
            "private_key_file": kalshi_key_file,
            "base_url": "https://api.elections.kalshi.com",
            "environment": "production",
        }
    else:
        kalshi_cfg = {"enabled": False}

    # ── Build config ──────────────────────────────────────────────────────────

    # Build chat_handles — merge auto-discovered + phone-based + existing
    chat_handles = {int(personal_chat_id): [phone, "buddy"]}
    for k, v in extra_handles.items():
        if k not in chat_handles:
            chat_handles[k] = v

    # Build known_senders — merge auto-discovered + user + existing
    known_senders = {phone: name}
    for k, v in extra_senders.items():
        if k not in known_senders:
            known_senders[k] = v

    config = {
        "user": {
            "name": name,
            "location": location_str,
            "latitude": round(lat, 4),
            "longitude": round(lon, 4),
            "personality": personality,
            "assistant_name": ex_user.get("assistant_name", "Rout"),
        },
        "chats": {
            "personal_id": int(personal_chat_id),
            "group_ids": group_ids,
        },
        "chat_handles": chat_handles,
        "known_senders": known_senders,
        "anthropic_api_key": api_key,
        "kalshi": kalshi_cfg,
    }

    # Preserve any extra handles from existing config
    if ex_handles:
        for k, v in ex_handles.items():
            k_int = int(k)
            if k_int not in config["chat_handles"]:
                config["chat_handles"][k_int] = v

    # Preserve extra known senders from existing config
    if ex_senders:
        for k, v in ex_senders.items():
            if k not in config["known_senders"]:
                config["known_senders"][k] = v

    # ── Write config ──────────────────────────────────────────────────────────

    # Backup existing
    if CONFIG_FILE.exists():
        backup = CONFIG_FILE.with_suffix(f".yaml.bak")
        shutil.copy2(CONFIG_FILE, backup)
        ok(f"Backed up existing config to {backup.name}")

    with open(CONFIG_FILE, "w") as f:
        f.write("# Rout config — generated by setup.py\n")
        f.write("# Edit freely. Re-run `python3 setup.py` to reconfigure.\n\n")
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    os.chmod(CONFIG_FILE, 0o600)
    ok(f"Config saved to {CONFIG_FILE}")

    # ── MEMORY.md ─────────────────────────────────────────────────────────────

    if not MEMORY_FILE.exists():
        MEMORY_FILE.write_text(f"""# Rout Memory — long-term context
# Add anything you want Rout to always know about you.
# This file is read on every message and included in Claude's context.

## About Me
- Name: {name}
- Location: {location_str}

## Notes
- (add anything: family, preferences, projects, pets, etc.)
""")
        ok(f"Created {MEMORY_FILE} — edit to add personal context")
    else:
        ok("MEMORY.md already exists")

    # ── Make scripts executable ───────────────────────────────────────────────

    for script in ["start_watcher.sh", "stop_watcher.sh", "rout-imsg-watcher"]:
        p = SCRIPT_DIR / script
        if p.exists():
            p.chmod(0o755)

    # ── Summary ───────────────────────────────────────────────────────────────

    print(f"\n{'=' * 40}")
    print(f"{GREEN}{BOLD}Setup complete!{NC}\n")
    print(f"  Config:  {CONFIG_FILE}")
    print(f"  Memory:  {MEMORY_FILE}")
    print(f"  Logs:    {OPENCLAW_DIR / 'logs' / 'imsg_watcher.log'}")
    print(f"\n{BOLD}Start Rout:{NC}")
    print(f"  cd {SCRIPT_DIR}")
    print(f"  python3 comms/imsg_watcher.py")
    print(f"\n{BOLD}Test it:{NC}")
    print(f"  Text yourself: ping")
    print()


if __name__ == "__main__":
    main()
