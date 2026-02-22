"""
General-purpose handlers for non-command messages.
Routes free-form iMessages to Claude with:
- Conversation history (multi-turn context)
- Live Kalshi data (for trading queries)
- Image analysis (for photo attachments)
- Web search (for current events / news queries)
- Calendar read/write
- Apple Reminders / Tasks
- Timed reminders via `at`
"""

import base64
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import yaml
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load personal config from config.yaml."""
    for candidate in [
        Path(__file__).parent.parent / "config.yaml",
        Path.home() / ".config/imsg-watcher/config.yaml",
    ]:
        if candidate.exists():
            with open(candidate) as f:
                return yaml.safe_load(f)
    return {}

_CONFIG = _load_config()
_PATHS = _CONFIG.get("paths", {})
IMSG = _PATHS.get("imsg", "/opt/homebrew/bin/imsg")

# Keywords that trigger each feature
KALSHI_KEYWORDS = ['kalshi', 'position', 'portfolio', 'trade', 'market', 'contract',
                   'balance', 'profit', 'p&l']

SEARCH_KEYWORDS = ['news', 'latest', 'current', 'today', 'yesterday', 'what happened',
                   'update', 'who won', 'score', 'weather', 'forecast', 'price of',
                   'how much is', 'did they', 'is it true', 'recently', 'this week']

CALENDAR_READ_KEYWORDS = ['my calendar', 'what do i have', 'am i free', 'any events',
                          "what's on my", 'anything scheduled', 'my schedule',
                          'do i have anything', 'events today', 'events tomorrow']

CALENDAR_WRITE_KEYWORDS = ['add to my calendar', 'add to calendar', 'put on my calendar',
                           'schedule a', 'create an event', 'create event', 'block off',
                           'add an event', 'add event', 'calendar event']

REMINDER_KEYWORDS = ['remind me', 'set a reminder', "don't let me forget",
                     'reminder for', 'alert me', 'ping me']

TASK_KEYWORDS = ['add to my list', 'add a task', 'add to reminders', 'add a reminder',
                 'add to inbox', 'add a todo', 'add todo', 'remember to buy',
                 'add to my reminders', 'create a task', 'create a reminder']

HISTORY_LIMIT = _CONFIG.get("watcher", {}).get("history_limit", 10)

AUTH_PROFILES_PATH = Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
PROVIDER_STATE_PATH = Path.home() / ".openclaw" / "state" / "provider_failover.json"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
DEFAULT_ANTHROPIC_MAX_TOKENS = 512
DEFAULT_ANTHROPIC_COOLDOWN_SECONDS = 60
DEFAULT_CODEX_COOLDOWN_SECONDS = 120
DEFAULT_CODEX_TIMEOUT_SECONDS = 120


@dataclass
class ProviderError(Exception):
    provider: str
    kind: str
    message: str
    retry_after_seconds: int | None = None

    def __str__(self) -> str:
        return self.message


def _load_json_file(path: Path) -> dict:
    try:
        with open(path, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _default_provider_state() -> dict:
    return {
        "active_provider": "anthropic",
        "providers": {
            "anthropic": {"cooldown_until": 0, "reason": "", "updated_at": 0},
            "codex": {"cooldown_until": 0, "reason": "", "updated_at": 0},
        },
    }


def _load_provider_state() -> dict:
    state = _default_provider_state()
    raw = _load_json_file(PROVIDER_STATE_PATH)
    if not raw:
        return state
    providers = raw.get("providers")
    if isinstance(providers, dict):
        for provider in ("anthropic", "codex"):
            current = providers.get(provider)
            if isinstance(current, dict):
                state["providers"][provider]["cooldown_until"] = _to_int(current.get("cooldown_until")) or 0
                state["providers"][provider]["reason"] = str(current.get("reason", "")).strip()
                state["providers"][provider]["updated_at"] = _to_int(current.get("updated_at")) or 0
    active = str(raw.get("active_provider", "")).strip().lower()
    if active in {"anthropic", "codex"}:
        state["active_provider"] = active
    return state


def _save_provider_state(state: dict) -> None:
    try:
        PROVIDER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PROVIDER_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except Exception:
        pass


def _provider_cooldown_until(state: dict, provider: str) -> int:
    providers = state.get("providers", {})
    current = providers.get(provider, {})
    if not isinstance(current, dict):
        return 0
    return _to_int(current.get("cooldown_until")) or 0


def _set_provider_cooldown(state: dict, provider: str, cooldown_until: int, reason: str = "") -> None:
    providers = state.setdefault("providers", {})
    current = providers.setdefault(provider, {"cooldown_until": 0, "reason": "", "updated_at": 0})
    if not isinstance(current, dict):
        current = {"cooldown_until": 0, "reason": "", "updated_at": 0}
        providers[provider] = current
    current["cooldown_until"] = max(_to_int(current.get("cooldown_until")) or 0, max(0, int(cooldown_until)))
    current["reason"] = reason[:500]
    current["updated_at"] = int(time.time())


def _clear_provider_cooldown(state: dict, provider: str) -> None:
    providers = state.setdefault("providers", {})
    current = providers.setdefault(provider, {"cooldown_until": 0, "reason": "", "updated_at": 0})
    if not isinstance(current, dict):
        current = {"cooldown_until": 0, "reason": "", "updated_at": 0}
        providers[provider] = current
    current["cooldown_until"] = 0
    current["reason"] = ""
    current["updated_at"] = int(time.time())


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _parse_retry_hint_seconds(text: str) -> int | None:
    lower = (text or "").lower()
    match = re.search(
        r'(?:retry|try again|cooldown)[^\d]{0,20}(\d+)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)',
        lower,
    )
    if not match:
        return None
    amount = _to_int(match.group(1)) or 0
    unit = match.group(2)
    if amount <= 0:
        return None
    if unit.startswith("h"):
        return amount * 3600
    if unit.startswith("m"):
        return amount * 60
    return amount


def _parse_retry_after_seconds(headers, error_text: str, default_seconds: int) -> int:
    retry_after = _header_value(headers, ["retry-after"])
    if retry_after:
        value = _to_int(retry_after)
        if value and value > 0:
            return value
        try:
            parsed = parsedate_to_datetime(retry_after)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delta = int((parsed - datetime.now(timezone.utc)).total_seconds())
            if delta > 0:
                return delta
        except Exception:
            pass

    reset_value = _header_value(headers, [
        "anthropic-ratelimit-requests-reset",
        "x-ratelimit-reset-requests",
        "anthropic-ratelimit-tokens-reset",
        "x-ratelimit-reset-tokens",
    ])
    if reset_value:
        as_int = _to_int(reset_value)
        if as_int:
            # Accept either epoch seconds or epoch milliseconds.
            if as_int > 1_000_000_000_000:
                as_int //= 1000
            if as_int > 1_000_000_000:
                delta = as_int - int(time.time())
                if delta > 0:
                    return delta
        else:
            try:
                parsed_reset = datetime.fromisoformat(reset_value.replace("Z", "+00:00"))
                if parsed_reset.tzinfo is None:
                    parsed_reset = parsed_reset.replace(tzinfo=timezone.utc)
                delta = int((parsed_reset - datetime.now(timezone.utc)).total_seconds())
                if delta > 0:
                    return delta
            except Exception:
                pass

    hint_seconds = _parse_retry_hint_seconds(error_text)
    if hint_seconds:
        return hint_seconds

    return max(1, int(default_seconds))


def _provider_enabled(provider: str) -> bool:
    if provider == "anthropic":
        return True
    if provider == "codex":
        cfg = _CONFIG.get("codex", {})
        enabled = cfg.get("enabled", True)
        if isinstance(enabled, str):
            return enabled.strip().lower() not in {"0", "false", "no", "off"}
        return bool(enabled)
    return False


def _codex_binary() -> str:
    cfg = _CONFIG.get("codex", {})
    bin_path = str(cfg.get("binary", "")).strip()
    if bin_path:
        return bin_path
    return _PATHS.get("codex", "codex")


def _looks_like_anthropic_token(token: str) -> bool:
    val = str(token or "").strip()
    if not val or val.startswith("Symbol("):
        return False
    return val.startswith("sk-ant-")


def _get_anthropic_profile_info() -> dict:
    data = _load_json_file(AUTH_PROFILES_PATH)
    profiles = data.get("profiles", {})
    usage_stats = data.get("usageStats", {})
    last_good = str(data.get("lastGood", {}).get("anthropic", "")).strip()

    candidates = []
    if last_good:
        candidates.append(last_good)
    candidates.append("anthropic:oauth-token")
    if isinstance(profiles, dict):
        for profile_id, profile in profiles.items():
            if isinstance(profile, dict) and profile.get("provider") == "anthropic":
                candidates.append(profile_id)

    now = int(time.time())
    seen = set()
    valid_profiles = []
    for profile_id in candidates:
        if profile_id in seen:
            continue
        seen.add(profile_id)

        profile = profiles.get(profile_id, {})
        if not isinstance(profile, dict):
            continue
        if profile.get("provider") != "anthropic":
            continue

        token = str(profile.get("token") or profile.get("api_key") or profile.get("apiKey") or "").strip()
        if not _looks_like_anthropic_token(token):
            continue

        usage = usage_stats.get(profile_id, {}) if isinstance(usage_stats, dict) else {}
        cooldown_ms = _to_int(usage.get("cooldownUntil")) or 0
        cooldown_until = cooldown_ms // 1000 if cooldown_ms > 0 else 0
        valid_profiles.append({
            "profile_id": profile_id,
            "token": token,
            "cooldown_until": cooldown_until,
        })

    for profile in valid_profiles:
        if int(profile.get("cooldown_until", 0)) <= now:
            return profile

    if valid_profiles:
        return min(valid_profiles, key=lambda profile: int(profile.get("cooldown_until", 0)))

    raise RuntimeError(
        "Anthropic OAuth token not found. Re-auth in OpenClaw: "
        "openclaw models auth login --provider anthropic"
    )


def _effective_provider_cooldown_until(state: dict, provider: str) -> int:
    local_until = _provider_cooldown_until(state, provider)
    if provider == "anthropic":
        try:
            info = _get_anthropic_profile_info()
            local_until = max(local_until, _to_int(info.get("cooldown_until")) or 0)
        except RuntimeError:
            pass
    return local_until


def _provider_attempt_order(state: dict) -> list[str]:
    now = int(time.time())
    anthropic_ready = _effective_provider_cooldown_until(state, "anthropic") <= now
    codex_ready = _effective_provider_cooldown_until(state, "codex") <= now and _provider_enabled("codex")

    if anthropic_ready:
        order = ["anthropic"]
        if _provider_enabled("codex"):
            order.append("codex")
        return order

    if codex_ready:
        return ["codex", "anthropic"]

    order = ["anthropic"]
    if _provider_enabled("codex"):
        order.append("codex")
    return order


def _record_provider_error(state: dict, error: ProviderError) -> None:
    if error.kind != "cooldown":
        return
    fallback_seconds = (
        DEFAULT_ANTHROPIC_COOLDOWN_SECONDS
        if error.provider == "anthropic"
        else DEFAULT_CODEX_COOLDOWN_SECONDS
    )
    retry_after = max(1, int(error.retry_after_seconds or fallback_seconds))
    _set_provider_cooldown(
        state,
        error.provider,
        int(time.time()) + retry_after,
        reason=error.message,
    )


def _classify_codex_error(error_text: str) -> tuple[str, int | None]:
    lower = (error_text or "").lower()
    retry_after = _parse_retry_hint_seconds(lower)
    if any(token in lower for token in ["rate limit", "429", "too many requests", "cooldown"]):
        return ("cooldown", retry_after)
    if any(token in lower for token in ["unauthorized", "forbidden", "login", "authentication", "auth"]):
        return ("auth", retry_after)
    if any(token in lower for token in ["timed out", "timeout", "stream disconnected", "network", "connection"]):
        return ("network", retry_after)
    return ("other", retry_after)


def _to_int(value) -> int | None:
    """Best-effort integer parsing for headers and config values."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except Exception:
        try:
            return int(float(str(value).strip()))
        except Exception:
            return None


