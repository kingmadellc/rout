#!/usr/bin/env python3
"""
Rout Webhook Server v2 — Production-grade HTTP endpoint for event-driven triggers.
===================================================================================
Lightweight HTTP server that accepts POST requests from external services
and converts them into iMessage notifications via Rout's send pipeline.

Any service with an HTTP client can trigger Rout:
  - Kalshi price alerts (via edge scanner or monitor)
  - Home automation (HomeAssistant, IFTTT, etc.)
  - GitHub/CI notifications
  - Custom scripts on the same Mac
  - Zapier/Make/n8n workflows

Architecture:
  - Runs as a standalone process alongside imsg_watcher (separate launchd plist)
  - Listens on localhost only (no external exposure by default)
  - Authenticates via shared secret (X-Rout-Secret header or ?secret= param)
  - Rate-limited per source to prevent spam
  - Sends messages via BB REST API -> osascript -> imsg CLI (same chain as watcher)
  - Supports both raw text and template-based messages via trigger configs
  - Hot-reloads config on SIGHUP or POST /admin/reload
  - Retries failed sends with exponential backoff
  - In-memory history ring buffer + queryable /admin/history endpoint

Endpoints:
  POST /webhook              Generic webhook -- sends payload through template or raw
  POST /webhook/<trigger_id> Named trigger -- uses config from proactive_triggers.yaml
  GET  /health               Health check (no auth required)
  GET  /admin/triggers       List all registered triggers with templates
  GET  /admin/history        Recent webhook events (last 100, filterable)
  GET  /admin/stats          Server uptime, total fires, success rate
  POST /admin/reload         Hot-reload config from disk
  POST /admin/triggers       Register a new trigger at runtime (persists to YAML)

Config (in config/proactive_triggers.yaml):
  webhooks:
    enabled: true
    port: 7888
    secret: "your-shared-secret"
    triggers:
      kalshi-alert:
        template: "target Kalshi: {ticker} hit {price}c ({direction})"
        chat_id: 1

Usage:
    python3 comms/webhook_server.py                    # Start server
    python3 comms/webhook_server.py --port 7888        # Custom port
    rout-webhook fire kalshi-alert ticker=KXBTC price=52 direction=up
    rout-webhook triggers
    rout-webhook history --last 10
"""

import json
import os
import signal
import shutil
import subprocess
import sys
import re
import time
import yaml
from collections import deque
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Lock, Thread
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs

# ── Setup ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OPENCLAW_DIR = Path(
    os.environ.get("ROUT_OPENCLAW_DIR", str(Path.home() / ".openclaw"))
).expanduser()

LOG_DIR = OPENCLAW_DIR / "logs"
LOG_FILE = LOG_DIR / "webhook_server.log"
AUDIT_LOG = LOG_DIR / "webhook_audit.jsonl"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TRIGGERS_YAML = PROJECT_ROOT / "config" / "proactive_triggers.yaml"

# ── Server State (mutable, reload-safe) ─────────────────────────────────────

_state_lock = Lock()
_server_start_time = time.time()
_total_fires = 0
_total_success = 0
_total_failures = 0


