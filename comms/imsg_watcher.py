#!/usr/bin/env python3
"""
Rout iMessage Watcher v3 — rebuilt from working v2 + hardening
================================================================
Based on the PROVEN v2 architecture that was running reliably.
Hardening additions (non-breaking):
  - Structured audit logging (JSONL)
  - Graceful circuit breaker with persistent cooldown (no os.abort)
  - SIGTERM handler for clean shutdown
  - Parameterized AppleScript (sanitized inputs)

Architecture (unchanged from v2):
  - osascript primary send, imsg CLI fallback
  - imsg history --chat-id --limit --json polling
  - Message ID dedup (not timestamp-based)
  - Dynamic handler loading from handlers/*.py
  - Command registry from imsg_commands.yaml
"""

import subprocess
import json
import re
import yaml
import os
import signal
import sys
import importlib.util
import time
import shutil
import inspect
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable, Dict, Set, Tuple

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
LEGACY_WORKSPACE = OPENCLAW_DIR / "hardened"

# Ensure directories exist
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
    """Pick a runtime workspace that contains handlers + command registry."""
    override = os.environ.get("ROUT_WORKSPACE")
    candidates = []

    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(DEFAULT_WORKSPACE)
    candidates.append(LEGACY_WORKSPACE)

    for candidate in candidates:
        if (candidate / "imsg_commands.yaml").exists() and (candidate / "handlers").is_dir():
            return candidate

    return DEFAULT_WORKSPACE


def _resolve_imsg_bin(cfg: dict) -> str:
    """Resolve imsg binary path from config or PATH."""
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

# Add workspace to sys.path so handler modules can import from agent/
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
    """Append a structured event to the JSONL audit log."""
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
# APPLESCRIPT SEND (primary — proven reliable from launchd)
# ============================================================

_UNSAFE_CHARS = re.compile(r'[\x00-\x1f]')

def _osascript_send(text: str, chat_id: int) -> bool:
    """Send iMessage via osascript. Sanitizes inputs to prevent injection."""
    # Sanitize: strip control chars, escape backslashes and quotes
    text = _UNSAFE_CHARS.sub("", text)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')

    handle_info = CHAT_HANDLES.get(chat_id)
    if not handle_info:
        return False

    handle, handle_type = handle_info
    # Sanitize handle too
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
# CIRCUIT BREAKER (graceful cooldown, persistent state)
# ============================================================

CIRCUIT_BREAKER_FILE = OPENCLAW_DIR / "state" / "circuit_breaker.json"

class CircuitBreaker:
    """
    Rate limiter with persistent cooldown.
    v2 used os.abort (hard kill). v3 uses exponential cooldown instead.
    """

    def __init__(self, max_sends: int = 8, window_seconds: int = 60):
        self.max_sends = max_sends
        self.window = window_seconds
        self._send_timestamps: list = []
        self.cooldown_until: float = 0
        self.trip_count: int = 0
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
        now = time.time()
        if now < self.cooldown_until:
            return False
        # Reset trip count after cooldown expires
        if self.trip_count > 0 and now >= self.cooldown_until:
            self.trip_count = 0
            self._save_state()
        return True

    def record_send(self) -> bool:
        """Record a send. Returns False if circuit breaker tripped."""
        now = time.time()
        if not self.can_send():
            return False

        self._send_timestamps = [t for t in self._send_timestamps if now - t < self.window]
        self._send_timestamps.append(now)

        if len(self._send_timestamps) > self.max_sends:
            self._trip()
            return False
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
# COMMAND WATCHER (v2 architecture, v3 hardening)
# ============================================================

