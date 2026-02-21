"""General handlers: Claude routing, calendar, reminders, search, image analysis."""

import json
import os
import re
import subprocess
import base64
import mimetypes
import yaml
from pathlib import Path


def _is_real_token(val):
    """Check if a string looks like a real Anthropic token (not a placeholder)."""
    if not val or not isinstance(val, str):
        return False
    val = val.strip()
    if not val.startswith("sk-"):
        return False
    if "XXXX" in val or "xxxx" in val or len(val) < 30:
        return False
    return True


def _get_api_key():
    """Find Anthropic API/OAuth token from all available sources.

    Search order:
      1. Environment variables (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, CLAUDE_API_KEY)
      2. Config files (~/.openclaw/config.yaml, workspace/config.yaml)
      3. OpenClaw credential files (oauth.json, anthropic.json, auth-profiles.json)
      4. openclaw CLI (runtime token discovery)
    Rejects placeholder tokens (sk-ant-XXXXXXXXX).
    """
    import subprocess

    # 1. Environment variables
    for var in ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY"]:
        val = os.environ.get(var, "")
        if _is_real_token(val):
            return val

    # 2. Config YAML files
    openclaw = Path.home() / ".openclaw"
    for cfg_path in [
        openclaw / "config.yaml",
        openclaw / "workspace" / "config.yaml",
    ]:
        if not cfg_path.exists():
            continue
        try:
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            # Top-level keys
            for key_name in ["anthropic_api_key", "api_key", "ANTHROPIC_API_KEY"]:
                val = str(cfg.get(key_name, "")).strip()
                if _is_real_token(val):
                    return val
            # Nested: anthropic.*, claude.*
            for section in ["anthropic", "claude"]:
                nested = cfg.get(section, {})
                if isinstance(nested, dict):
                    for key_name in ["api_key", "anthropic_api_key", "token", "auth_token"]:
                        val = str(nested.get(key_name, "")).strip()
                        if _is_real_token(val):
                            return val
        except Exception:
            continue

    # 3. OpenClaw credential files (JSON)
    for cred_path in [
        openclaw / "credentials" / "oauth.json",
        openclaw / "credentials" / "anthropic.json",
        openclaw / "agent" / "auth-profiles.json",
        openclaw / "openclaw.json",
    ]:
        if not cred_path.exists():
            continue
        try:
            import json as _json
            with open(cred_path) as f:
                data = _json.load(f)
            token = _extract_token_from_dict(data)
            if token:
                return token
        except Exception:
            continue

    # 4. OpenClaw .env file
    env_path = openclaw / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text().split("\n"):
                if "sk-ant-" in line:
                    val = line.split("=", 1)[-1].strip().strip("'\"")
                    if _is_real_token(val):
                        return val
        except Exception:
            pass

    # 5. openclaw CLI (runtime discovery)
    for cmd in [
        ["openclaw", "config", "get", "providers.anthropic.apiKey"],
        ["openclaw", "auth", "status"],
        ["openclaw", "config", "show"],
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            for line in result.stdout.split("\n"):
                for word in line.split():
                    word = word.strip("'\",:=")
                    if _is_real_token(word):
                        return word
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            continue

    return ""


def _extract_token_from_dict(data, depth=0):
    """Recursively search a dict for a real Anthropic token."""
    if depth > 5:
        return None
    if isinstance(data, str):
        return data if _is_real_token(data) else None
    if isinstance(data, dict):
        for key in ["api_key", "anthropic_api_key", "token", "auth_token", "access_token", "key"]:
            val = data.get(key, "")
            if _is_real_token(val):
                return val
        for val in data.values():
            result = _extract_token_from_dict(val, depth + 1)
            if result:
                return result
    if isinstance(data, list):
        for item in data:
            result = _extract_token_from_dict(item, depth + 1)
            if result:
                return result
    return None

# --- Keyword lists for intent detection ---
CALENDAR_READ_KEYWORDS = [
    "calendar", "schedule", "what's on", "upcoming", "events",
    "meetings", "agenda", "plans for", "free time", "availability",
    "what do i have", "am i busy", "what's happening"
]

CALENDAR_WRITE_KEYWORDS = [
    "add to calendar", "create event", "schedule a", "book a",
    "set up a meeting", "add meeting", "new event", "put on calendar",
    "block time", "calendar event"
]

REMINDER_KEYWORDS = [
    "remind me", "set a reminder", "reminder to", "don't let me forget",
    "remember to", "alert me", "notify me"
]

TASK_KEYWORDS = [
    "add task", "new task", "todo", "to-do", "to do"
]

SEARCH_KEYWORDS = [
    "search for", "look up", "google", "find out", "what is",
    "who is", "search the web", "web search"
]

KALSHI_KEYWORDS = [
    "kalshi", "market", "prediction", "bet", "trade",
    "position", "portfolio", "contract"
]

# Dangerous characters for AppleScript injection
_UNSAFE_CHARS = re.compile(r'[\\"\x00-\x1f]')


def _sanitize_applescript(text):
    """Strip dangerous characters from AppleScript inputs."""
    return _UNSAFE_CHARS.sub("", str(text)[:500])


def _load_memory():
    """Load personal context from MEMORY.md."""
    memory_path = os.path.expanduser("~/.openclaw/MEMORY.md")
    if not os.path.exists(memory_path):
        return ""
    try:
        with open(memory_path, "r") as f:
            content = f.read()
        return content[:4000]
    except Exception:
        return ""


def _load_chat_history(sender=None, limit=10):
    """Fetch recent iMessage history for multi-turn context."""
    try:
        cmd = ["imsg", "history"]
        if sender:
            cmd.extend(["--from", sender])
        cmd.extend(["--limit", str(limit)])
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _personality_style(personality: str) -> str:
    """Return prompt style instructions for the selected personality."""
    styles = {
        "casual": "Respond like a sharp, helpful friend: short, witty, and direct.",
        "professional": "Respond like a professional assistant: polished, concise, and clear.",
        "minimal": "Respond with minimal text: direct facts only, no extra phrasing.",
    }
    return styles.get((personality or "").strip().lower(), styles["casual"])


def _detect_intent(message):
    """Detect intent from keywords. Returns (intent_type, confidence)."""
    msg_lower = message.lower()

    for kw in WEATHER_KEYWORDS:
        if kw in msg_lower:
            return ("weather", 0.9)

    for kw in CALENDAR_WRITE_KEYWORDS:
        if kw in msg_lower:
            return ("calendar_write", 0.9)

    for kw in CALENDAR_READ_KEYWORDS:
        if kw in msg_lower:
            return ("calendar_read", 0.8)

    for kw in REMINDER_KEYWORDS:
        if kw in msg_lower:
            return ("reminder", 0.9)

    for kw in TASK_KEYWORDS:
        if kw in msg_lower:
            return ("task", 0.8)

    for kw in SEARCH_KEYWORDS:
        if kw in msg_lower:
            return ("search", 0.7)

    for kw in KALSHI_KEYWORDS:
        if kw in msg_lower:
            return ("kalshi", 0.7)

    return ("general", 0.5)


def _read_calendar(days_ahead=7):
    """Read calendar events via AppleScript."""
    days = max(1, min(int(days_ahead), 30))
    script = f'''
    tell application "Calendar"
        set now to current date
        set endDate to now + ({days} * days)
        set output to ""
        repeat with cal in calendars
            set evts to (every event of cal whose start date >= now and start date <= endDate)
            repeat with evt in evts
                set output to output & (summary of evt) & " | " & (start date of evt) & linefeed
            end repeat
        end repeat
        return output
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return "No upcoming events found."
    except Exception as e:
        return f"Calendar error: {e}"


def _create_calendar_event(title, date_str, time_str=None, duration=60):
    """Create a calendar event via AppleScript."""
    title = _sanitize_applescript(title)
    date_str = _sanitize_applescript(date_str)

    time_part = ""
    if time_str:
        time_str = _sanitize_applescript(time_str)
        time_part = f' & " " & "{time_str}"'

    script = f'''
    tell application "Calendar"
        tell calendar "Calendar"
            set startDate to date ("{date_str}"{time_part})
            set endDate to startDate + ({int(duration)} * minutes)
            make new event with properties {{summary:"{title}", start date:startDate, end date:endDate}}
        end tell
    end tell
    return "Event created: {title}"
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            return f"Created event: {title}"
        return f"Failed to create event: {result.stderr.strip()}"
    except Exception as e:
        return f"Calendar error: {e}"


def _create_reminder(title, due_date=None):
    """Create a reminder via AppleScript."""
    title = _sanitize_applescript(title)

    date_prop = ""
    if due_date:
        due_date = _sanitize_applescript(due_date)
        date_prop = f', due date:date "{due_date}"'

    script = f'''
    tell application "Reminders"
        make new reminder with properties {{name:"{title}"{date_prop}}}
    end tell
    return "Reminder set: {title}"
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            return f"Reminder set: {title}"
        return f"Failed to create reminder: {result.stderr.strip()}"
    except Exception as e:
        return f"Reminder error: {e}"


def _get_weather():
    """Get current weather for user's location via Open-Meteo (free, no key)."""
    import requests

    # Load location from config
    user_cfg = {}
    try:
        cfg_path = os.path.expanduser("~/.openclaw/config.yaml")
        with open(cfg_path) as f:
            user_cfg = yaml.safe_load(f) or {}
    except Exception:
        pass

    user_info = user_cfg.get("user", {})
    loc = user_info.get("location", "")
    lat = user_info.get("latitude", 40.7128)
    lon = user_info.get("longitude", -74.0060)
    timezone = user_info.get("timezone", "auto")
    location_name = loc if loc else "your area"

    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
                "timezone": timezone,
                "forecast_days": 1,
            },
            timeout=10,
        )
        data = resp.json()
        cur = data.get("current", {})
        daily = data.get("daily", {})

        # Weather code descriptions
        WMO = {
            0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
            45: "Foggy", 48: "Icy fog", 51: "Light drizzle", 53: "Drizzle",
            55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
            71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
            80: "Light showers", 81: "Showers", 82: "Heavy showers",
            85: "Light snow showers", 86: "Snow showers",
            95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Severe thunderstorm",
        }

        code = cur.get("weather_code", 0)
        condition = WMO.get(code, "Unknown")
        temp = cur.get("temperature_2m", "?")
        feels = cur.get("apparent_temperature", "?")
        humidity = cur.get("relative_humidity_2m", "?")
        wind = cur.get("wind_speed_10m", "?")
        precip = cur.get("precipitation", 0)

        hi = daily.get("temperature_2m_max", ["?"])[0]
        lo = daily.get("temperature_2m_min", ["?"])[0]
        rain_chance = daily.get("precipitation_probability_max", ["?"])[0]

        lines = [
            f"{location_name} right now: {condition}, {temp}°F (feels {feels}°F)",
            f"High {hi}° / Low {lo}° today",
            f"Wind {wind} mph, humidity {humidity}%",
        ]
        if rain_chance and rain_chance != "?" and int(rain_chance) > 10:
            lines.append(f"{rain_chance}% chance of rain")
        if precip and float(precip) > 0:
            lines.append(f"Current precipitation: {precip}\"")

        return "\n".join(lines)

    except Exception as e:
        return f"Couldn't get weather: {e}"


WEATHER_KEYWORDS = [
    "weather", "temperature", "forecast", "rain", "snow",
    "how cold", "how hot", "how warm", "is it raining",
]


def _web_search(query):
    """Search via DuckDuckGo instant answer API."""
    import requests
    try:
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1},
            timeout=10
        )
        data = resp.json()
        abstract = data.get("AbstractText", "")
        answer = data.get("Answer", "")
        if abstract:
            return f"Search: {abstract[:500]}"
        if answer:
            return f"Answer: {answer[:500]}"
        # Fall back to related topics
        topics = data.get("RelatedTopics", [])
        if topics:
            results = []
            for t in topics[:3]:
                if isinstance(t, dict) and "Text" in t:
                    results.append(t["Text"][:200])
            if results:
                return "Related:\n" + "\n".join(results)
        return f"No instant results for '{query}'. Try asking me directly."
    except Exception as e:
        return f"Search error: {e}"


def _analyze_image(file_path, prompt="Describe this image."):
    """Analyze an image using Claude vision API."""
    ALLOWED_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    path = Path(file_path)

    if not path.exists():
        return "Image file not found."
    if path.suffix.lower() not in ALLOWED_TYPES:
        return f"Unsupported image type: {path.suffix}"
    if path.stat().st_size > 20_000_000:
        return "Image too large (max 20MB)."

    mime_type = mimetypes.guess_type(str(path))[0] or "image/png"

    try:
        with open(path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        return f"Error reading image: {e}"

    return _call_claude(
        prompt,
        image_data=img_data,
        image_media_type=mime_type
    )


def _call_claude(message, system_prompt=None, image_data=None, image_media_type=None):
    """Call Claude API. Returns TEXT ONLY — never executed."""
    try:
        api_key = _get_api_key()
        if not api_key:
            return "No API token found. Re-run setup.py or set ANTHROPIC_API_KEY."

        import requests
        headers = {
            "x-api-key": api_key,
            "content-type": "application/json",
            "anthropic-version": "2023-06-01"
        }

        content = []
        if image_data and image_media_type:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image_media_type,
                    "data": image_data
                }
            })
        content.append({"type": "text", "text": message})

        body = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": content}]
        }

        if system_prompt:
            body["system"] = system_prompt

        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=body,
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()

        # Extract text only
        text_parts = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block["text"])
        return "\n".join(text_parts) if text_parts else "No response from Claude."

    except Exception as e:
        return f"Claude error: {e}"


def claude_command(args=None, message="", sender=None, metadata=None):
    """Main handler — routes messages through intent detection and Claude.

    This is the fallback handler for all non-command messages.
    Flow: parse metadata → detect intent → handle or route to Claude.
    Returns TEXT ONLY — never executed.
    """
    if not message and args:
        message = args

    if not message:
        return "Hey! Send me anything and I'll help out."

    # Parse any metadata tags from watcher
    meta = metadata or {}
    attachments = meta.get("attachments", [])
    is_group = bool(meta.get("is_group"))
    sender_name = meta.get("sender_name", "")

    # Check for image attachments
    if attachments:
        for att in attachments:
            if any(att.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]):
                return _analyze_image(att, prompt=message if message else "Describe this image.")

    # Detect intent
    intent, confidence = _detect_intent(message)

    # Route by intent
    if intent == "weather" and confidence >= 0.7:
        return _get_weather()

    if intent == "calendar_read" and confidence >= 0.7:
        events = _read_calendar()
        # Also ask Claude to summarize if the message is conversational
        if len(message.split()) > 3:
            memory = _load_memory()
            system = f"You are Rout, a personal AI assistant via iMessage. Be concise.\n\n{memory}" if memory else "You are Rout, a personal AI assistant via iMessage. Be concise."
            return _call_claude(
                f"Here are my upcoming calendar events:\n{events}\n\nUser asked: {message}\n\nGive a helpful, concise response.",
                system_prompt=system
            )
        return events

    if intent == "calendar_write" and confidence >= 0.8:
        memory = _load_memory()
        system = "You are Rout. The user wants to create a calendar event. Extract: title, date, time, duration. Respond ONLY with JSON: {\"title\": ..., \"date\": ..., \"time\": ..., \"duration\": ...}. Use null for missing fields."
        claude_resp = _call_claude(message, system_prompt=system)
        try:
            event_data = json.loads(claude_resp)
            if event_data.get("title") and event_data.get("date"):
                return _create_calendar_event(
                    event_data["title"],
                    event_data["date"],
                    event_data.get("time"),
                    event_data.get("duration", 60)
                )
        except (json.JSONDecodeError, KeyError):
            pass
        return "I couldn't parse that event. Try: 'Add meeting with John tomorrow at 3pm'"

    if intent == "reminder" and confidence >= 0.8:
        system = "You are Rout. The user wants to set a reminder. Extract: title and optional due_date. Respond ONLY with JSON: {\"title\": ..., \"due_date\": ...}. Use null for missing fields."
        claude_resp = _call_claude(message, system_prompt=system)
        try:
            reminder_data = json.loads(claude_resp)
            if reminder_data.get("title"):
                return _create_reminder(
                    reminder_data["title"],
                    reminder_data.get("due_date")
                )
        except (json.JSONDecodeError, KeyError):
            pass
        return "I couldn't parse that reminder. Try: 'Remind me to call mom tomorrow'"

    if intent == "search" and confidence >= 0.7:
        # Strip search prefixes
        query = message.lower()
        for prefix in ["search for", "look up", "google", "find out about", "search the web for"]:
            if query.startswith(prefix):
                query = query[len(prefix):].strip()
                break
        return _web_search(query if query else message)

    # Default: send to Claude with full context
    memory = _load_memory()
    history = _load_chat_history(sender=sender, limit=8)

    # Load user info from config
    user_cfg = {}
    try:
        cfg_path = os.path.expanduser("~/.openclaw/config.yaml")
        with open(cfg_path) as f:
            user_cfg = yaml.safe_load(f) or {}
    except Exception:
        pass
    user_info = user_cfg.get("user", {})
    user_name = user_info.get("name", "")
    user_location = user_info.get("location", "")
    personality = user_info.get("personality", "casual")

    system_parts = [
        "You are Rout, a personal AI assistant texting via iMessage.",
        _personality_style(personality),
        "This is a text conversation. 2-3 sentences max unless they ask for detail.",
        "No bullet points, no markdown formatting, no bold text — plain text only.",
        "Never say 'based on search results' or hedge. Just give the answer.",
        "Don't sign off or ask 'anything else?' — just answer and stop.",
    ]
    if user_name:
        system_parts.append(f"You're texting with {user_name}.")
    if user_location:
        system_parts.append(f"{user_name or 'They'} live in {user_location}. Always use this for anything location-specific — weather, restaurants, events, directions, local news.")
    if memory:
        system_parts.append(f"\nContext about them:\n{memory}")
    if is_group and sender_name:
        system_parts.append(
            f"This message is from a group chat and the sender is {sender_name}. "
            "Use that sender context in your response when helpful."
        )
    system_prompt = "\n".join(system_parts)

    # Build conversation
    messages = []
    if history:
        messages.append({
            "role": "user",
            "content": f"[Recent chat history for context — do NOT repeat or reference this directly]\n{history}"
        })
        messages.append({
            "role": "assistant",
            "content": "Got it, I have the context."
        })

    messages.append({"role": "user", "content": message})

    # Use _call_claude with the full conversation
    # For multi-turn we need to call the API directly
    try:
        api_key = _get_api_key()
        if not api_key:
            return "No API token found. Re-run setup.py or set ANTHROPIC_API_KEY."

        import requests
        headers = {
            "x-api-key": api_key,
            "content-type": "application/json",
            "anthropic-version": "2023-06-01"
        }

        body = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": messages
        }

        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=body,
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()

        text_parts = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block["text"])
        return "\n".join(text_parts) if text_parts else "No response."

    except Exception as e:
        return f"Error: {e}"