class ServerConfig:
    """Mutable server configuration — reloaded on SIGHUP / /admin/reload.
    Thread-safe: all reads/writes go through _lock."""

    def __init__(self):
        self._lock = Lock()
        self.config: dict = {}
        self.webhook_cfg: dict = {}
        self.secret: str = ""
        self.triggers: dict = {}
        self.personal_chat_id: int = 1
        self.chat_handles: dict = {}
        self.imsg: str = "/opt/homebrew/bin/imsg"
        self.osascript: str = shutil.which("osascript") or "/usr/bin/osascript"
        self.bb_enabled: bool = False
        self.bb_url: str = ""
        self.bb_password: str = ""
        self.bb_send_method: str = "private-api"
        self.bb_chat_map: dict = {}
        self.rate_limit: int = 5
        self.port: int = 7888
        self.reload()

    def reload(self):
        """Load/reload all configuration from disk. Thread-safe."""
        with self._lock:
            self._do_reload()

    def _do_reload(self):
        """Internal reload (caller must hold _lock)."""
        self.config = self._load_config()
        self.webhook_cfg = self._load_webhook_config()

        paths = self.config.get("paths", {})
        self.imsg = paths.get("imsg", "/opt/homebrew/bin/imsg")
        self.personal_chat_id = self.config.get("chats", {}).get("personal_id", 1)
        self.chat_handles = {
            int(k): tuple(v)
            for k, v in self.config.get("chat_handles", {}).items()
        }

        self.port = self.webhook_cfg.get("port", 7888)
        self.secret = self.webhook_cfg.get("secret", "")
        self.triggers = self.webhook_cfg.get("triggers", {})
        self.rate_limit = self.webhook_cfg.get("rate_limit", 5)

        bb = self.config.get("bluebubbles", {})
        self.bb_enabled = bb.get("enabled", False)
        self.bb_url = bb.get("server_url", "http://localhost:1234").rstrip("/")
        self.bb_password = bb.get("password", "")
        self.bb_send_method = bb.get("send_method", "private-api")
        self.bb_chat_map = {int(k): v for k, v in bb.get("chat_map", {}).items()}

    def _load_config(self) -> dict:
        for candidate in [
            PROJECT_ROOT / "config.yaml",
            Path.home() / ".config/imsg-watcher/config.yaml",
        ]:
            if candidate.exists():
                with open(candidate) as f:
                    return yaml.safe_load(f) or {}
        return {}

    def _load_webhook_config(self) -> dict:
        if TRIGGERS_YAML.exists():
            with open(TRIGGERS_YAML) as f:
                data = yaml.safe_load(f) or {}
                return data.get("webhooks", {})
        return {}


CFG = ServerConfig()


# ── History Ring Buffer ─────────────────────────────────────────────────────

MAX_HISTORY = 200

_history: deque = deque(maxlen=MAX_HISTORY)
_history_lock = Lock()


def _record_event(event: dict):
    """Add an event to the in-memory history ring buffer."""
    event["timestamp"] = datetime.utcnow().isoformat() + "Z"
    event["ts_epoch"] = time.time()
    with _history_lock:
        _history.append(event)


def _get_history(limit: int = 50, trigger: str = None, status: str = None) -> list:
    """Query recent history, optionally filtered."""
    with _history_lock:
        items = list(_history)
    items.reverse()  # newest first
    if trigger:
        items = [e for e in items if e.get("trigger") == trigger]
    if status:
        items = [e for e in items if e.get("status") == status]
    return items[:limit]


# ── Rate Limiting ────────────────────────────────────────────────────────────

class RateLimiter:
    """Per-source rate limiter. Max N requests per minute.
    Bounded: evicts stale sources on every check, caps at 1000 sources max."""

    MAX_SOURCES = 1000  # Hard cap to prevent unbounded memory growth

    def __init__(self, max_per_minute: int = 5):
        self.max_per_minute = max_per_minute
        self._timestamps: Dict[str, list] = {}
        self._lock = Lock()
        self._last_cleanup = time.time()

    def update_limit(self, max_per_minute: int):
        self.max_per_minute = max_per_minute

    def check(self, source: str) -> bool:
        """Returns True if request is allowed."""
        with self._lock:
            now = time.time()
            cutoff = now - 60

            # Periodic full cleanup — every 5 minutes, evict all stale sources
            if now - self._last_cleanup > 300:
                stale_sources = [
                    s for s, ts in self._timestamps.items()
                    if not any(t > cutoff for t in ts)
                ]
                for s in stale_sources:
                    del self._timestamps[s]
                # Hard cap: if still too many, drop oldest
                if len(self._timestamps) > self.MAX_SOURCES:
                    sorted_sources = sorted(
                        self._timestamps.items(),
                        key=lambda x: max(x[1]) if x[1] else 0,
                    )
                    for s, _ in sorted_sources[:len(self._timestamps) - self.MAX_SOURCES]:
                        del self._timestamps[s]
                self._last_cleanup = now

            timestamps = self._timestamps.get(source, [])
            timestamps = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= self.max_per_minute:
                return False
            timestamps.append(now)
            self._timestamps[source] = timestamps
            return True


_rate_limiter = RateLimiter(max_per_minute=CFG.rate_limit)


# ── Messaging with Retry ────────────────────────────────────────────────────

_UNSAFE_CHARS = re.compile(r'[\x00-\x1f]')

MAX_RETRIES = 3
RETRY_DELAYS = [1, 3, 8]  # seconds — exponential-ish