class CommandWatcher:
    def __init__(self):
        self.workspace = WORKSPACE
        self.config = self._load_config(COMMANDS_CONFIG)
        self.handlers = self._load_handlers()
        self.processed_commands: Set[str] = self._load_processed_commands()
        self.circuit_breaker = CircuitBreaker(
            max_sends=_USER_CFG.get("circuit_breaker_max", 8),
            window_seconds=_USER_CFG.get("circuit_breaker_window", 60),
        )

    def _load_config(self, path: Path) -> Dict:
        """Load command registry from YAML."""
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
        """Dynamically load handler modules from handlers/*.py."""
        handlers = {}
        handlers_dir = self.workspace / "handlers"

        if not handlers_dir.exists():
            self._log(f"Handlers directory not found: {handlers_dir}")
            return handlers

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
            except Exception as e:
                self._log(f"Failed to load {module_name}: {e}")

        return handlers

    def _load_processed_commands(self) -> Set[str]:
        """Load set of already-processed command identifiers."""
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE, 'r') as f:
                    data = json.load(f)
                    return set(data.get('processed', []))
        except Exception:
            pass
        return set()

    def _save_processed_commands(self):
        """Save processed command set to state file."""
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump({
                    'processed': list(self.processed_commands),
                    'saved_at': datetime.now().isoformat()
                }, f)
        except Exception as e:
            self._log(f"Failed to save state: {e}")

    def _get_command_id(self, msg: Dict) -> str:
        """Create unique ID for a message (dedup key)."""
        msg_id = msg.get('id', 0)
        timestamp = msg.get('created_at', '')
        return f"{msg_id}:{timestamp}"

    def parse_command(self, text: str) -> Optional[Tuple[str, str]]:
        """Parse text for commands. Returns (command_key, args) or None.

        Understands several formats:
          kalshi: portfolio          -> kalshi:portfolio
          Portfolio: Kalshi          -> kalshi:portfolio  (swapped order)
          help                       -> help  (bare registry key)
          Help: list all commands    -> help  (bare key, ignores extra words)
          ping                       -> ping
        """
        text = text.strip()
        registry = self.config.get('commands', {})

        # 1. Bare single-word commands (no colon)
        if ':' not in text:
            word = text.split()[0].lower()
            if word in registry:
                args = text[len(word):].strip()
                return (word, args)
            return None

        # 2. Colon-format: "prefix: rest"
        match = re.match(r'(\w+)\s*:\s*(.*)', text, re.DOTALL)
        if not match:
            return None

        prefix = match.group(1).lower()
        rest = match.group(2).strip()
        first_word = rest.split()[0].lower() if rest.split() else ""
        extra_args = ' '.join(rest.split()[1:]) if len(rest.split()) > 1 else ""

        # 2a. Canonical format: "kalshi: portfolio [args]"
        canonical_key = f"{prefix}:{first_word}" if first_word else prefix
        if canonical_key in registry:
            return (canonical_key, extra_args)

        # 2b. Bare prefix is a registry key: "help: [anything]"
        if prefix in registry:
            return (prefix, rest)

        # 2c. Swapped order: "Portfolio: Kalshi" -> try "kalshi:portfolio"
        if first_word:
            swapped_key = f"{first_word}:{prefix}"
            if swapped_key in registry:
                return (swapped_key, extra_args)

        # 2d. Prefix as namespace fallback
        for key in registry:
            if key.startswith(f"{prefix}:") and first_word and key.endswith(f":{first_word}"):
                return (key, extra_args)

        return None

    def _invoke_handler(self, handler: Callable, args: str, message: str, sender: str, metadata: dict):
        """Invoke handlers with backward-compatible signature support."""
        try:
            signature = inspect.signature(handler)
        except (TypeError, ValueError):
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
        """Execute command via handler."""
        registry = self.config.get('commands', {})

        if command_key not in registry:
            return f"Unknown command: '{command_key}'. Type 'help' for available commands."

        cmd_config = registry[command_key]
        handler_path = cmd_config.get('handler')

        if not handler_path or handler_path not in self.handlers:
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
                return None  # Handler is managing delivery itself
            prefix = self.config.get('watcher', {}).get('response_prefix', '')
            return f"{prefix}{response}"
        except Exception as e:
            audit_log("command_error", {
                "command": command_key,
                "error": str(e),
            })
            return f"Error: {str(e)}"

    def _check_provider_recovery(self):
        """Notify user when providers recover from cooldown."""
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
                self.send_response(f"✅ {provider} is back online. I'm ready.", chat_id=_personal_id)
                self._all_providers_blocked = False
            elif not anthropic_ready and not codex_ready and not local_ready:
                self._all_providers_blocked = True
        except Exception:
            pass

    def send_response(self, response: str, chat_id: int = 1) -> bool:
        """Send response via iMessage. osascript primary, imsg CLI fallback."""
        if not self.circuit_breaker.record_send():
            self._log("Send blocked by circuit breaker cooldown")
            return False

        # Truncate long messages for iMessage
        if len(response) > 1500:
            response = response[:1497] + "..."

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
            "chat_id": chat_id,
            "message_length": len(response),
            "success": success,
        })

        return success

    def poll_messages(self) -> list:
        """Poll recent messages from all watched chats via imsg CLI."""
        all_messages = []
        for chat_id in CHAT_IDS:
            try:
                result = subprocess.run(
                    [IMSG_BIN, "history", "--chat-id", str(chat_id),
                     "--limit", str(HISTORY_LIMIT), "--attachments", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=10
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
        """Detect unexpected restarts (crash recovery by launchd)."""
        startup_file = OPENCLAW_DIR / "state" / ".imsg_watcher_last_start"
        now = time.time()

        was_recent_restart = False
        if startup_file.exists():
            try:
                last_start = float(startup_file.read_text().strip())
                gap = now - last_start
                if 0 < gap < 120:
                    was_recent_restart = True
                    self._log(f"Crash detected — last start was {gap:.0f}s ago")
            except Exception:
                pass

        try:
            startup_file.write_text(str(now))
        except Exception:
            pass

        if was_recent_restart:
            alert = "Heads up — I crashed and just restarted automatically. Everything should be back to normal."
            _osascript_send(alert, _personal_id)

    def _startup_check(self):
        """Verify imsg history and osascript work before entering main loop."""
        issues = []

        # Test 1: imsg history (Full Disk Access)
        try:
            result = subprocess.run(
                [IMSG_BIN, "history", "--chat-id", str(_personal_id),
                 "--limit", "1", "--json"],
                timeout=10, capture_output=True, text=True
            )
            if result.returncode != 0 or not result.stdout.strip():
                issues.append("imsg history failed — Full Disk Access may be missing for python3")
            else:
                self._log("Startup check: imsg history OK")
        except subprocess.TimeoutExpired:
            issues.append("imsg history timed out")
        except Exception as e:
            issues.append(f"imsg history error: {e}")

        # Test 2: osascript Messages.app (Automation permission)
        try:
            result = subprocess.run(
                [OSASCRIPT_BIN, "-e", 'tell application "Messages" to get name'],
                timeout=10, capture_output=True, text=True
            )
            if result.returncode != 0:
                issues.append("osascript Messages.app failed — check Automation in Privacy settings")
            else:
                self._log("Startup check: osascript OK")
        except subprocess.TimeoutExpired:
            issues.append("osascript timed out")
        except Exception as e:
            issues.append(f"osascript error: {e}")

        if issues:
            summary = "; ".join(issues)
            self._log(f"STARTUP CHECK FAILED: {summary}")
            _osascript_send(
                f"Rout startup warning: {summary}. Bot may not respond until this is fixed.",
                _personal_id
            )
        else:
            self._log("All startup checks passed")

    def watch(self):
        """Main polling loop."""
        self._check_restart_and_notify()
        self._startup_check()

        audit_log("watcher_started", {
            "chat_ids": CHAT_IDS,
            "handlers": list(self.handlers.keys()),
            "commands": list(self.config.get('commands', {}).keys()),
        })

        self._log(f"Starting watcher (chat_ids={CHAT_IDS})")
        self._log(f"Commands: {len(self.handlers)} handlers loaded")
        self._log(f"Poll interval: {POLL_INTERVAL}s")
        # Log API key status at startup
        try:
            from handlers.general_handlers import _get_api_key
            key = _get_api_key()
            if key:
                self._log(f"API key: {key[:8]}...{key[-4:]} ({len(key)} chars)")
            else:
                self._log("WARNING: No API key found — Claude responses will fail")
        except Exception as e:
            self._log(f"Could not check API key: {e}")

        self._log("Waiting for commands...\n")

        last_heartbeat = time.time()
        last_poll_had_results = time.time()
        HEARTBEAT_INTERVAL = 300
        STUCK_THRESHOLD = 900

        try:
            while True:
                now = time.time()

                # Heartbeat
                if now - last_heartbeat > HEARTBEAT_INTERVAL:
                    self._log("Heartbeat — polling active")
                    last_heartbeat = now

                # Check if providers recovered from cooldown
                self._check_provider_recovery()

                # Skip polling during circuit breaker cooldown
                if not self.circuit_breaker.can_send():
                    time.sleep(POLL_INTERVAL * 2)
                    continue

                messages = self.poll_messages()

                inbound = [m for m in messages if not m.get('is_from_me')]
                if inbound:
                    self._log(f"Poll: {len(inbound)} user msg(s) found: {[m.get('id') for m in inbound]}")

                if messages:
                    last_poll_had_results = now
                elif now - last_poll_had_results > STUCK_THRESHOLD:
                    self._log(f"No messages returned for {STUCK_THRESHOLD//60}min — possible issue")
                    last_poll_had_results = now

                processed_in_this_poll = []

                for msg in reversed(messages):
                    # Skip outgoing messages
                    if msg.get('is_from_me', False):
                        continue

                    # Dedup by message ID
                    cmd_id = self._get_command_id(msg)
                    if cmd_id in self.processed_commands or cmd_id in processed_in_this_poll:
                        continue

                    text = msg.get('text', '').strip()
                    if not text:
                        continue

                    sender = msg.get('sender', '')
                    sender_name = KNOWN_SENDERS.get(sender, sender)
                    chat_id = msg.get('_chat_id', 1)
                    is_group = chat_id != _personal_id

                    # Try to parse as command
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
                    }

                    if parsed:
                        command_key, args = parsed
                    else:
                        # Fallback: free-form message -> Claude
                        command_key = "general:claude"
                        args = text

                    timestamp = datetime.now().isoformat()
                    self._log(f"[{timestamp}] Chat {chat_id} | {sender_name}: {command_key} {args[:50] if args else ''}")

                    audit_log("command_received", {
                        "sender": sender,
                        "sender_name": sender_name,
                        "chat_id": chat_id,
                        "command": command_key,
                        "text_preview": text[:50],
                    })

                    processed_in_this_poll.append(cmd_id)

                    # CRITICAL: Mark as processed BEFORE executing handler to prevent
                    # duplicate processing if subsequent polls happen during handler execution.
                    # Handlers can take 3-5 seconds (Claude API calls), and poll interval is 2s.
                    # Without this, the same message can be processed multiple times by
                    # different poll cycles running concurrently.
                    self.processed_commands.add(cmd_id)
                    self._save_processed_commands()

                    response = self.execute(
                        command_key,
                        args,
                        message=text,
                        sender=sender,
                        metadata=metadata,
                    )
                    if response is not None:
                        self.send_response(response, chat_id=chat_id)
                        self._log(f"[{timestamp}] Responded.\n")
                    else:
                        self._log(f"[{timestamp}] Handler sent response directly.\n")

                time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            self._log("Watcher stopped (keyboard interrupt)")
            audit_log("watcher_stopped", {"reason": "keyboard_interrupt"})

        except Exception as e:
            self._log(f"Fatal error: {e}")
            audit_log("watcher_stopped", {"reason": "fatal_error", "error": str(e)})
            sys.exit(1)

    def _rotate_log(self):
        """Keep log under 5MB."""
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
                archive = LOG_FILE.with_suffix('.log.1')
                if archive.exists():
                    archive.unlink()
                LOG_FILE.rename(archive)
        except Exception:
            pass

    def _log(self, msg: str):
        """Log with rotation."""
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
    watcher.watch()