def _header_value(headers, candidates: list[str]) -> str:
    """Get the first matching HTTP header value (case-insensitive)."""
    if not headers:
        return ""
    for name in candidates:
        val = headers.get(name)
        if val is not None:
            return str(val).strip()
    try:
        lower = {str(k).lower(): str(v).strip() for k, v in headers.items()}
    except Exception:
        return ""
    for name in candidates:
        val = lower.get(name.lower())
        if val is not None:
            return val
    return ""


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_oauth_token() -> str:
    """Read the currently preferred Anthropic OAuth token from OpenClaw auth profiles."""
    return _get_anthropic_profile_info()["token"]


def _load_memory() -> str:
    """Load MEMORY.md for context injection into Claude prompts."""
    memory_path = Path(__file__).parent.parent / "MEMORY.md"
    try:
        return memory_path.read_text()
    except Exception:
        return ""


# ── History ───────────────────────────────────────────────────────────────────

def _strip_garbled_prefix(text: str) -> str:
    """Strip leading garbled bytes that appear in sent iMessage texts."""
    if not text.startswith('\ufffd'):
        return text
    return re.sub(r'^[\ufffd\x00-\x1f\x7f\x7d\x7e\x02\x03]+', '', text).strip()


def _load_chat_history(chat_id: int, current_text: str, limit: int = HISTORY_LIMIT) -> list:
    """Fetch recent messages and return as Anthropic messages format (oldest first)."""
    try:
        result = subprocess.run(
            [IMSG, 'history', '--chat-id', str(chat_id), '--limit', str(limit), '--json'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []

        raw = []
        for line in result.stdout.strip().splitlines():
            try:
                msg = json.loads(line)
                text = _strip_garbled_prefix((msg.get('text') or '').strip())
                if not text:
                    continue
                raw.append({
                    'role': 'assistant' if msg.get('is_from_me') else 'user',
                    'content': text,
                    'ts': msg.get('created_at', ''),
                })
            except Exception:
                continue

        raw.sort(key=lambda x: x['ts'])

        # Drop the most recent user message (it's the one we're currently processing)
        for i in range(len(raw) - 1, -1, -1):
            if raw[i]['role'] == 'user':
                raw.pop(i)
                break

        messages = [{'role': m['role'], 'content': m['content']} for m in raw]

        # Merge consecutive same-role turns; ensure starts with user
        merged = []
        for m in messages:
            if merged and merged[-1]['role'] == m['role']:
                merged[-1]['content'] += '\n' + m['content']
            else:
                merged.append(dict(m))
        while merged and merged[0]['role'] == 'assistant':
            merged.pop(0)

        return merged
    except Exception:
        return []


# ── Image handling ────────────────────────────────────────────────────────────

def _prepare_image(path: str) -> tuple | None:
    """Convert image to JPEG if needed and return (base64_data, media_type)."""
    try:
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return None

        ext = Path(path).suffix.lower()

        if ext in ('.heic', '.heif'):
            # macOS built-in converter
            tmp = tempfile.mktemp(suffix='.jpg')
            result = subprocess.run(
                ['sips', '-s', 'format', 'jpeg', path, '--out', tmp],
                capture_output=True, timeout=15
            )
            if result.returncode != 0 or not os.path.exists(tmp):
                return None
            jpeg_path, media_type = tmp, 'image/jpeg'
        elif ext in ('.jpg', '.jpeg'):
            jpeg_path, media_type = path, 'image/jpeg'
        elif ext == '.png':
            jpeg_path, media_type = path, 'image/png'
        elif ext == '.gif':
            jpeg_path, media_type = path, 'image/gif'
        elif ext == '.webp':
            jpeg_path, media_type = path, 'image/webp'
        else:
            jpeg_path, media_type = path, 'image/jpeg'

        with open(jpeg_path, 'rb') as f:
            data = base64.standard_b64encode(f.read()).decode('utf-8')

        if ext in ('.heic', '.heif') and os.path.exists(tmp):
            os.unlink(tmp)

        return data, media_type
    except Exception:
        return None


# ── Calendar ──────────────────────────────────────────────────────────────────

def _calendar_get_events(date_offset_days: int = 0) -> str:
    """Fetch Calendar.app events for a given day (0=today, 1=tomorrow, etc.)."""
    skip = '{"US Holidays", "Siri Suggestions", "Birthdays"}'
    script = f'''
tell application "Calendar"
  set targetDate to (current date) + ({date_offset_days} * 86400)
  set dayStart to targetDate
  set hours of dayStart to 0
  set minutes of dayStart to 0
  set seconds of dayStart to 0
  set dayEnd to dayStart + 86400
  set results to {{}}
  repeat with cal in calendars
    if name of cal is not in {skip} then
      repeat with e in (every event of cal whose start date >= dayStart and start date < dayEnd)
        set results to results & {{(summary of e) & " @ " & ((start date of e) as string) & " [" & (name of cal) & "]"}}
      end repeat
    end if
  end repeat
  if (count of results) = 0 then return "No events"
  set AppleScript's text item delimiters to linefeed
  return results as string
end tell
'''
    try:
        result = subprocess.run(['osascript', '-e', script],
                                capture_output=True, text=True, timeout=15)
        return result.stdout.strip() or result.stderr.strip() or "No events"
    except Exception as e:
        return f"[Calendar error: {e}]"


def _calendar_create_event(title: str, date_str: str, start_hour: int, start_min: int,
                            duration_hours: float = 1.0, calendar_name: str = "Home") -> str:
    """Create a Calendar.app event via osascript."""
    duration_seconds = int(duration_hours * 3600)
    script = f'''
tell application "Calendar"
  set targetCal to first calendar whose name is "{calendar_name}"
  set eventDate to date "{date_str}"
  set hours of eventDate to {start_hour}
  set minutes of eventDate to {start_min}
  set seconds of eventDate to 0
  set endDate to eventDate + {duration_seconds}
  tell targetCal
    make new event with properties {{summary:"{title}", start date:eventDate, end date:endDate}}
  end tell
  return "Event created: {title}"
end tell
'''
    try:
        result = subprocess.run(['osascript', '-e', script],
                                capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return result.stdout.strip()
        return f"[Calendar write error: {result.stderr.strip()[:200]}]"
    except Exception as e:
        return f"[Calendar error: {e}]"


# ── Reminders ─────────────────────────────────────────────────────────────────

def _schedule_reminder(text: str, minutes: int, chat_id: int) -> str:
    """Schedule a reminder iMessage using the `at` command."""
    try:
        safe_text = text.replace("'", "\\'").replace('"', '\\"')
        cmd = f'{IMSG} send --chat-id {chat_id} --service imessage --text "⏰ Reminder: {safe_text}"\n'
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
            f.write(cmd)
            tmp = f.name
        os.chmod(tmp, 0o755)
        result = subprocess.run(
            ['at', '-f', tmp, f'now + {minutes} minutes'],
            capture_output=True, text=True, timeout=5
        )
        os.unlink(tmp)
        if result.returncode == 0:
            return f"Reminder set: '{text}' in {minutes} minute(s)."
        return f"[Reminder scheduling failed: {result.stderr[:100]}]"
    except Exception as e:
        return f"[Reminder error: {e}]"


def _add_reminder_task(title: str, notes: str = "", list_name: str = "Reminders",
                       deadline: str = "") -> str:
    """Add a task to Apple Reminders via osascript."""
    notes_line = f'set body of newItem to "{notes}"' if notes else ''
    deadline_line = f'set due date of newItem to date "{deadline}"' if deadline else ''
    script = f'''
tell application "Reminders"
  tell list "{list_name}"
    set newItem to make new reminder with properties {{name:"{title}"}}
    {notes_line}
    {deadline_line}
  end tell
  return "Added to {list_name}: {title}"
end tell
'''
    try:
        result = subprocess.run(['osascript', '-e', script],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip()
        # Fallback: add to default list
        fallback = f'''
tell application "Reminders"
  set newItem to make new reminder with properties {{name:"{title}"}}
  {notes_line}
  return "Added to Reminders: {title}"
end tell
'''
        result2 = subprocess.run(['osascript', '-e', fallback],
                                 capture_output=True, text=True, timeout=10)
        return result2.stdout.strip() or f"[Reminders error: {result.stderr.strip()[:100]}]"
    except Exception as e:
        return f"[Reminders error: {e}]"


# ── Provider failover (Anthropic <-> Codex) ──────────────────────────────────

def _message_content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                block_type = str(block.get("type", "")).strip().lower()
                if block_type == "text":
                    parts.append(str(block.get("text", "")))
                elif block_type == "image":
                    parts.append("[Image attachment omitted in Codex fallback mode]")
                else:
                    parts.append(str(block))
            else:
                parts.append(str(block))
        return "\n".join(p for p in parts if p).strip()
    if content is None:
        return ""
    return str(content)


def _build_codex_prompt(system_prompt: str, messages: list, expect_json: bool = False) -> str:
    lines = []
    if system_prompt:
        lines.append("System instructions:")
        lines.append(system_prompt.strip())
        lines.append("")
    lines.append("Conversation:")
    for message in messages:
        role = str(message.get("role", "user")).strip().upper()
        content = _message_content_to_text(message.get("content"))
        lines.append(f"{role}: {content}")
    if expect_json:
        lines.append("")
        lines.append("Return ONLY valid JSON. Do not use markdown or code fences.")
    return "\n".join(lines).strip()


def _anthropic_request(system_prompt: str, messages: list, max_tokens: int) -> str:
    try:
        profile = _get_anthropic_profile_info()
    except RuntimeError as error:
        raise ProviderError(
            provider="anthropic",
            kind="auth",
            message=str(error),
        )
    model = _CONFIG.get("anthropic", {}).get("model", DEFAULT_ANTHROPIC_MODEL)
    body = json.dumps({
        "model": model,
        "max_tokens": int(max_tokens or DEFAULT_ANTHROPIC_MAX_TOKENS),
        "system": system_prompt,
        "messages": messages,
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {profile['token']}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
    }
    req = urllib.request.Request(ANTHROPIC_MESSAGES_URL, data=body, headers=headers)

    try:
        resp = urllib.request.urlopen(req, timeout=45)
        result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body_text = ""

        if e.code == 429:
            retry_after = _parse_retry_after_seconds(
                e.headers,
                body_text,
                DEFAULT_ANTHROPIC_COOLDOWN_SECONDS,
            )
            raise ProviderError(
                provider="anthropic",
                kind="cooldown",
                message="Anthropic OAuth is cooling down.",
                retry_after_seconds=retry_after,
            )
        if e.code == 401:
            raise ProviderError(
                provider="anthropic",
                kind="auth",
                message="Anthropic OAuth auth failed.",
            )
        if e.code >= 500:
            raise ProviderError(
                provider="anthropic",
                kind="network",
                message=f"Anthropic API is unavailable (HTTP {e.code}).",
            )
        raise ProviderError(
            provider="anthropic",
            kind="other",
            message=f"Anthropic API error (HTTP {e.code}).",
        )
    except urllib.error.URLError:
        raise ProviderError(
            provider="anthropic",
            kind="network",
            message="Couldn't reach Anthropic API.",
        )

    text_parts = []
    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text_parts.append(str(block.get("text", "")).strip())
    text = "\n".join(part for part in text_parts if part).strip()
    if not text:
        raise ProviderError(
            provider="anthropic",
            kind="other",
            message="Anthropic returned an empty response.",
        )
    return text


def _codex_request(system_prompt: str, messages: list, expect_json: bool = False) -> str:
    codex_bin = _codex_binary()
    codex_cfg = _CONFIG.get("codex", {})
    prompt = _build_codex_prompt(system_prompt, messages, expect_json=expect_json)
    timeout_seconds = _to_int(codex_cfg.get("timeout_seconds")) or DEFAULT_CODEX_TIMEOUT_SECONDS
    model = str(codex_cfg.get("model", "")).strip()

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as temp_output:
        output_path = temp_output.name

    cmd = [codex_bin, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-o", output_path]
    if model:
        cmd.extend(["-m", model])
    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(30, int(timeout_seconds)),
            cwd=str(Path(__file__).resolve().parent.parent),
        )
    except FileNotFoundError:
        raise ProviderError(
            provider="codex",
            kind="auth",
            message=f"Codex CLI not found at '{codex_bin}'.",
        )
    except subprocess.TimeoutExpired:
        raise ProviderError(
            provider="codex",
            kind="network",
            message="Codex request timed out.",
        )

    output_text = ""
    try:
        output_text = Path(output_path).read_text(encoding="utf-8").strip()
    except Exception:
        output_text = ""
    finally:
        try:
            os.unlink(output_path)
        except Exception:
            pass

    combined_error = "\n".join(
        part for part in [result.stderr.strip(), result.stdout.strip()] if part
    ).strip()

    if result.returncode != 0:
        kind, retry_after = _classify_codex_error(combined_error)
        raise ProviderError(
            provider="codex",
            kind=kind,
            message=combined_error or "Codex request failed.",
            retry_after_seconds=retry_after,
        )

    if not output_text:
        raise ProviderError(
            provider="codex",
            kind="other",
            message="Codex returned an empty response.",
        )

    return output_text


def _raise_provider_failure(errors: list[ProviderError], state: dict) -> None:
    now = int(time.time())
    anthropic_remaining = max(0, _effective_provider_cooldown_until(state, "anthropic") - now)
    codex_enabled = _provider_enabled("codex")
    codex_remaining = (
        max(0, _effective_provider_cooldown_until(state, "codex") - now) if codex_enabled else 0
    )

    if anthropic_remaining > 0 and codex_enabled and codex_remaining > 0:
        raise RuntimeError(
            "Both providers are cooling down. "
            f"Anthropic retry in {_format_duration(anthropic_remaining)}; "
            f"Codex retry in {_format_duration(codex_remaining)}."
        )

    non_cooldown_errors = [err for err in errors if err.kind != "cooldown"]
    if non_cooldown_errors:
        primary = non_cooldown_errors[0]
        if primary.provider == "codex" and primary.kind == "auth":
            if anthropic_remaining > 0:
                raise RuntimeError(
                    "Anthropic is cooling down and Codex OAuth is unavailable. "
                    f"Anthropic retry in {_format_duration(anthropic_remaining)}."
                )
        if primary.provider == "anthropic" and primary.kind == "auth":
            raise RuntimeError("Anthropic OAuth auth failed and Codex fallback was unavailable.")
        raise RuntimeError(primary.message)

    if anthropic_remaining > 0:
        raise RuntimeError(f"Anthropic is cooling down. Retry in {_format_duration(anthropic_remaining)}.")
    if codex_enabled and codex_remaining > 0:
        raise RuntimeError(f"Codex is cooling down. Retry in {_format_duration(codex_remaining)}.")
    raise RuntimeError("No model provider was available.")


def _model_request_with_failover(
    system_prompt: str,
    messages: list,
    max_tokens: int,
    expect_json: bool = False,
) -> str:
    state = _load_provider_state()
    errors: list[ProviderError] = []

    for provider in _provider_attempt_order(state):
        if not _provider_enabled(provider):
            continue

        cooldown_until = _effective_provider_cooldown_until(state, provider)
        now = int(time.time())
        if cooldown_until > now:
            errors.append(
                ProviderError(
                    provider=provider,
                    kind="cooldown",
                    message=f"{provider} cooldown active",
                    retry_after_seconds=cooldown_until - now,
                )
            )
            continue

        try:
            if provider == "anthropic":
                response = _anthropic_request(system_prompt, messages, max_tokens=max_tokens)
            elif provider == "codex":
                response = _codex_request(system_prompt, messages, expect_json=expect_json)
            else:
                continue

            _clear_provider_cooldown(state, provider)
            state["active_provider"] = provider
            _save_provider_state(state)
            return response
        except ProviderError as error:
            errors.append(error)
            _record_provider_error(state, error)
            continue

    _save_provider_state(state)
    _raise_provider_failure(errors, state)
    return ""


# ── JSON extraction via provider failover ─────────────────────────────────────

def _extract_json(instruction: str) -> dict:
    """Run structured extraction via Anthropic with Codex fallback."""
    response_text = _model_request_with_failover(
        system_prompt="You are a structured data extractor. Reply with ONLY valid JSON, no explanation.",
        messages=[{"role": "user", "content": instruction}],
        max_tokens=256,
        expect_json=True,
    )
    cleaned = re.sub(r'^```(?:json)?\s*', '', response_text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        return {}


# ── Kalshi live data ──────────────────────────────────────────────────────────

def _fetch_kalshi_live() -> str:
    """Fetch live Kalshi balance + positions with current prices."""
    kalshi_cfg = _CONFIG.get("kalshi", {})
    if not kalshi_cfg.get("enabled"):
        return ""

    key_id = kalshi_cfg.get("key_id", "")
    private_key_path = os.path.expanduser(kalshi_cfg.get("private_key_path", ""))
    python = _PATHS.get("python", "/opt/homebrew/bin/python3")

    script = f"""
import json, sys
for p in ['/opt/homebrew/lib/python3.13/site-packages',
          '/opt/homebrew/lib/python3.14/site-packages',
          '/usr/local/lib/python3.13/site-packages']:
    sys.path.insert(0, p)
try:
    from kalshi_python import Configuration, KalshiClient
    config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
    with open("{private_key_path}", "r") as f:
        config.private_key_pem = f.read()
    config.api_key_id = "{key_id}"
    client = KalshiClient(config)

    cash = client.get_balance().balance / 100.0
    resp = client._portfolio_api.get_positions_without_preload_content(limit=100)
    positions = [p for p in json.loads(resp.read()).get("market_positions", [])
                 if int(p.get("position", 0)) != 0]

    lines = [f"LIVE KALSHI DATA:", f"Cash: ${{cash:.2f}}", f"Open positions: {{len(positions)}}"]
    total_cost = total_val = 0.0
    exit_candidates = []

    for p in positions:
        ticker = p.get("ticker", "?")
        qty = int(p.get("position", 0))
        side = "YES" if qty >= 0 else "NO"
        abs_qty = abs(qty)
        cost = float(p.get("market_exposure_dollars", 0))
        try:
            url = f"https://api.elections.kalshi.com/trade-api/v2/markets/{{ticker}}"
            mkt = json.loads(client.call_api("GET", url).read()).get("market", {{}})
            bid = mkt.get("yes_bid" if side == "YES" else "no_bid", 0)
            cur_val = abs_qty * bid / 100.0
            pnl = cur_val - cost
            pct = (pnl / cost * 100) if cost else 0
            flag = " [EXIT CANDIDATE]" if pct >= 20 else ""
            lines.append(f"  {{ticker}}: {{abs_qty}}x {{side}} | cost ${{cost:.2f}} | now ${{cur_val:.2f}} | P&L ${{pnl:+.2f}} ({{pct:+.0f}}%){{flag}}")
            total_cost += cost
            total_val += cur_val
            if pct >= 20:
                exit_candidates.append(f"{{ticker}} +{{pct:.0f}}%")
        except:
            lines.append(f"  {{ticker}}: {{abs_qty}}x {{side}} | cost ${{cost:.2f}} | [price unavailable]")
            total_cost += cost

    lines.append(f"TOTAL: cost ${{total_cost:.2f}} | now ${{total_val:.2f}} | unrealized P&L ${{total_val - total_cost:+.2f}}")
    if exit_candidates:
        lines.append(f"EXIT CANDIDATES (>=20%): {{', '.join(exit_candidates)}}")
    print("\\n".join(lines))
except Exception as e:
    print(f"ERROR: {{e}}", file=sys.stderr)
    sys.exit(1)
"""
    try:
        result = subprocess.run([python, '-c', script],
                                capture_output=True, text=True, timeout=20)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return f"[Kalshi API error: {result.stderr.strip()[:200]}]"
    except Exception as e:
        return f"[Kalshi fetch failed: {e}]"


# ── Web search ────────────────────────────────────────────────────────────────

def _web_search(query: str, num_results: int = 4) -> str:
    """Search DuckDuckGo lite and return top result snippets."""
    try:
        data = urllib.parse.urlencode({'q': query}).encode()
        req = urllib.request.Request(
            'https://lite.duckduckgo.com/lite/',
            data=data,
            headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
                'Content-Type': 'application/x-www-form-urlencoded',
            }
        )
        resp = urllib.request.urlopen(req, timeout=10)
        html = resp.read().decode('utf-8', errors='ignore')

        titles = re.findall(r"class='result-link'[^>]*>(.*?)</a>", html, re.DOTALL)
        snippets = re.findall(r"class='result-snippet'[^>]*>(.*?)</td>", html, re.DOTALL)

        results = []
        for title, snippet in zip(titles, snippets):
            title = re.sub(r'<[^>]+>', '', title).strip()
            snippet = re.sub(r'<[^>]+>', '', snippet).strip()
            snippet = re.sub(r'\s+', ' ', snippet).replace('&#x27;', "'").replace('&amp;', '&')
            if title and len(title) > 3:
                results.append(f"• {title}: {snippet[:200]}" if snippet else f"• {title}")
            if len(results) >= num_results:
                break

        if not results:
            return ""
        return f"Web search results for '{query}':\n" + "\n".join(results)
    except Exception as e:
        return f"[Search failed: {e}]"


def _needs_search(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in SEARCH_KEYWORDS)


# ── Claude API call ───────────────────────────────────────────────────────────

def _call_claude(prompt: str, live_context: str = "", history: list = None,
                 images: list = None) -> str:
    """Call Anthropic with automatic Codex OAuth fallback."""
    memory = _load_memory()
    live_section = f"\n\n<live_data>\n{live_context}\n</live_data>" if live_context else ""
    assistant_name = _CONFIG.get("user", {}).get("assistant_name", "Rout")
    user_name = _CONFIG.get("user", {}).get("name", "")

    system = f"""You are {assistant_name}, an AI assistant responding via iMessage.

You have long-term memory and recent conversation history for context.

<memory>
{memory}
</memory>{live_section}

Be warm, concise, and natural — like texting a capable friend. \
One message per response. When live_data is present, use it as authoritative."""

    messages = list(history or [])
    max_tokens = _to_int(_CONFIG.get("anthropic", {}).get("max_tokens")) or DEFAULT_ANTHROPIC_MAX_TOKENS

    if images:
        content = []
        for img_data, img_type in images:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": img_type, "data": img_data}
            })
        content.append({"type": "text", "text": prompt or "What's in this image?"})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": prompt})

    return _model_request_with_failover(
        system_prompt=system,
        messages=messages,
        max_tokens=max_tokens,
    )


# ── Send ──────────────────────────────────────────────────────────────────────

def _send_to_chat(text: str, chat_id: int):
    subprocess.run(
        [IMSG, "send", "--chat-id", str(chat_id), "--service", "imessage", "--text", text],
        timeout=10, check=False, capture_output=True
    )


# ── Main handler ──────────────────────────────────────────────────────────────

def claude_command(text: str) -> None:
    """
    Route free-form iMessages to Claude.
    Parses [CHAT_ID:N] and [ATTACHMENTS:[...]] tags injected by the watcher.
    """
    try:
        # Extract chat_id
        chat_id = _CONFIG.get("chats", {}).get("personal_id", 1)
        m = re.match(r'\[CHAT_ID:(\d+)\]\s*', text)
        if m:
            chat_id = int(m.group(1))
            text = text[m.end():]

        # Extract attachment paths
        attachment_paths = []
        m = re.match(r'\[ATTACHMENTS:(\[.*?\])\]\s*', text)
        if m:
            try:
                attachment_paths = json.loads(m.group(1))
            except Exception:
                attachment_paths = []
            text = text[m.end():]

        # Strip [From Name] tag for group chats
        m = re.match(r'\[From \w+\]\s*', text)
        if m:
            text = text[m.end():]

        history = _load_chat_history(chat_id, text)

        images = []
        for path in attachment_paths:
            result = _prepare_image(path)
            if result:
                images.append(result)

        live_ctx = ""
        lower = text.lower()

        # ── Kalshi live data ──────────────────────────────────────────────────
        if any(kw in lower for kw in KALSHI_KEYWORDS):
            live_ctx += _fetch_kalshi_live()

        # ── Calendar read ─────────────────────────────────────────────────────
        elif any(kw in lower for kw in CALENDAR_READ_KEYWORDS):
            offset = 1 if any(w in lower for w in ['tomorrow', 'tmrw']) else 0
            if 'this week' in lower or 'week' in lower:
                events = '\n'.join(_calendar_get_events(i) for i in range(7))
            else:
                events = _calendar_get_events(offset)
            label = "Tomorrow" if offset == 1 else "Today"
            live_ctx += f"\nCALENDAR ({label}): {events}"

        # ── Calendar write ────────────────────────────────────────────────────
        elif any(kw in lower for kw in CALENDAR_WRITE_KEYWORDS):
            extracted = _extract_json(
                f'Extract calendar event from: "{text}"\n'
                f'Today is {__import__("datetime").date.today().strftime("%A, %B %d, %Y")}.\n'
                f'Return JSON: {{"title": str, "date": "Month Day Year", '
                f'"start_hour": int (24h), "start_min": int, '
                f'"duration_hours": float, "calendar": "Home or Work"}}'
            )
            if extracted.get('title') and extracted.get('date'):
                result = _calendar_create_event(
                    title=extracted['title'],
                    date_str=extracted['date'],
                    start_hour=extracted.get('start_hour', 12),
                    start_min=extracted.get('start_min', 0),
                    duration_hours=extracted.get('duration_hours', 1.0),
                    calendar_name=extracted.get('calendar', 'Home')
                )
                live_ctx += f"\nACTION RESULT: {result}"
            else:
                live_ctx += "\nACTION RESULT: Could not parse event details — please be more specific."

        # ── Timed reminder ────────────────────────────────────────────────────
        elif any(kw in lower for kw in REMINDER_KEYWORDS):
            extracted = _extract_json(
                f'Extract reminder from: "{text}"\n'
                f'Return JSON: {{"reminder_text": str, "minutes_from_now": int}}\n'
                f'Examples: "in 30 minutes"->30, "in 2 hours"->120.'
            )
            mins = extracted.get('minutes_from_now', 0)
            reminder_text = extracted.get('reminder_text', text)
            if mins and mins > 0:
                result = _schedule_reminder(reminder_text, mins, chat_id)
                live_ctx += f"\nACTION RESULT: {result}"
            else:
                live_ctx += "\nACTION RESULT: Couldn't determine reminder time — be specific (e.g. 'in 30 minutes')."

        # ── Apple Reminders task ──────────────────────────────────────────────
        elif any(kw in lower for kw in TASK_KEYWORDS):
            extracted = _extract_json(
                f'Extract task from: "{text}"\n'
                f'Return JSON: {{"title": str, "notes": str, "deadline": str or null}}'
            )
            if extracted.get('title'):
                result = _add_reminder_task(
                    title=extracted['title'],
                    notes=extracted.get('notes', ''),
                    deadline=extracted.get('deadline') or ''
                )
                live_ctx += f"\nACTION RESULT: {result}"
            else:
                live_ctx += "\nACTION RESULT: Couldn't parse task — please try again."

        # ── Web search ────────────────────────────────────────────────────────
        elif _needs_search(text) and not images:
            search_results = _web_search(text)
            if search_results and not search_results.startswith('['):
                live_ctx += f"\n{search_results}"

        response = _call_claude(text, live_context=live_ctx.strip(), history=history,
                                images=images if images else None)
        _send_to_chat(response, chat_id)
        return None

    except RuntimeError as e:
        # Clean user-facing errors from _call_claude (rate limit, auth, etc.)
        _send_to_chat(str(e), chat_id)
        return None
    except Exception as e:
        # Unexpected error — log full details, send friendly message
        import traceback
        err_detail = traceback.format_exc()
        # Write to log file for debugging
        log_path = os.path.expanduser("~/.openclaw/workspace/imsg_watcher.log")
        try:
            with open(log_path, "a") as f:
                f.write(f"[claude_command ERROR] {err_detail}\n")
        except Exception:
            pass
        _send_to_chat("Something went wrong on my end — I couldn't process that. Try again? 🤷", chat_id)
        return None


def help_command(args: str = "") -> str:
    return """Available commands:

(Or just text naturally — I'll understand)

Kalshi (if configured):
  kalshi: portfolio    - Balance + open positions
  kalshi: positions    - Positions with P&L
  kalshi: markets      - Top opportunities from cache
  kalshi: cache        - Research cache status

System:
  help                 - This message
  status               - Watcher status
  ping                 - Test connectivity"""