def _send_bb(text: str, chat_id: int) -> bool:
    """Send via BlueBubbles REST API."""
    if not CFG.bb_enabled or not CFG.bb_password:
        return False

    chat_guid = CFG.bb_chat_map.get(chat_id, "")
    if not chat_guid:
        return False

    try:
        import requests
        url = f"{CFG.bb_url}/api/v1/message/text"
        resp = requests.post(
            url,
            json={"chatGuid": chat_guid, "message": text, "method": CFG.bb_send_method},
            params={"password": CFG.bb_password},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        return resp.status_code == 200
    except (ConnectionError, TimeoutError, OSError) as e:
        return False


def _send_osascript(text: str, chat_id: int) -> bool:
    """Send via AppleScript."""
    handle_info = CFG.chat_handles.get(chat_id)
    if not handle_info:
        return False

    handle, handle_type = handle_info
    escaped = _UNSAFE_CHARS.sub("", text).replace("\\", "\\\\").replace('"', '\\"')
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
        result = subprocess.run([CFG.osascript, "-e", script], timeout=30, capture_output=True)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        return False


def _send_once(text: str, chat_id: int) -> bool:
    """Single send attempt via best available transport."""
    if _send_bb(text, chat_id):
        return True
    if _send_osascript(text, chat_id):
        return True
    try:
        result = subprocess.run(
            [CFG.imsg, "send", "--chat-id", str(chat_id),
             "--service", "imessage", "--text", text],
            timeout=30, check=False, capture_output=True,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        return False


def send_message(text: str, chat_id: int = None) -> bool:
    """Send a message with retry on failure."""
    global _total_fires, _total_success, _total_failures

    chat_id = chat_id or CFG.personal_chat_id
    text = text[:1500]

    with _state_lock:
        _total_fires += 1

    for attempt in range(MAX_RETRIES):
        if _send_once(text, chat_id):
            with _state_lock:
                _total_success += 1
            return True
        if attempt < MAX_RETRIES - 1:
            delay = RETRY_DELAYS[attempt]
            _log(f"Send failed (attempt {attempt + 1}/{MAX_RETRIES}), retrying in {delay}s...")
            time.sleep(delay)

    _log(f"Send FAILED after {MAX_RETRIES} attempts")
    with _state_lock:
        _total_failures += 1
    return False


# ── Audit ────────────────────────────────────────────────────────────────────

def audit_log(event: str, data: dict):
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "event": event,
        **data,
    }
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except (IOError, OSError) as e:
        pass


# ── Logging ──────────────────────────────────────────────────────────────────

def _log(msg: str):
    timestamp = datetime.now().isoformat()
    line = f"[{timestamp}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except (IOError, OSError) as e:
        pass


# ── Config Persistence ───────────────────────────────────────────────────────

def _persist_triggers():
    """Write current triggers back to proactive_triggers.yaml (webhooks section only)."""
    if not TRIGGERS_YAML.exists():
        return

    with open(TRIGGERS_YAML) as f:
        full_data = yaml.safe_load(f) or {}

    if "webhooks" not in full_data:
        full_data["webhooks"] = {}

    full_data["webhooks"]["triggers"] = CFG.triggers

    with open(TRIGGERS_YAML, "w") as f:
        yaml.dump(full_data, f, default_flow_style=False, sort_keys=False)


# ── SIGHUP Reload ────────────────────────────────────────────────────────────

def _handle_sighup(signum, frame):
    _log("SIGHUP received — reloading config...")
    _do_reload()


def _do_reload():
    """Reload config from disk and update rate limiter."""
    CFG.reload()
    _rate_limiter.update_limit(CFG.rate_limit)
    _log(f"Config reloaded. Triggers: {list(CFG.triggers.keys())}")
    _log(f"Rate limit: {CFG.rate_limit}/min, Secret: {'set' if CFG.secret else 'unset'}")
    audit_log("config_reloaded", {"triggers": list(CFG.triggers.keys())})


# ── Webhook Handler ──────────────────────────────────────────────────────────

class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP request handler for webhook endpoints."""

    def do_GET(self):
        path = self.path.split("?")[0]
        params = {}
        if "?" in self.path:
            params = parse_qs(urlparse(self.path).query)

        if path == "/health":
            self._respond(200, {
                "status": "ok",
                "version": "2.0",
                "triggers": list(CFG.triggers.keys()),
                "uptime_seconds": int(time.time() - _server_start_time),
            })

        elif path == "/admin/triggers":
            triggers_out = {}
            for tid, tcfg in CFG.triggers.items():
                triggers_out[tid] = {
                    "template": tcfg.get("template", ""),
                    "chat_id": tcfg.get("chat_id", CFG.personal_chat_id),
                    "fields": _extract_template_fields(tcfg.get("template", "")),
                }
            self._respond(200, {"triggers": triggers_out})

        elif path == "/admin/history":
            limit = int(params.get("limit", ["50"])[0])
            trigger = params.get("trigger", [None])[0]
            status = params.get("status", [None])[0]
            history = _get_history(limit=limit, trigger=trigger, status=status)
            self._respond(200, {"count": len(history), "events": history})

        elif path == "/admin/stats":
            with _state_lock:
                fires = _total_fires
                success = _total_success
                failures = _total_failures
            uptime = int(time.time() - _server_start_time)
            self._respond(200, {
                "uptime_seconds": uptime,
                "total_fires": fires,
                "total_success": success,
                "total_failures": failures,
                "success_rate": round(success / fires * 100, 1) if fires else 0,
                "triggers_registered": len(CFG.triggers),
                "rate_limit": CFG.rate_limit,
            })

        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]

        # Admin endpoints — auth required if secret set
        if path.startswith("/admin/"):
            if not self._check_auth():
                return
            if path == "/admin/reload":
                self._handle_reload()
            elif path == "/admin/triggers":
                self._handle_register_trigger()
            else:
                self._respond(404, {"error": "not found"})
            return

        # Webhook endpoints — auth required if secret set
        if not self._check_auth():
            return

        # Rate limit
        source = self.headers.get("X-Rout-Source", self.client_address[0])
        if not _rate_limiter.check(source):
            _log(f"Rate limited: {source}")
            audit_log("webhook_rate_limited", {"source": source})
            _record_event({
                "type": "rate_limited",
                "source": source,
                "status": "rejected",
            })
            self._respond(429, {"error": "rate limited"})
            return

        # Parse body
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._respond(400, {"error": f"invalid JSON: {e}"})
            return

        # Route
        if path.startswith("/webhook/"):
            trigger_id = path[len("/webhook/"):]
            self._handle_named_trigger(trigger_id, payload, source)
        elif path == "/webhook":
            self._handle_generic_webhook(payload, source)
        else:
            self._respond(404, {"error": "not found"})

    def do_DELETE(self):
        path = self.path.split("?")[0]

        if not self._check_auth():
            return

        # DELETE /admin/triggers/<id> — remove a trigger
        if path.startswith("/admin/triggers/"):
            trigger_id = path[len("/admin/triggers/"):]
            if trigger_id in CFG.triggers:
                del CFG.triggers[trigger_id]
                _persist_triggers()
                _log(f"Trigger deleted: {trigger_id}")
                audit_log("trigger_deleted", {"trigger": trigger_id})
                self._respond(200, {"ok": True, "deleted": trigger_id})
            else:
                self._respond(404, {"error": f"unknown trigger: {trigger_id}"})
        else:
            self._respond(404, {"error": "not found"})

    def _check_auth(self) -> bool:
        """Check authentication. Returns True if OK, sends 401 and returns False if not.
        Always requires a secret — never allows unauthenticated access."""
        if not CFG.secret:
            _log(f"CRITICAL: No secret configured — rejecting request from {self.client_address[0]}")
            self._respond(500, {"error": "server misconfigured: no secret"})
            return False

        header_secret = self.headers.get("X-Rout-Secret", "")
        param_secret = ""
        if "?" in self.path:
            params = parse_qs(urlparse(self.path).query)
            param_secret = params.get("secret", [""])[0]

        # Constant-time comparison to prevent timing attacks
        # Always run compare_digest even on empty strings to avoid timing leaks
        import hmac
        header_ok = hmac.compare_digest(header_secret, CFG.secret)
        param_ok = hmac.compare_digest(param_secret, CFG.secret)

        if not header_ok and not param_ok:
            _log(f"Auth failed from {self.client_address[0]}")
            audit_log("webhook_auth_failed", {"ip": self.client_address[0]})
            self._respond(401, {"error": "unauthorized"})
            return False
        return True

    def _handle_reload(self):
        """POST /admin/reload — hot-reload config from disk."""
        _do_reload()
        self._respond(200, {
            "ok": True,
            "triggers": list(CFG.triggers.keys()),
            "rate_limit": CFG.rate_limit,
        })

    def _handle_register_trigger(self):
        """POST /admin/triggers — register a new trigger at runtime, persist to YAML."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._respond(400, {"error": f"invalid JSON: {e}"})
            return

        trigger_id = payload.get("id", "").strip()
        template = payload.get("template", "").strip()

        if not trigger_id:
            self._respond(400, {"error": "missing 'id' field"})
            return
        if not re.match(r'^[a-zA-Z0-9_-]+$', trigger_id):
            self._respond(400, {"error": "trigger id must be alphanumeric/hyphens/underscores"})
            return
        if not template:
            self._respond(400, {"error": "missing 'template' field"})
            return

        chat_id = payload.get("chat_id", CFG.personal_chat_id)
        overwrite = payload.get("overwrite", False)

        if trigger_id in CFG.triggers and not overwrite:
            self._respond(409, {"error": f"trigger '{trigger_id}' already exists. Set overwrite:true to replace."})
            return

        CFG.triggers[trigger_id] = {
            "template": template,
            "chat_id": chat_id,
        }
        _persist_triggers()

        _log(f"Trigger registered: {trigger_id} -> {template}")
        audit_log("trigger_registered", {
            "trigger": trigger_id,
            "template": template,
            "chat_id": chat_id,
        })

        self._respond(201, {
            "ok": True,
            "trigger": trigger_id,
            "template": template,
            "chat_id": chat_id,
            "fields": _extract_template_fields(template),
        })

    def _handle_named_trigger(self, trigger_id: str, payload: dict, source: str):
        """Handle a named trigger with config-based template."""
        trigger_cfg = CFG.triggers.get(trigger_id)
        if not trigger_cfg:
            self._respond(404, {"error": f"unknown trigger: {trigger_id}"})
            return

        template = trigger_cfg.get("template", "")
        chat_id = trigger_cfg.get("chat_id", CFG.personal_chat_id)

        if template:
            try:
                message = template.format(**payload)
            except KeyError as e:
                self._respond(400, {
                    "error": f"missing template field: {e}",
                    "required_fields": _extract_template_fields(template),
                    "provided_fields": list(payload.keys()),
                })
                return
        else:
            message = payload.get("message", json.dumps(payload, indent=2))

        _log(f"Trigger [{trigger_id}] from {source}: {message[:100]}")
        audit_log("webhook_trigger", {
            "trigger": trigger_id,
            "source": source,
            "chat_id": chat_id,
            "message_preview": message[:100],
        })

        success = send_message(message, chat_id=chat_id)

        _record_event({
            "type": "named_trigger",
            "trigger": trigger_id,
            "source": source,
            "chat_id": chat_id,
            "message_preview": message[:120],
            "status": "ok" if success else "failed",
        })

        self._respond(200 if success else 500, {
            "ok": success,
            "trigger": trigger_id,
            "message_length": len(message),
        })

    def _handle_generic_webhook(self, payload: dict, source: str):
        """Handle a generic webhook -- expects 'message' field or processes payload."""
        message = payload.get("message", "")
        chat_id = payload.get("chat_id", CFG.personal_chat_id)

        if not message:
            message = self._extract_message(payload)

        if not message:
            self._respond(400, {"error": "no 'message' field and couldn't extract one from payload"})
            return

        _log(f"Generic webhook from {source}: {message[:100]}")
        audit_log("webhook_generic", {
            "source": source,
            "chat_id": chat_id,
            "message_preview": message[:100],
        })

        success = send_message(message, chat_id=chat_id)

        _record_event({
            "type": "generic",
            "trigger": "_generic",
            "source": source,
            "chat_id": chat_id,
            "message_preview": message[:120],
            "status": "ok" if success else "failed",
        })

        self._respond(200 if success else 500, {
            "ok": success,
            "message_length": len(message),
        })

    def _extract_message(self, payload: dict) -> str:
        """Try to extract a meaningful message from common webhook formats."""
        # GitHub-style
        if "action" in payload and "repository" in payload:
            repo = payload["repository"].get("full_name", "?") if isinstance(payload["repository"], dict) else payload["repository"]
            return f"GitHub: {payload['action']} on {repo}"

        # Alert-style (Grafana, PagerDuty, etc.)
        if "title" in payload:
            body = payload.get("body", payload.get("description", ""))
            title = payload["title"]
            return f"{title}: {body}" if body else title

        # Text field fallback
        for key in ("text", "content", "body", "description", "summary"):
            if key in payload and isinstance(payload[key], str):
                return payload[key]

        return ""

    def _respond(self, status: int, body: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))

    def log_message(self, format, *args):
        """Suppress default HTTP logging -- we use our own."""
        pass


