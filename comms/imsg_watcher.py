#!/usr/bin/env python3
"""
Rout iMessage Watcher v4 — push transport + polling fallback
================================================================
Built on proven v3 architecture. v4 adds:
  - BlueBubbles Socket.IO push transport (real-time, <50ms latency)
  - BB REST API sending (faster + more reliable than osascript)
  - Automatic fallback to polling if BB is unreachable
  - Thread-safe dedup and circuit breaker for push concurrency
  - All v3 features preserved: audit log, circuit breaker, SIGTERM

Transport priority:
  1. BlueBubbles push (Socket.IO) — if configured and connected
  2. Polling (imsg CLI) — automatic fallback
  Send priority:
  1. BlueBubbles REST API — if configured
  2. osascript — primary legacy send
  3. imsg CLI — last resort fallback
"""

import subprocess
import json
import hashlib
import re
import yaml
import os
import signal
import sys
import importlib.util
import time
import shutil
import inspect
import threading
from pathlib import Path
from datetime import datetime
from collections import deque
from typing import Optional, Callable, Dict, Set, Tuple

# ============================================================
# HANDLER SIGNATURE CACHE (reduce reflection overhead)
# ============================================================

_HANDLER_SIG_CACHE: Dict[str, inspect.Signature] = {}

def _get_handler_sig(func: Callable) -> Optional[inspect.Signature]:
    """Get cached handler signature, computing only on first access.

    Avoids expensive inspect.signature() calls on every message dispatch.
    Returns None if signature cannot be inspected.
    """
    func_id = id(func)
    if func_id not in _HANDLER_SIG_CACHE:
        try:
            _HANDLER_SIG_CACHE[func_id] = inspect.signature(func)
        except (TypeError, ValueError):
            return None
    return _HANDLER_SIG_CACHE[func_id]


