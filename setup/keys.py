"""
User information and iMessage configuration for Rout setup.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

from .ui import ok, warn, fail, ask, ask_choice, BOLD, DIM, CYAN, NC


# ── Validators ────────────────────────────────────────────────────────────────

def validate_phone(val):
    """Validate phone number in E.164 format."""
    if not re.match(r"^\+\d{10,15}$", val):
        return "Use E.164 format: +1XXXXXXXXXX"
    return None


def validate_api_key(val):
    """Validate Anthropic API key format."""
    if not val.startswith("sk-"):
        return "Anthropic keys start with sk-"
    if len(val) < 30:
        return "That key looks too short"
    if "XXXX" in val:
        return "That looks like a placeholder"
    return None


# ── User Info Setup ───────────────────────────────────────────────────────────

def setup_user_info(existing):
    """
    Collect user name and phone.

    Args:
        existing: Existing config dict

    Returns:
        (name, phone) — strings
    """
    ex_user = existing.get("user", {})
    ex_senders = existing.get("known_senders", {})

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

    return name, phone


# ── Geocoding ─────────────────────────────────────────────────────────────────

def geocode(location_str):
    """
    Geocode a city name to lat/lon using Open-Meteo (free, no key).

    Returns:
        (lat, lon, display_name, timezone) or (None, None, None, None)
    """
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
            return None, None, None, None

        r = results[0]
        display = f"{r.get('name', '')}, {r.get('admin1', '')}, {r.get('country', '')}"
        timezone = r.get("timezone", "auto")
        return r["latitude"], r["longitude"], display, timezone

    except Exception:
        return None, None, None, None


def setup_location(existing):
    """
    Collect and geocode location.

    Args:
        existing: Existing config dict

    Returns:
        (location_str, lat, lon, timezone) — strings/floats
    """
    ex_user = existing.get("user", {})

    print(f"\n{BOLD}Location{NC}")
    print("-" * 30)

    location_str = ask(
        "Your city and state (e.g. Portland, OR)",
        default=ex_user.get("location", ""),
        required=True,
    )

    lat = ex_user.get("latitude")
    lon = ex_user.get("longitude")
    tz = ex_user.get("timezone", "auto")
    print(f"  Geocoding {location_str}...")
    geo_lat, geo_lon, geo_display, geo_timezone = geocode(location_str)
    if geo_lat is not None:
        lat, lon = geo_lat, geo_lon
        tz = geo_timezone or tz
        ok(f"Found: {geo_display} ({lat:.4f}, {lon:.4f})")
    elif lat and lon:
        ok(f"Using saved coordinates ({lat}, {lon})")
    else:
        warn("Couldn't geocode — weather will use approximate coordinates")
        lat, lon = 40.7128, -74.0060
        tz = "America/New_York"

    return location_str, lat, lon, tz


def setup_personality(existing):
    """
    Choose communication style.

    Returns:
        personality string
    """
    return ask_choice(
        "How should Rout talk to you?",
        [
            ("Casual friend — short, witty, straight talk", "casual"),
            ("Professional assistant — polished but concise", "professional"),
            ("Minimal — terse, just the facts", "minimal"),
        ],
        default=1,
    )


# ── iMessage Setup ────────────────────────────────────────────────────────────

def setup_imessage(existing, phone):
    """
    Configure iMessage chats.

    Args:
        existing: Existing config dict
        phone: User's phone number

    Returns:
        (personal_chat_id, group_ids, extra_handles, extra_senders)
    """
    ex_chats = existing.get("chats", {})

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
        print(f"\n  {BOLD}Your iMessage conversations:{NC}\n")
        chat_rows = []
        for i, c in enumerate(discovered_chats[:15], 1):
            cid_raw = c.get("chat_id", c.get("id", i))
            display = c.get("display_name", "")
            identifier = c.get("chat_identifier", "")
            participants = c.get("participants", [])
            if not isinstance(participants, list):
                participants = []

            try:
                cid = int(cid_raw)
            except (TypeError, ValueError):
                cid = i

            if display:
                label = display
            elif identifier and identifier.startswith("+"):
                label = identifier
            elif participants:
                label = ", ".join(str(p) for p in participants[:3])
            else:
                label = identifier or f"Chat {cid}"

            is_group = c.get("is_group", False) or len(participants) > 1
            tag = f"{DIM}(group){NC}" if is_group else f"{DIM}(1:1){NC}"

            print(f"    {CYAN}{i:2d}.{NC} {label}  {tag}")
            print(f"       {DIM}chat_id={cid} identifier={identifier or '-'}{NC}")

            chat_rows.append({
                "index": i, "chat_id": cid, "label": label,
                "identifier": identifier, "participants": participants,
                "is_group": is_group, "display": display,
            })

        by_index = {r["index"]: r for r in chat_rows}
        one_to_one_rows = [r for r in chat_rows if not r["is_group"]]
        default_row = one_to_one_rows[0] if one_to_one_rows else chat_rows[0]

        use_default = ask(
            f'Use "{default_row["label"]}" as your primary chat? (Y/n)',
            default="y",
        )
        if use_default.lower().startswith("y"):
            chosen_row = default_row
        else:
            personal_pick = ask(
                "Enter the number of your primary chat",
                default=str(default_row["index"]),
            )
            try:
                chosen_row = by_index.get(int(personal_pick), default_row)
            except ValueError:
                chosen_row = default_row

        personal_chat_id = int(chosen_row["chat_id"])
        if chosen_row["identifier"]:
            handle_type = "chat" if chosen_row["is_group"] else "buddy"
            extra_handles[personal_chat_id] = [chosen_row["identifier"], handle_type]

        monitor_groups = ask("Also respond in group chats? (y/N)", default="n")
        if monitor_groups.lower().startswith("y"):
            group_rows = [
                r for r in chat_rows
                if r["is_group"] and int(r["chat_id"]) != int(personal_chat_id)
            ]

            if not group_rows:
                warn("No group chats found in the discovered list.")
            else:
                print(f"\n  {DIM}Group chats:{NC}")
                for r in group_rows:
                    print(f'    {CYAN}{r["index"]:2d}.{NC} {r["label"]}')
                    print(f'       {DIM}chat_id={r["chat_id"]} identifier={r["identifier"] or "-"}{NC}')

                existing_group_ids = {
                    int(g) for g in ex_chats.get("group_ids", [])
                    if str(g).isdigit()
                }
                default_group_pick = ",".join(
                    str(r["index"]) for r in group_rows
                    if int(r["chat_id"]) in existing_group_ids
                )

                group_pick = ask(
                    "Enter numbers to enable (comma-separated, blank for none)",
                    default=default_group_pick,
                )
                if group_pick.strip():
                    for num in group_pick.split(","):
                        num = num.strip()
                        if not num:
                            continue
                        try:
                            row = by_index.get(int(num))
                            if not row or not row["is_group"]:
                                continue
                            gid = int(row["chat_id"])
                            if gid == int(personal_chat_id):
                                continue
                            if gid not in group_ids:
                                group_ids.append(gid)
                            if row["identifier"]:
                                extra_handles[gid] = [row["identifier"], "chat"]
                            for p in row["participants"]:
                                p_str = str(p)
                                if p_str.startswith("+") and p_str != phone:
                                    p_name = row["display"] or p_str
                                    extra_senders[p_str] = p_name
                        except (ValueError, TypeError):
                            pass

    else:
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

        monitor_groups = ask("Also respond in group chats? (y/N)", default="n")
        if monitor_groups.lower().startswith("y"):
            group_input = ask(
                "Group chat IDs to monitor (comma-separated, or blank for none)",
                default=",".join(str(g) for g in ex_chats.get("group_ids", [])),
            )
            if group_input:
                for g in group_input.split(","):
                    g = g.strip()
                    if g.isdigit():
                        group_ids.append(int(g))

    return personal_chat_id, group_ids, extra_handles, extra_senders