# ── Utilities ────────────────────────────────────────────────────────────────

def _extract_template_fields(template: str) -> list:
    """Extract {field} names from a template string."""
    return re.findall(r'\{(\w+)\}', template)


# ── Main ─────────────────────────────────────────────────────────────────────

def run_server(port: int = None):
    """Start the webhook server."""
    global _server_start_time
    port = port or CFG.port

    if not CFG.webhook_cfg.get("enabled", False):
        _log("Webhook server disabled in config. Set webhooks.enabled: true")
        sys.exit(0)

    if not CFG.secret:
        _log("FATAL: No webhook secret configured. Set webhooks.secret in proactive_triggers.yaml")
        _log("Refusing to start without authentication. Exiting.")
        sys.exit(1)

    # Register SIGHUP for hot-reload
    signal.signal(signal.SIGHUP, _handle_sighup)

    _server_start_time = time.time()
    server = HTTPServer(("127.0.0.1", port), WebhookHandler)
    _log(f"Webhook server v2 started on http://127.0.0.1:{port}")
    _log(f"Triggers: {list(CFG.triggers.keys()) or ['(none -- use generic /webhook)']}")
    _log(f"Admin: /admin/triggers, /admin/history, /admin/stats, /admin/reload")
    _log(f"Health: http://127.0.0.1:{port}/health")
    _log(f"Hot-reload: kill -HUP {os.getpid()} or POST /admin/reload")

    audit_log("webhook_server_started", {
        "version": "2.0",
        "port": port,
        "pid": os.getpid(),
        "triggers": list(CFG.triggers.keys()),
    })

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("Webhook server stopped")
        audit_log("webhook_server_stopped", {"reason": "keyboard_interrupt"})
    finally:
        server.server_close()