def _normalize_command_text(text: str) -> str:
    """Normalize command text for robust trigger matching."""
    normalized = (text or "").strip().lower()
    normalized = re.sub(r"\s*:\s*", ":", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _compile_trigger_regex(trigger: str) -> re.Pattern:
    """Compile a trigger pattern that tolerates whitespace and colon spacing."""
    normalized = _normalize_command_text(trigger)
    parts = []
    for ch in normalized:
        if ch == " ":
            parts.append(r"\s+")
        elif ch == ":":
            parts.append(r"\s*:\s*")
        else:
            parts.append(re.escape(ch))
    body = "".join(parts)
    return re.compile(rf"^\s*{body}(?:\s+(?P<args>.*))?\s*$", re.IGNORECASE | re.DOTALL)

# Ensure project root is in sys.path so cross-module imports work
# regardless of how the script is invoked (python script.py vs python -m)
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Conditional import — BB push is optional
try:
    from comms.bb_push import BlueBubblesPush
    HAS_BB_PUSH = True
except ImportError:
    HAS_BB_PUSH = False

# ============================================================
# CONFIGURATION
# ============================================================

OPENCLAW_DIR = Path(
    os.environ.get("ROUT_OPENCLAW_DIR", str(Path.home() / ".openclaw"))
).expanduser()
LOG_DIR = OPENCLAW_DIR / "logs"
LOG_FILE = LOG_DIR / "imsg_watcher.log"
STATE_FILE = OPENCLAW_DIR / "state" / ".imsg_watcher_state_v3"
AUDIT_LOG = LOG_DIR / "imsg_audit.jsonl"
POLL_INTERVAL = 2
HISTORY_LIMIT = 20

DEFAULT_WORKSPACE = Path(__file__).resolve().parent.parent

LOG_DIR.mkdir(parents=True, exist_ok=True)
(OPENCLAW_DIR / "state").mkdir(parents=True, exist_ok=True)


# ============================================================
# USER CONFIG (from config.yaml)
# ============================================================

def _load_user_config() -> dict:
    try:
        cfg_path = OPENCLAW_DIR / "config.yaml"
        with open(cfg_path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

_USER_CFG = _load_user_config()


def _resolve_workspace() -> Path:
    override = os.environ.get("ROUT_WORKSPACE")
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(DEFAULT_WORKSPACE)
    for candidate in candidates:
        if (candidate / "imsg_commands.yaml").exists() and (candidate / "handlers").is_dir():
            return candidate
    return DEFAULT_WORKSPACE


def _resolve_imsg_bin(cfg: dict) -> str:
    paths_cfg = cfg.get("paths", {}) if isinstance(cfg, dict) else {}
    override = paths_cfg.get("imsg", "")
    candidates = [override, shutil.which("imsg"), "/opt/homebrew/bin/imsg", "/usr/local/bin/imsg"]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "imsg"


WORKSPACE = _resolve_workspace()
COMMANDS_CONFIG = WORKSPACE / "imsg_commands.yaml"
IMSG_BIN = _resolve_imsg_bin(_USER_CFG)
OSASCRIPT_BIN = shutil.which("osascript") or "/usr/bin/osascript"
os.environ["ROUT_WORKSPACE"] = str(WORKSPACE)

if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

KNOWN_SENDERS: Dict[str, str] = _USER_CFG.get("known_senders", {})

_chats_cfg = _USER_CFG.get("chats", {})
_personal_id: int = _chats_cfg.get("personal_id", 1)
_group_ids: list = _chats_cfg.get("group_ids", [])
CHAT_IDS: list = [_personal_id] + _group_ids

_handles_cfg = _USER_CFG.get("chat_handles", {})
CHAT_HANDLES: Dict[int, tuple] = {int(k): tuple(v) for k, v in _handles_cfg.items()}


# ============================================================
# AUDIT LOGGING
# ============================================================

def audit_log(event_type: str, data: dict):
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "event": event_type,
        **data
    }
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ============================================================
# APPLESCRIPT SEND (legacy — proven reliable from launchd)
# ============================================================

_UNSAFE_CHARS = re.compile(r'[\x00-\x1f]')

def _osascript_send(text: str, chat_id: int) -> bool:
    text = _UNSAFE_CHARS.sub("", text)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')

    handle_info = CHAT_HANDLES.get(chat_id)
    if not handle_info:
        return False

    handle, handle_type = handle_info
    handle = _UNSAFE_CHARS.sub("", handle).replace('"', '\\"')

    if handle_type == "buddy":
        script = f'''tell application "Messages"
    set s to 1st service whose service type = iMessage
    set b to buddy "{handle}" of s
    send "{escaped}" to b
end tell'''
    else:
        script = f'''tell application "Messages"
    set c to (1st chat whose id = "{handle}")
    send "{escaped}" to c
end tell'''

    try:
        result = subprocess.run(
            [OSASCRIPT_BIN, "-e", script],
            timeout=30, capture_output=True
        )
        return result.returncode == 0
    except Exception:
        return False


# ============================================================
# CIRCUIT BREAKER (graceful cooldown, persistent state, thread-safe)
# ============================================================

CIRCUIT_BREAKER_FILE = OPENCLAW_DIR / "state" / "circuit_breaker.json"

class CircuitBreaker:
    """Rate limiter with persistent cooldown. Thread-safe for push + polling."""

    def __init__(self, max_sends: int = 8, window_seconds: int = 60):
        self.max_sends = max_sends
        self.window = window_seconds
        self._send_timestamps: list = []
        self.cooldown_until: float = 0
        self.trip_count: int = 0
        self._lock = threading.Lock()
        self._load_state()

    def _load_state(self):
        if CIRCUIT_BREAKER_FILE.exists():
            try:
                with open(CIRCUIT_BREAKER_FILE, "r") as f:
                    state = json.load(f)
                self.cooldown_until = state.get("cooldown_until", 0)
                self.trip_count = state.get("trip_count", 0)
            except (json.JSONDecodeError, KeyError):
                pass

    def _save_state(self):
        try:
            with open(CIRCUIT_BREAKER_FILE, "w") as f:
                json.dump({
                    "cooldown_until": self.cooldown_until,
                    "trip_count": self.trip_count,
                    "last_updated": datetime.utcnow().isoformat()
                }, f)
        except Exception:
            pass

    def can_send(self) -> bool:
        with self._lock:
            now = time.time()
            if now < self.cooldown_until:
                return False
            if self.trip_count > 0 and now >= self.cooldown_until:
                self.trip_count = 0
                self._save_state()
            return True

    def record_send(self) -> bool:
        with self._lock:
            now = time.time()
            if now < self.cooldown_until:
                return False
            if self.trip_count > 0 and now >= self.cooldown_until:
                self.trip_count = 0
            self._send_timestamps = [t for t in self._send_timestamps if now - t < self.window]
            if len(self._send_timestamps) >= self.max_sends:
                self._trip()
                return False
            self._send_timestamps.append(now)
            return True

    def _trip(self):
        self.trip_count += 1
        cooldown = min(60 * (2 ** (self.trip_count - 1)), 3600)
        self.cooldown_until = time.time() + cooldown
        self._send_timestamps.clear()
        self._save_state()
        audit_log("circuit_breaker_trip", {
            "trip_count": self.trip_count,
            "cooldown_seconds": cooldown,
        })


# ============================================================
# COMMAND WATCHER (v4 — push transport + polling fallback)
# ============================================================

class CommandWatcher:
    def __init__(self):
        self.workspace = WORKSPACE
        self.config = self._load_config(COMMANDS_CONFIG)
        self._trigger_patterns = self._build_trigger_patterns()
        self.handlers = self._load_handlers()
        self.processed_commands: Set[str] = self._load_processed_commands()
        self.circuit_breaker = CircuitBreaker(
            max_sends=_USER_CFG.get("circuit_breaker_max", 8),
            window_seconds=_USER_CFG.get("circuit_breaker_window", 60),
        )

        # Thread-safe access to processed_commands + state file
        self._dedup_lock = threading.Lock()
        self._state_lock = threading.Lock()

        # Cross-transport content dedup: prevents push+poll processing same msg
        # Stores (sender, text_hash) with expiry timestamps
        self._content_dedup: Dict[str, float] = {}
        # Keep this comfortably above reduced polling cadence (~30s) so
        # push+poll cannot race the same inbound text into duplicate runs.
        self._content_dedup_ttl = 120  # seconds
        self._content_dedup_last_cleanup = 0.0  # timestamp of last eviction sweep
        self._content_dedup_cleanup_interval = 30  # sweep every 30s, not every message

        # Per-chat processing lock: prevents concurrent LLM calls for same chat
        self._chat_locks: Dict[int, threading.Lock] = {}
        self._chat_locks_lock = threading.Lock()
        # Per-chat message queue: holds messages that arrive while chat is processing
        # Bounded to 5 per chat to prevent memory bloat under extreme load
        self._chat_queues: Dict[int, deque] = {}
        self._CHAT_QUEUE_MAX = 5

        # BlueBubbles push transport
        self._bb = None
        self._push_active = False
        if HAS_BB_PUSH:
            bb_cfg = _USER_CFG.get("bluebubbles", {})
            if bb_cfg.get("enabled", False):
                self._bb = BlueBubblesPush(
                    config=_USER_CFG,
                    on_message=self._on_push_message,
                    audit_log_fn=audit_log,
                )

    def _build_trigger_patterns(self) -> list[Tuple[str, str, re.Pattern]]:
        """Build trigger->command regex patterns (longest trigger first)."""
        registry = self.config.get("commands", {})
        patterns: list[Tuple[str, str, re.Pattern]] = []
        seen: Set[Tuple[str, str]] = set()

        for command_key, details in registry.items():
            candidates = [command_key]
            if ":" in command_key:
                candidates.append(command_key.replace(":", ": "))

            trigger = details.get("trigger", "") if isinstance(details, dict) else ""
            if isinstance(trigger, str) and trigger.strip():
                candidates.append(trigger.strip())
            elif isinstance(trigger, list):
                for trig in trigger:
                    if isinstance(trig, str) and trig.strip():
                        candidates.append(trig.strip())

            for candidate in candidates:
                normalized = _normalize_command_text(candidate)
                dedup_key = (normalized, command_key)
                if not normalized or dedup_key in seen:
                    continue
                seen.add(dedup_key)
                patterns.append((normalized, command_key, _compile_trigger_regex(candidate)))

        patterns.sort(key=lambda item: len(item[0]), reverse=True)
        return patterns

    def _load_config(self, path: Path) -> Dict:
        try:
            with open(path, 'r') as f:
                data = yaml.safe_load(f) or {}
                if not isinstance(data, dict):
                    raise ValueError("Command config is not a mapping")
                data.setdefault("commands", {})
                return data
        except Exception as e:
            self._log(f"Failed to load config at {path}: {e}")
            sys.exit(1)

    def _load_handlers(self) -> Dict[str, Callable]:
        handlers = {}
        handlers_dir = self.workspace / "handlers"
        if not handlers_dir.exists():
            self._log(f"Handlers directory not found: {handlers_dir}")
            return handlers
        # Ensure workspace root is on sys.path so handler imports resolve
        workspace_str = str(self.workspace)
        if workspace_str not in sys.path:
            sys.path.insert(0, workspace_str)
            self._log(f"Added to sys.path: {workspace_str}")
        for module_file in handlers_dir.glob("*.py"):
            if module_file.name == "__init__.py":
                continue
            module_name = module_file.stem
            try:
                spec = importlib.util.spec_from_file_location(
                    f"handlers.{module_name}", module_file
                )
                module = importlib.util.module_from_spec(spec)
                sys.modules[f"handlers.{module_name}"] = module
                spec.loader.exec_module(module)
                for attr_name in dir(module):
                    if attr_name.endswith("_command"):
                        attr = getattr(module, attr_name)
                        if callable(attr):
                            handler_key = f"{module_name}.{attr_name}"
                            handlers[handler_key] = attr
                            self._log(f"Loaded: {handler_key}")
            except ImportError as e:
                self._log(f"FATAL: Failed to import handler {module_name}: {e}")
                raise SystemExit(f"Handler import failed: {module_name} — {e}")
            except Exception as e:
                self._log(f"ERROR: Failed to load {module_name}: {e}")
                import traceback
                self._log(traceback.format_exc())
        return handlers

    def _load_processed_commands(self) -> Set[str]:
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE, 'r') as f:
                    data = json.load(f)
                    return set(data.get('processed', []))
        except Exception:
            pass
        return set()

    def _save_processed_commands(self):
        with self._state_lock:
            try:
                with open(STATE_FILE, 'w') as f:
                    json.dump({
                        'processed': list(self.processed_commands),
                        'saved_at': datetime.now().isoformat()
                    }, f)
            except Exception as e:
                self._log(f"Failed to save state: {e}")

    def _mark_processed(self, cmd_id: str) -> bool:
        """Atomically check-and-mark a command as processed. Returns True if new."""
        with self._dedup_lock:
            if cmd_id in self.processed_commands:
                return False
            self.processed_commands.add(cmd_id)
            self._save_processed_commands()
        return True

    def _get_chat_lock(self, chat_id: int) -> threading.Lock:
        """Get or create a per-chat lock to prevent concurrent processing."""
        with self._chat_locks_lock:
            if chat_id not in self._chat_locks:
                self._chat_locks[chat_id] = threading.Lock()
            return self._chat_locks[chat_id]

    def _enqueue_chat_message(self, chat_id: int, item: tuple) -> bool:
        """Queue a message for later processing if chat is busy.
        Returns True if queued, False if queue is full (message dropped with warning)."""
        with self._chat_locks_lock:
            if chat_id not in self._chat_queues:
                self._chat_queues[chat_id] = deque(maxlen=self._CHAT_QUEUE_MAX)
            q = self._chat_queues[chat_id]
            if len(q) >= self._CHAT_QUEUE_MAX:
                self._log(f"WARNING: chat {chat_id} queue full ({self._CHAT_QUEUE_MAX}), dropping oldest message")
                q.popleft()  # Drop oldest to make room
            q.append(item)
            return True

    def _dequeue_chat_message(self, chat_id: int) -> Optional[tuple]:
        """Pop next queued message for a chat, or None if empty."""
        with self._chat_locks_lock:
            q = self._chat_queues.get(chat_id)
            if q:
                return q.popleft()
            return None

    @staticmethod
    def _normalize_sender(sender: str) -> str:
        """Normalize sender address for consistent dedup across transports.
        Strips +1 country code, non-digit chars for phone numbers.
        Lowercases email addresses."""
        if not sender:
            return ""
        s = sender.strip()
        # Email-like
        if "@" in s:
            return s.lower()
        # Phone-like: strip everything except digits, then take last 10
        digits = re.sub(r'\D', '', s)
        if len(digits) >= 10:
            return digits[-10:]
        return s.lower()

    def _content_dedup_check(self, sender: str, text: str) -> bool:
        """Cross-transport dedup. Returns True if this is a duplicate (already seen)."""
        now = time.time()
        key = f"{self._normalize_sender(sender)}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
        with self._dedup_lock:
            # Periodic eviction sweep — not on every message (O(n) scan throttled)
            if now - self._content_dedup_last_cleanup > self._content_dedup_cleanup_interval:
                expired = [k for k, exp in self._content_dedup.items() if now > exp]
                for k in expired:
                    del self._content_dedup[k]
                self._content_dedup_last_cleanup = now
            # Check and mark
            if key in self._content_dedup and now <= self._content_dedup[key]:
                return True  # duplicate
            self._content_dedup[key] = now + self._content_dedup_ttl
            return False  # new

    def _get_command_id(self, msg: Dict) -> str:
        msg_id = msg.get('id', 0)
        timestamp = msg.get('created_at', '')
        return f"{msg_id}:{timestamp}"

    def parse_command(self, text: str) -> Optional[Tuple[str, str]]:
        text = text.strip()
        if not text:
            return None
        registry = self.config.get('commands', {})
        trigger_patterns = getattr(self, "_trigger_patterns", None)
        if trigger_patterns is None:
            trigger_patterns = self._build_trigger_patterns()
            self._trigger_patterns = trigger_patterns

        # First pass: trigger-aware matching from imsg_commands.yaml.
        for _, command_key, pattern in trigger_patterns:
            match = pattern.match(text)
            if match:
                args = (match.group("args") or "").strip()
                return (command_key, args)

        if ':' not in text:
            word = text.split()[0].lower()
            if word in registry:
                args = text[len(word):].strip()
                return (word, args)
            return None

        match = re.match(r'(\w+)\s*:\s*(.*)', text, re.DOTALL)
        if not match:
            return None

        prefix = match.group(1).lower()
        rest = match.group(2).strip()
        first_word = rest.split()[0].lower() if rest.split() else ""
        extra_args = ' '.join(rest.split()[1:]) if len(rest.split()) > 1 else ""

        canonical_key = f"{prefix}:{first_word}" if first_word else prefix
        if canonical_key in registry:
            return (canonical_key, extra_args)

        if prefix in registry:
            return (prefix, rest)

        if first_word:
            swapped_key = f"{first_word}:{prefix}"
            if swapped_key in registry:
                return (swapped_key, extra_args)

        for key in registry:
            if key.startswith(f"{prefix}:") and first_word and key.endswith(f":{first_word}"):
                return (key, extra_args)

        return None

    def _invoke_handler(self, handler: Callable, args: str, message: str, sender: str, metadata: dict):
        signature = _get_handler_sig(handler)
        if signature is None:
            return handler(args)

        params = signature.parameters
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        call_kwargs = {}

        if accepts_kwargs or "args" in params:
            call_kwargs["args"] = args
        if accepts_kwargs or "message" in params:
            call_kwargs["message"] = message
        if accepts_kwargs or "sender" in params:
            call_kwargs["sender"] = sender
        if accepts_kwargs or "metadata" in params:
            call_kwargs["metadata"] = metadata

        if call_kwargs:
            return handler(**call_kwargs)
        return handler(args)

    def execute(self, command_key: str, args: str, message: str = "", sender: str = "", metadata: dict = None) -> str:
        registry = self.config.get('commands', {})

        if command_key not in registry:
            return f"Unknown command: '{command_key}'. Type 'help' for available commands."

        cmd_config = registry[command_key]
        handler_path = cmd_config.get('handler')

        if not handler_path or handler_path not in self.handlers:
            loaded = ', '.join(sorted(self.handlers.keys())) or '(none)'
            self._log(f"Handler not found for '{command_key}': wanted '{handler_path}', loaded: [{loaded}]")
            return f"Handler not found for '{command_key}'"

        try:
            handler = self.handlers[handler_path]
            response = self._invoke_handler(
                handler,
                args=args,
                message=message,
                sender=sender,
                metadata=metadata or {},
            )
            audit_log("command_executed", {
                "command": command_key,
                "success": True,
                "response_length": len(response) if response else 0,
            })
            if response is None:
                return None
            prefix = self.config.get('watcher', {}).get('response_prefix', '')
            return f"{prefix}{response}"
        except Exception as e:
            audit_log("command_error", {
                "command": command_key,
                "error": str(e),
            })
            return f"Error: {str(e)}"

    def _check_provider_recovery(self):
        try:
            from handlers.general_handlers import (
                _load_provider_state, _effective_provider_cooldown_until, _provider_enabled
            )
            state = _load_provider_state()
            now = int(time.time())
            anthropic_ready = _effective_provider_cooldown_until(state, "anthropic") <= now
            codex_ready = _effective_provider_cooldown_until(state, "codex") <= now and _provider_enabled("codex")
            local_ready = _provider_enabled("local")
            was_blocked = getattr(self, '_all_providers_blocked', False)
            any_ready = anthropic_ready or codex_ready or local_ready
            if was_blocked and any_ready:
                if anthropic_ready:
                    provider = "Anthropic"
                elif codex_ready:
                    provider = "Codex"
                else:
                    provider = "Local model"
                self.send_response(f"\u2705 {provider} is back online. I'm ready.", chat_id=_personal_id)
                self._all_providers_blocked = False
            elif not anthropic_ready and not codex_ready and not local_ready:
                self._all_providers_blocked = True
        except Exception:
            pass

    # ── Push Message Processing ─────────────────────────────────────────────

    def _process_push_message(self, text, sender, chat_guid, chat_id, is_group, attachments, raw_data):
        """Process a single push message. Called under chat lock — not thread-safe on its own."""
        # Notify personality layer that user sent a message
        try:
            from proactive.personality.response_tracker import ResponseTracker
            tracker = ResponseTracker()
            tracker.record_user_message()
            # Also update last_user_message_ts in proactive state for absence detection
            import json
            _state_path = Path.home() / ".openclaw" / "state" / "proactive_state.json"
            if _state_path.exists():
                with open(_state_path) as _sf:
                    _pstate = json.load(_sf)
                _pstate["last_user_message_ts"] = time.time()
                with open(_state_path, "w") as _sf:
                    json.dump(_pstate, _sf, indent=2)
        except Exception:
            pass  # Personality layer not available or state file missing

        sender_name = KNOWN_SENDERS.get(sender, sender)
        parsed = self.parse_command(text)
        metadata = {
            "chat_id": chat_id,
            "attachments": attachments,
            "is_group": is_group,
            "sender_name": sender_name,
            "chat_guid": chat_guid,
            "transport": "push",
        }

        if parsed:
            command_key, args = parsed
        else:
            command_key = "general:claude"
            args = text

        timestamp = datetime.now().isoformat()
        self._log(f"[{timestamp}] PUSH Chat {chat_id} | {sender_name}: {command_key} {args[:50] if args else ''}")
        audit_log("command_received", {
            "sender": sender, "sender_name": sender_name,
            "chat_id": chat_id, "command": command_key,
            "text_preview": text[:50], "transport": "push",
        })

        response = self.execute(command_key, args, message=text, sender=sender, metadata=metadata)
        if response is not None:
            self.send_response(response, chat_id=chat_id)
            self._log(f"[{timestamp}] PUSH responded.\n")
        else:
            self._log(f"[{timestamp}] PUSH handler sent response directly.\n")

    # ── Push Transport Callback ────────────────────────────────────────────

    def _on_push_message(self, text, sender, chat_guid, chat_id, is_group, attachments, raw_data):
        """Called by BlueBubblesPush on Socket.IO thread. Must be thread-safe."""
        try:
            self._push_last_message_time = time.time()
            msg_guid = raw_data.get("guid", "")
            cmd_id = f"bb:{msg_guid}" if msg_guid else f"bb:{hash(text + sender + str(time.time()))}"

            if not self._mark_processed(cmd_id):
                return

            # Cross-transport dedup: skip if polling already handled this
            if self._content_dedup_check(sender, text):
                self._log(f"PUSH: skipping msg already handled by polling ({text[:30]})")
                return

            # Per-chat lock: prevents concurrent LLM calls if polling races us
            chat_lock = self._get_chat_lock(chat_id)
            if not chat_lock.acquire(blocking=False):
                # Queue instead of dropping — will be drained when current message finishes
                self._enqueue_chat_message(chat_id, (text, sender, chat_guid, chat_id, is_group, attachments, raw_data))
                self._log(f"PUSH: chat {chat_id} busy, queued message ({text[:30]}...)")
                return

            try:
                self._process_push_message(text, sender, chat_guid, chat_id, is_group, attachments, raw_data)
                # Drain any messages that queued while we were processing
                while True:
                    queued = self._dequeue_chat_message(chat_id)
                    if queued is None:
                        break
                    q_text, q_sender, q_guid, q_cid, q_group, q_attach, q_raw = queued
                    self._log(f"PUSH: draining queued message for chat {chat_id} ({q_text[:30]}...)")
                    self._process_push_message(q_text, q_sender, q_guid, q_cid, q_group, q_attach, q_raw)
            finally:
                chat_lock.release()

        except Exception as e:
            self._log(f"Push callback error: {e}")
            audit_log("push_callback_error", {"error": str(e)})

    # ── Send (BB REST API -> osascript -> imsg CLI) ───────────────────────

    def send_response(self, response: str, chat_id: int = 1) -> bool:
        if not self.circuit_breaker.record_send():
            self._log("Send blocked by circuit breaker cooldown")
            return False

        if len(response) > 1500:
            response = response[:1497] + "..."

        # Try BB REST API first
        if self._bb and self._bb.available:
            success = self._bb.send(response, chat_id=chat_id)
            if success:
                audit_log("message_sent", {
                    "chat_id": chat_id, "message_length": len(response),
                    "success": True, "transport": "bluebubbles",
                })
                return True
            self._log(f"BB send failed for chat {chat_id}, falling back to osascript")

        # osascript fallback
        success = _osascript_send(response, chat_id)

        if not success:
            self._log(f"osascript send failed for chat {chat_id}, trying imsg fallback")
            try:
                result = subprocess.run(
                    [IMSG_BIN, "send",
                     "--chat-id", str(chat_id),
                     "--service", "imessage", "--text", response],
                    timeout=30, check=False, capture_output=True
                )
                success = result.returncode == 0
                if not success:
                    self._log(f"imsg fallback also failed: {result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr}")
            except Exception as e:
                self._log(f"Failed to send: {e}")
                return False

        audit_log("message_sent", {
            "chat_id": chat_id, "message_length": len(response),
            "success": success, "transport": "osascript" if success else "failed",
        })
        return success

    def poll_messages(self) -> list:
        all_messages = []
        for chat_id in CHAT_IDS:
            try:
                result = subprocess.run(
                    [IMSG_BIN, "history", "--chat-id", str(chat_id),
                     "--limit", str(HISTORY_LIMIT), "--attachments", "--json"],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode != 0:
                    continue
                for line in result.stdout.strip().split('\n'):
                    if line.strip():
                        try:
                            msg = json.loads(line)
                            msg['_chat_id'] = chat_id
                            all_messages.append(msg)
                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                self._log(f"Poll error (chat {chat_id}): {e}")
        return all_messages

    def _check_restart_and_notify(self):
        startup_file = OPENCLAW_DIR / "state" / ".imsg_watcher_last_start"
        now = time.time()
        was_recent_restart = False
        if startup_file.exists():
            try:
                last_start = float(startup_file.read_text().strip())
                gap = now - last_start
                if 0 < gap < 120:
                    was_recent_restart = True
                    self._log(f"Crash detected -- last start was {gap:.0f}s ago")
            except Exception:
                pass
        try:
            startup_file.write_text(str(now))
        except Exception:
            pass
        if was_recent_restart:
            _osascript_send("Heads up -- I crashed and just restarted automatically. Everything should be back to normal.", _personal_id)

    def _startup_check(self):
        issues = []
        try:
            result = subprocess.run(
                [IMSG_BIN, "history", "--chat-id", str(_personal_id),
                 "--limit", "1", "--json"],
                timeout=10, capture_output=True, text=True
            )
            if result.returncode != 0 or not result.stdout.strip():
                issues.append("imsg history failed -- Full Disk Access may be missing for python3")
            else:
                self._log("Startup check: imsg history OK")
        except subprocess.TimeoutExpired:
            issues.append("imsg history timed out")
        except Exception as e:
            issues.append(f"imsg history error: {e}")

        try:
            result = subprocess.run(
                [OSASCRIPT_BIN, "-e", 'tell application "Messages" to get name'],
                timeout=10, capture_output=True, text=True
            )
            if result.returncode != 0:
                issues.append("osascript Messages.app failed -- check Automation in Privacy settings")
            else:
                self._log("Startup check: osascript OK")
        except subprocess.TimeoutExpired:
            issues.append("osascript timed out")
        except Exception as e:
            issues.append(f"osascript error: {e}")

        if self._bb and self._bb.available:
            try:
                info = self._bb.get_server_info()
                if info:
                    self._log(f"Startup check: BlueBubbles OK (v{info.get('server_version', '?')})")
                else:
                    issues.append("BlueBubbles server unreachable -- push will retry in background")
            except Exception as e:
                issues.append(f"BlueBubbles check error: {e}")

        if issues:
            summary = "; ".join(issues)
            self._log(f"STARTUP CHECK WARNINGS: {summary}")
            _osascript_send(f"Rout startup warning: {summary}. Bot may not respond until this is fixed.", _personal_id)
        else:
            self._log("All startup checks passed")

    def watch(self):
        """Main loop -- push transport with polling fallback."""
        self._check_restart_and_notify()
        self._startup_check()

        # Start push transport if available
        if self._bb and self._bb.available:
            started = self._bb.start()
            if started:
                self._push_active = True
                self._log("BlueBubbles push transport STARTED -- polling is standby")
                audit_log("push_transport_started", {"server": self._bb.server_url})
            else:
                self._log("BlueBubbles push failed to start -- using polling")
        else:
            if _USER_CFG.get("bluebubbles", {}).get("enabled", False):
                if not HAS_BB_PUSH:
                    self._log("BB push enabled but python-socketio not installed. pip install python-socketio[client] requests")
                else:
                    self._log("BB push enabled but not fully configured. Check password + chat_map.")
            self._log("Using polling transport")

        transport_mode = "push+fallback" if self._push_active else "polling"

        audit_log("watcher_started", {
            "chat_ids": CHAT_IDS,
            "handlers": list(self.handlers.keys()),
            "commands": list(self.config.get('commands', {}).keys()),
            "transport": transport_mode,
        })

        self._log(f"Starting watcher (chat_ids={CHAT_IDS}, transport={transport_mode})")
        self._log(f"Commands: {len(self.handlers)} handlers loaded")
        self._log(f"Poll interval: {POLL_INTERVAL}s")
        try:
            from handlers.general_handlers import _get_api_key
            key = _get_api_key()
            if key:
                self._log(f"API key: {key[:8]}...{key[-4:]} ({len(key)} chars)")
            else:
                self._log("WARNING: No API key found -- Claude responses will fail")
        except Exception as e:
            self._log(f"Could not check API key: {e}")

        self._log("Waiting for commands...\n")

        last_heartbeat = time.time()
        last_poll_had_results = time.time()
        HEARTBEAT_INTERVAL = 300
        STUCK_THRESHOLD = 900
        _was_push_connected = False
        self._push_last_message_time = 0
        _push_proven = False  # Push must deliver a message before we reduce polling
        REDUCED_POLL_INTERVAL = 30  # polling frequency when push is proven (safety net)
        _reduced_poll_counter = 0  # counts sleep cycles to know when to poll

        try:
            while True:
                now = time.time()

                # Heartbeat (always runs)
                if now - last_heartbeat > HEARTBEAT_INTERVAL:
                    if self._push_active and self._bb:
                        status = self._bb.status()
                        last_evt = status.get('last_event')
                        evt_age = f", last_event={int(now - self._bb._last_event_time)}s ago" if self._bb._last_event_time > 0 else ""
                        self._log(
                            f"Heartbeat -- push {'connected' if status['connected'] else 'DISCONNECTED'}"
                            f"{evt_age}, polling={'safety-net' if _push_proven else 'active'}"
                        )
                    else:
                        self._log("Heartbeat -- polling active")
                    last_heartbeat = now

                self._check_provider_recovery()

                if not self.circuit_breaker.can_send():
                    time.sleep(POLL_INTERVAL * 2)
                    continue

                # Push mode: REDUCE polling frequency after push delivers first message.
                # NEVER fully suspend polling — it's the safety net for zombie sockets.
                # Content dedup + per-chat locks prevent duplicate responses.
                if self._push_active and self._bb and self._bb.connected:
                    if not _push_proven and self._push_last_message_time > 0:
                        _push_proven = True
                        self._log("Push transport PROVEN -- first message delivered, reducing polling to safety-net mode")

                    if _push_proven:
                        if not _was_push_connected:
                            self._log("Push transport connected -- polling reduced (every ~30s safety net)")
                            _was_push_connected = True
                        # Still poll, but at reduced frequency
                        _reduced_poll_counter += 1
                        polls_per_cycle = max(1, REDUCED_POLL_INTERVAL // POLL_INTERVAL)
                        if _reduced_poll_counter < polls_per_cycle:
                            time.sleep(POLL_INTERVAL)
                            continue
                        _reduced_poll_counter = 0
                        # Fall through to polling below — dedup handles overlap
                    else:
                        if not _was_push_connected:
                            self._log("Push transport connected but UNPROVEN -- polling continues (dedup protects against duplicates)")
                            _was_push_connected = True

                if self._push_active and _was_push_connected and not (self._bb and self._bb.connected):
                    self._log("Push transport DISCONNECTED -- activating full-speed polling fallback")
                    _was_push_connected = False
                    _push_proven = False
                    _reduced_poll_counter = 0
                    audit_log("push_fallback_activated", {})

                # Polling mode (v3 logic, thread-safe dedup)
                messages = self.poll_messages()

                inbound = [m for m in messages if not m.get('is_from_me')]
                if inbound:
                    self._log(f"Poll: {len(inbound)} user msg(s) found: {[m.get('id') for m in inbound]}")

                if messages:
                    last_poll_had_results = now
                elif now - last_poll_had_results > STUCK_THRESHOLD:
                    self._log(f"No messages returned for {STUCK_THRESHOLD//60}min -- possible issue")
                    last_poll_had_results = now

                for msg in reversed(messages):
                    if msg.get('is_from_me', False):
                        continue

                    cmd_id = self._get_command_id(msg)
                    if not self._mark_processed(cmd_id):
                        continue

                    text = msg.get('text', '').strip()
                    if not text:
                        continue

                    sender = msg.get('sender', '')

                    # Cross-transport dedup: skip if push already handled this
                    if self._content_dedup_check(sender, text):
                        self._log(f"Poll: skipping msg already handled by push ({text[:30]})")
                        continue

                    # Per-chat lock: queue if push is already processing this chat
                    chat_id = msg.get('_chat_id', 1)
                    chat_lock = self._get_chat_lock(chat_id)
                    if not chat_lock.acquire(blocking=False):
                        # Queue for drain when push finishes, instead of dropping
                        self._enqueue_chat_message(chat_id, (text, sender, None, chat_id, chat_id != _personal_id, msg.get('attachments') or [], msg))
                        self._log(f"Poll: chat {chat_id} locked by push, queued ({text[:30]}...)")
                        continue

                    try:
                        sender_name = KNOWN_SENDERS.get(sender, sender)
                        is_group = chat_id != _personal_id

                        parsed = self.parse_command(text)
                        attachments = msg.get('attachments') or []
                        attachment_paths = [
                            a['original_path'] for a in attachments
                            if a.get('original_path') and not a.get('missing')
                        ]
                        metadata = {
                            "chat_id": chat_id,
                            "attachments": attachment_paths,
                            "is_group": is_group,
                            "sender_name": sender_name,
                            "transport": "polling",
                        }

                        if parsed:
                            command_key, args = parsed
                        else:
                            command_key = "general:claude"
                            args = text

                        timestamp = datetime.now().isoformat()
                        self._log(f"[{timestamp}] Chat {chat_id} | {sender_name}: {command_key} {args[:50] if args else ''}")
                        audit_log("command_received", {
                            "sender": sender, "sender_name": sender_name,
                            "chat_id": chat_id, "command": command_key,
                            "text_preview": text[:50], "transport": "polling",
                        })

                        response = self.execute(command_key, args, message=text, sender=sender, metadata=metadata)
                        if response is not None:
                            self.send_response(response, chat_id=chat_id)
                            self._log(f"[{timestamp}] Responded.\n")
                        else:
                            self._log(f"[{timestamp}] Handler sent response directly.\n")

                        # Drain any messages queued by push while we held the lock
                        while True:
                            queued = self._dequeue_chat_message(chat_id)
                            if queued is None:
                                break
                            q_text, q_sender, q_guid, q_cid, q_group, q_attach, q_raw = queued
                            self._log(f"Poll: draining queued message for chat {chat_id} ({q_text[:30]}...)")
                            self._process_push_message(q_text, q_sender, q_guid, q_cid, q_group, q_attach, q_raw)
                    finally:
                        chat_lock.release()

                time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            self._log("Watcher stopped (keyboard interrupt)")
            audit_log("watcher_stopped", {"reason": "keyboard_interrupt"})
            if self._bb:
                self._bb.stop()

        except Exception as e:
            self._log(f"Fatal error: {e}")
            audit_log("watcher_stopped", {"reason": "fatal_error", "error": str(e)})
            if self._bb:
                self._bb.stop()
            sys.exit(1)

    def _rotate_log(self):
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
                archive = LOG_FILE.with_suffix('.log.1')
                if archive.exists():
                    archive.unlink()
                LOG_FILE.rename(archive)
        except Exception:
            pass

    def _log(self, msg: str):
        timestamp = datetime.now().isoformat()
        log_line = f"[{timestamp}] {msg}"
        print(log_line)
        try:
            self._rotate_log()
            with open(LOG_FILE, 'a') as f:
                f.write(log_line + '\n')
        except Exception:
            pass


# ============================================================
# SIGNAL HANDLERS
# ============================================================

def handle_sigterm(signum, frame):
    audit_log("watcher_stopped", {"reason": "SIGTERM"})
    sys.exit(0)


# ============================================================
# MAIN
# ============================================================

if __name__ == '__main__':
    signal.signal(signal.SIGTERM, handle_sigterm)
    watcher = CommandWatcher()
    try:
        watcher.watch()
    finally:
        if watcher._bb:
            watcher._bb.stop()