# ── CLI Mode ─────────────────────────────────────────────────────────────────

def _cli_fire(args: list):
    """Fire a trigger from the CLI: rout-webhook fire <trigger_id> key=val key=val"""
    import requests as req

    if len(args) < 1:
        print("Usage: rout-webhook fire <trigger_id> [key=value ...]")
        sys.exit(1)

    trigger_id = args[0]
    payload = {}
    for arg in args[1:]:
        if "=" in arg:
            k, v = arg.split("=", 1)
            # Try to parse as number
            try:
                v = int(v)
            except ValueError:
                try:
                    v = float(v)
                except ValueError:
                    pass
            payload[k] = v

    url = f"http://127.0.0.1:{CFG.port}/webhook/{trigger_id}"
    headers = {"Content-Type": "application/json"}
    if CFG.secret:
        headers["X-Rout-Secret"] = CFG.secret

    try:
        resp = req.post(url, json=payload, headers=headers, timeout=10)
        data = resp.json()
        if data.get("ok"):
            print(f"Fired [{trigger_id}] -> {data.get('message_length', '?')} chars")
        else:
            print(f"FAILED: {data}")
        sys.exit(0 if data.get("ok") else 1)
    except (ConnectionError, TimeoutError, OSError, ValueError) as e:
        print(f"Error: {e}")
        print("Is the webhook server running?")
        sys.exit(1)


def _cli_triggers():
    """List registered triggers."""
    import requests as req

    url = f"http://127.0.0.1:{CFG.port}/admin/triggers"
    headers = {}
    if CFG.secret:
        headers["X-Rout-Secret"] = CFG.secret

    try:
        resp = req.get(url, headers=headers, timeout=5)
        data = resp.json()
        triggers = data.get("triggers", {})
        if not triggers:
            print("No triggers registered.")
            return
        for tid, tcfg in triggers.items():
            fields = ", ".join(tcfg.get("fields", [])) or "(none)"
            print(f"  {tid}")
            print(f"    template: {tcfg.get('template', '')}")
            print(f"    fields:   {fields}")
            print(f"    chat_id:  {tcfg.get('chat_id', '?')}")
            print()
    except (ConnectionError, TimeoutError, OSError, ValueError) as e:
        print(f"Error: {e}")
        sys.exit(1)


def _cli_history(args: list):
    """Show recent webhook history."""
    import requests as req

    limit = 10
    for i, arg in enumerate(args):
        if arg == "--last" and i + 1 < len(args):
            limit = int(args[i + 1])

    url = f"http://127.0.0.1:{CFG.port}/admin/history?limit={limit}"
    headers = {}
    if CFG.secret:
        headers["X-Rout-Secret"] = CFG.secret

    try:
        resp = req.get(url, headers=headers, timeout=5)
        data = resp.json()
        events = data.get("events", [])
        if not events:
            print("No recent events.")
            return
        for e in events:
            ts = e.get("timestamp", "?")[:19]
            trigger = e.get("trigger", "?")
            status = e.get("status", "?")
            preview = e.get("message_preview", "")[:60]
            marker = "+" if status == "ok" else "x"
            print(f"  [{marker}] {ts}  {trigger:20s}  {preview}")
    except (ConnectionError, TimeoutError, OSError, ValueError) as e:
        print(f"Error: {e}")
        sys.exit(1)


def _cli_stats():
    """Show server stats."""
    import requests as req

    url = f"http://127.0.0.1:{CFG.port}/admin/stats"
    headers = {}
    if CFG.secret:
        headers["X-Rout-Secret"] = CFG.secret

    try:
        resp = req.get(url, headers=headers, timeout=5)
        data = resp.json()
        uptime_h = data.get("uptime_seconds", 0) / 3600
        print(f"  Uptime:       {uptime_h:.1f}h")
        print(f"  Total fires:  {data.get('total_fires', 0)}")
        print(f"  Success:      {data.get('total_success', 0)}")
        print(f"  Failures:     {data.get('total_failures', 0)}")
        print(f"  Success rate: {data.get('success_rate', 0)}%")
        print(f"  Triggers:     {data.get('triggers_registered', 0)}")
        print(f"  Rate limit:   {data.get('rate_limit', '?')}/min")
    except (ConnectionError, TimeoutError, OSError, ValueError) as e:
        print(f"Error: {e}")
        sys.exit(1)


def _cli_register(args: list):
    """Register a new trigger: rout-webhook register <id> <template> [--chat-id N]"""
    import requests as req

    if len(args) < 2:
        print('Usage: rout-webhook register <trigger_id> "<template>" [--chat-id N]')
        sys.exit(1)

    trigger_id = args[0]
    template = args[1]
    chat_id = CFG.personal_chat_id
    overwrite = False

    for i, arg in enumerate(args[2:], start=2):
        if arg == "--chat-id" and i + 1 < len(args):
            chat_id = int(args[i + 1])
        if arg == "--overwrite":
            overwrite = True

    url = f"http://127.0.0.1:{CFG.port}/admin/triggers"
    headers = {"Content-Type": "application/json"}
    if CFG.secret:
        headers["X-Rout-Secret"] = CFG.secret

    payload = {
        "id": trigger_id,
        "template": template,
        "chat_id": chat_id,
        "overwrite": overwrite,
    }

    try:
        resp = req.post(url, json=payload, headers=headers, timeout=5)
        data = resp.json()
        if data.get("ok"):
            fields = ", ".join(data.get("fields", []))
            print(f"Registered [{trigger_id}]")
            print(f"  template: {template}")
            print(f"  fields:   {fields}")
        else:
            print(f"FAILED: {data.get('error', data)}")
            sys.exit(1)
    except (ConnectionError, TimeoutError, OSError, ValueError) as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        cmd = sys.argv[1]

        if cmd == "fire":
            _cli_fire(sys.argv[2:])
        elif cmd == "triggers":
            _cli_triggers()
        elif cmd == "history":
            _cli_history(sys.argv[2:])
        elif cmd == "stats":
            _cli_stats()
        elif cmd == "register":
            _cli_register(sys.argv[2:])
        elif cmd == "--port":
            port = int(sys.argv[2]) if len(sys.argv) > 2 else CFG.port
            run_server(port=port)
        else:
            print("Rout Webhook Server v2")
            print()
            print("Server mode:")
            print("  webhook_server.py              Start server")
            print("  webhook_server.py --port 7888  Start on custom port")
            print()
            print("CLI mode (talks to running server):")
            print("  webhook_server.py fire <trigger> key=val ...")
            print("  webhook_server.py triggers")
            print("  webhook_server.py history [--last N]")
            print("  webhook_server.py stats")
            print('  webhook_server.py register <id> "<template>" [--chat-id N] [--overwrite]')
            sys.exit(0)
    else:
        run_server()
