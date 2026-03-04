#!/usr/bin/env python3
"""
BlueBubbles Push Transport — Socket.IO real-time message delivery
=================================================================
Replaces polling-based message ingestion with a persistent Socket.IO
connection to the BlueBubbles server running on the same Mac.

Architecture:
  - Connects to BB server via Socket.IO (localhost, sub-ms latency)
  - Listens for 'new-message' events → feeds into CommandWatcher pipeline
  - Sends via BB REST API (POST /api/v1/message/text) — faster + more
    reliable than osascript, supports Private API features
  - Auto-reconnects with exponential backoff on disconnect
  - Falls back to polling if BB is unreachable (watcher handles this)

Usage:
    from comms.bb_push import BlueBubblesPush

    bb = BlueBubblesPush(config, on_message=handle_new_message)
    bb.start()          # non-blocking, runs in background thread
    bb.send("Hello", chat_guid="iMessage;-;+15551234567")
    bb.stop()

Config (in config.yaml):
    bluebubbles:
      enabled: true
      server_url: "http://localhost:1234"
      password: "your-bb-server-password"
      send_method: "private-api"   # or "apple-script"
"""

import json
import logging
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

try:
    import socketio
    HAS_SOCKETIO = True
except ImportError:
    HAS_SOCKETIO = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


logger = logging.getLogger("rout.bb_push")


class BlueBubblesPush:
    """
    Socket.IO push transport for BlueBubbles.

    Connects to the BB server, listens for real-time events,
    and provides REST API-based message sending.
    """

    # BB Socket.IO event names
    EVENT_NEW_MESSAGE = "new-message"
    EVENT_MESSAGE_UPDATED = "updated-message"
    EVENT_MESSAGE_ERROR = "message-send-error"
    EVENT_TYPING = "typing-indicator"
    EVENT_CHAT_READ = "chat-read-status-changed"
    EVENT_GROUP_NAME = "group-name-change"
    EVENT_PARTICIPANT_ADDED = "participant-added"
    EVENT_PARTICIPANT_REMOVED = "participant-removed"

    def __init__(
        self,
        config: dict,
        on_message: Optional[Callable] = None,
        on_event: Optional[Callable] = None,
        audit_log_fn: Optional[Callable] = None,
    ):
        """
        Args:
            config: Full config dict (reads bluebubbles: section)
            on_message: Callback for new messages. Signature:
                        on_message(text, sender, chat_guid, chat_id, is_group, attachments, raw_data)
            on_event: Callback for all other events (typing, read receipts, etc.)
                      on_event(event_type, data)
            audit_log_fn: Optional audit logging function(event_type, data_dict)
        """
        bb_cfg = config.get("bluebubbles", {})
        self.enabled = bb_cfg.get("enabled", False)
        self.server_url = bb_cfg.get("server_url", "http://localhost:1234").rstrip("/")
        self.password = bb_cfg.get("password", "")
        self.send_method = bb_cfg.get("send_method", "private-api")

        self._on_message = on_message
        self._on_event = on_event
        self._audit_log = audit_log_fn or (lambda *a, **k: None)

        # Chat GUID -> local chat_id mapping (populated from config)
        self._guid_to_chat_id: Dict[str, int] = {}
        self._chat_id_to_guid: Dict[int, str] = {}
        self._build_chat_maps(config)

        # Connection state — _sio_lock guards all _sio read/write access
        self._sio: Optional[socketio.Client] = None
        self._sio_lock = threading.Lock()
        self._connected = False
        self._should_run = False
        self._thread: Optional[threading.Thread] = None
        self._reconnect_delay = 1  # exponential backoff start
        self._max_reconnect_delay = 60
        self._last_event_time = 0.0
        self._last_pong_time = 0.0  # tracks last successful health check
        self._health_thread: Optional[threading.Thread] = None
        self._HEALTH_CHECK_INTERVAL = 30  # seconds between liveness pings
        self._ZOMBIE_THRESHOLD = 45  # seconds with no events before force-reconnect

        # Known senders from config
        self._known_senders = config.get("known_senders", {})
        self._personal_chat_id = config.get("chats", {}).get("personal_id", 1)

    def _build_chat_maps(self, config: dict):
        """Build chat_guid <-> chat_id mappings from config."""
        bb_cfg = config.get("bluebubbles", {})
        chat_map = bb_cfg.get("chat_map", {})

        # chat_map format in config.yaml:
        #   chat_map:
        #     1: "iMessage;-;+15551234567"
        #     2: "iMessage;-;chat123456"
        for chat_id_str, guid in chat_map.items():
            chat_id = int(chat_id_str)
            self._guid_to_chat_id[guid] = chat_id
            self._chat_id_to_guid[chat_id] = guid

    @property
    def connected(self) -> bool:
        with self._sio_lock:
            return self._connected and self._sio is not None

    @property
    def available(self) -> bool:
        """Whether BB push transport is configured and importable."""
        return self.enabled and HAS_SOCKETIO and HAS_REQUESTS and bool(self.password)

    def start(self):
        """Start the Socket.IO connection in a background thread."""
        if not self.available:
            missing = []
            if not HAS_SOCKETIO:
                missing.append("python-socketio[client]")
            if not HAS_REQUESTS:
                missing.append("requests")
            if not self.enabled:
                missing.append("bluebubbles.enabled=true in config")
            if not self.password:
                missing.append("bluebubbles.password in config")
            logger.warning(f"BB push not available. Missing: {', '.join(missing)}")
            return False

        self._should_run = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="bb-push")
        self._thread.start()
        # Start health check thread — detects zombie sockets and force-reconnects
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True, name="bb-health")
        self._health_thread.start()
        logger.info("BB push transport starting (with health monitor)")
        return True

    def stop(self):
        """Disconnect and stop the background thread. Thread-safe."""
        self._should_run = False
        with self._sio_lock:
            if self._sio and self._connected:
                try:
                    self._sio.disconnect()
                except (socket.error, ConnectionError, OSError):
                    pass
        self._connected = False
        logger.info("BB push transport stopped")

    def _run_loop(self):
        """Background thread: connect, reconnect on failure."""
        while self._should_run:
            try:
                self._connect()
            except Exception as e:
                logger.error(f"BB push connection error: {e}")
                self._connected = False
                self._audit_log("bb_push_error", {"error": str(e)})

            if self._should_run:
                delay = min(self._reconnect_delay, self._max_reconnect_delay)
                logger.info(f"BB push reconnecting in {delay}s")
                time.sleep(delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    def force_reconnect(self):
        """Force-kill the current socket and let _run_loop reconnect.
        Called by health monitor when zombie connection detected.
        Thread-safe: acquires _sio_lock before touching _sio."""
        logger.warning("Force-reconnecting: killing current socket")
        self._audit_log("bb_push_force_reconnect", {
            "last_event_age": time.time() - self._last_event_time if self._last_event_time else -1,
            "last_pong_age": time.time() - self._last_pong_time if self._last_pong_time else -1,
        })
        self._connected = False
        with self._sio_lock:
            if self._sio:
                try:
                    self._sio.disconnect()
                except (socket.error, ConnectionError, OSError):
                    pass
                self._sio = None

    def _health_loop(self):
        """Background thread: periodically verify BB server is reachable
        and that the socket is actually delivering events. Detects zombie
        connections that Socket.IO's built-in heartbeat misses."""
        # Wait for initial connection
        time.sleep(self._HEALTH_CHECK_INTERVAL)

        while self._should_run:
            try:
                with self._sio_lock:
                    sio_alive = self._connected and self._sio is not None
                if sio_alive:
                    # Active liveness check: hit BB REST API
                    alive = self._ping_server()
                    if alive:
                        self._last_pong_time = time.time()
                    else:
                        logger.warning("BB health check: server unreachable, forcing reconnect")
                        self.force_reconnect()
                        time.sleep(self._HEALTH_CHECK_INTERVAL)
                        continue

                    # Zombie detection: if connected but no events for too long,
                    # the socket is probably dead (half-open TCP)
                    if self._last_event_time > 0:
                        event_age = time.time() - self._last_event_time
                        if event_age > self._ZOMBIE_THRESHOLD:
                            # Don't force-reconnect just because no messages came in.
                            # Only reconnect if the REST API ping worked (server is up)
                            # but we're getting no socket events — that's the zombie signal.
                            logger.warning(
                                f"BB health check: no socket events in {event_age:.0f}s "
                                f"but server is reachable — zombie socket detected"
                            )
                            self.force_reconnect()
                            time.sleep(self._HEALTH_CHECK_INTERVAL)
                            continue

            except Exception as e:
                logger.error(f"BB health check error: {e}")

            time.sleep(self._HEALTH_CHECK_INTERVAL)

    def _ping_server(self) -> bool:
        """Quick REST API ping to verify BB server is alive."""
        if not HAS_REQUESTS:
            return False
        try:
            url = f"{self.server_url}/api/v1/server/info"
            params = {"password": self.password}
            resp = requests.get(url, params=params, timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def _connect(self):
        """Establish Socket.IO connection to BlueBubbles server."""
        # IMPORTANT: reconnection=False — we own reconnection in _run_loop.
        # Socket.IO's internal reconnection fights our external loop and
        # can silently create connections _run_loop doesn't track.
        sio = socketio.Client(
            reconnection=False,
            logger=False,
        )
        with self._sio_lock:
            self._sio = sio

        @self._sio.event
        def connect():
            self._connected = True
            self._reconnect_delay = 1  # reset backoff on success
            self._last_event_time = time.time()  # connection itself is an event
            self._last_pong_time = time.time()
            logger.info(f"BB push connected to {self.server_url}")
            self._audit_log("bb_push_connected", {"server": self.server_url})

        @self._sio.event
        def disconnect():
            self._connected = False
            logger.warning("BB push disconnected")
            self._audit_log("bb_push_disconnected", {})

        @self._sio.on(self.EVENT_NEW_MESSAGE)
        def on_new_message(data):
            self._handle_new_message(data)

        @self._sio.on(self.EVENT_MESSAGE_UPDATED)
        def on_message_updated(data):
            self._last_event_time = time.time()
            if self._on_event:
                self._on_event("message_updated", data)

        @self._sio.on(self.EVENT_TYPING)
        def on_typing(data):
            self._last_event_time = time.time()
            if self._on_event:
                self._on_event("typing", data)

        @self._sio.on(self.EVENT_CHAT_READ)
        def on_chat_read(data):
            self._last_event_time = time.time()
            if self._on_event:
                self._on_event("chat_read", data)

        @self._sio.on(self.EVENT_MESSAGE_ERROR)
        def on_send_error(data):
            logger.error(f"BB send error: {data}")
            self._audit_log("bb_send_error", {"data": str(data)[:200]})

        # Connect with auth
        # BB authenticates Socket.IO via query param (guid=password)
        # The auth dict is a handshake payload, NOT a query param —
        # BB ignores it. Pass password in the URL query string instead.
        connect_url = f"{self.server_url}?guid={self.password}"
        self._sio.connect(
            connect_url,
            transports=["websocket"],
            wait_timeout=10,
        )

        # Block this thread while connected
        self._sio.wait()

    def _handle_new_message(self, data):
        """Process incoming new-message event from BB."""
        try:
            self._last_event_time = time.time()

            # BB wraps the message in a data envelope
            msg_data = data if isinstance(data, dict) else {}

            # Skip messages sent by us
            is_from_me = msg_data.get("isFromMe", False)
            if is_from_me:
                return

            text = msg_data.get("text", "").strip()
            if not text:
                return

            # Extract sender handle
            handle = msg_data.get("handle", {}) or {}
            sender_address = handle.get("address", "")

            # Extract chat info (defensive — BB may return null or non-list)
            chats = msg_data.get("chats") or []
            if not isinstance(chats, list):
                chats = []
            chat_guid = chats[0].get("guid", "") if chats else ""
            is_group = chats[0].get("groupTitle") is not None if chats else False

            # Map BB chat_guid to local chat_id
            chat_id = self._guid_to_chat_id.get(chat_guid, self._personal_chat_id)

            # Extract attachments
            attachments = []
            for att in msg_data.get("attachments", []):
                file_path = att.get("filePath", "")
                if file_path:
                    # BB stores attachments with a ~ prefix for home dir
                    if file_path.startswith("~"):
                        file_path = str(Path(file_path).expanduser())
                    attachments.append(file_path)

            # Resolve sender name
            sender_name = self._known_senders.get(sender_address, sender_address)

            self._audit_log("bb_message_received", {
                "sender": sender_address,
                "sender_name": sender_name,
                "chat_guid": chat_guid,
                "chat_id": chat_id,
                "is_group": is_group,
                "text_preview": text[:50],
            })

            logger.info(f"BB push: msg from {sender_name} in chat {chat_id}: {text[:50]}")

            # Fire callback
            if self._on_message:
                self._on_message(
                    text=text,
                    sender=sender_address,
                    chat_guid=chat_guid,
                    chat_id=chat_id,
                    is_group=is_group,
                    attachments=attachments,
                    raw_data=msg_data,
                )

        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
            logger.error(f"BB push message handling error: {e}")
            self._audit_log("bb_push_handler_error", {"error": str(e)})

    # ── Sending ──────────────────────────────────────────────────────────────

    def send(self, text: str, chat_guid: str = "", chat_id: int = 0) -> bool:
        """
        Send a message via BlueBubbles REST API.

        Args:
            text: Message text
            chat_guid: BB chat GUID (preferred, direct)
            chat_id: Local chat_id (mapped to GUID via config)

        Returns:
            True if sent successfully
        """
        if not HAS_REQUESTS:
            logger.error("requests library not installed — cannot send via BB")
            return False

        # Resolve chat_guid from chat_id if needed
        if not chat_guid and chat_id:
            chat_guid = self._chat_id_to_guid.get(chat_id, "")

        if not chat_guid:
            logger.error(f"No chat_guid for chat_id={chat_id}. Configure bluebubbles.chat_map.")
            return False

        # Truncate for iMessage
        if len(text) > 1500:
            text = text[:1497] + "..."

        try:
            url = f"{self.server_url}/api/v1/message/text"
            params = {"password": self.password}
            payload = {
                "chatGuid": chat_guid,
                "message": text,
                "method": self.send_method,
            }

            resp = requests.post(
                url,
                json=payload,
                params=params,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )

            success = resp.status_code == 200
            if not success:
                logger.error(f"BB send failed ({resp.status_code}): {resp.text[:200]}")
                self._audit_log("bb_send_failed", {
                    "status": resp.status_code,
                    "chat_guid": chat_guid,
                    "error": resp.text[:200],
                })
            else:
                self._audit_log("bb_message_sent", {
                    "chat_guid": chat_guid,
                    "message_length": len(text),
                })
                logger.info(f"BB sent to {chat_guid}: {text[:50]}")

            return success

        except requests.Timeout:
            logger.error("BB send timed out")
            self._audit_log("bb_send_timeout", {"chat_guid": chat_guid})
            return False
        except Exception as e:
            logger.error(f"BB send error: {e}")
            self._audit_log("bb_send_error", {"error": str(e), "chat_guid": chat_guid})
            return False

    def send_reaction(self, chat_guid: str, message_guid: str, reaction: str) -> bool:
        """Send a tapback reaction via BB REST API (Private API only)."""
        if not HAS_REQUESTS:
            return False

        try:
            url = f"{self.server_url}/api/v1/message/react"
            params = {"password": self.password}
            payload = {
                "chatGuid": chat_guid,
                "selectedMessageGuid": message_guid,
                "reaction": reaction,  # e.g. "love", "like", "dislike", etc.
            }
            resp = requests.post(url, json=payload, params=params, timeout=15)
            return resp.status_code == 200
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.error(f"BB reaction error: {e}")
            return False

    def mark_read(self, chat_guid: str) -> bool:
        """Mark a chat as read via BB REST API."""
        if not HAS_REQUESTS:
            return False

        try:
            url = f"{self.server_url}/api/v1/chat/{chat_guid}/read"
            params = {"password": self.password}
            resp = requests.post(url, params=params, timeout=10)
            return resp.status_code == 200
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.error(f"BB mark_read error: {e}")
            return False

    # ── Chat Discovery ───────────────────────────────────────────────────────

    def list_chats(self, limit: int = 25) -> list:
        """Fetch chat list from BB REST API. Useful for mapping GUIDs."""
        if not HAS_REQUESTS:
            return []

        try:
            url = f"{self.server_url}/api/v1/chat"
            params = {
                "password": self.password,
                "limit": limit,
                "offset": 0,
                "with": "lastMessage",
                "sort": "lastmessage",
            }
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("data", [])
            return []
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.error(f"BB list_chats error: {e}")
            return []

    def get_server_info(self) -> dict:
        """Get BB server info (version, OS version, etc.)."""
        if not HAS_REQUESTS:
            return {}

        try:
            url = f"{self.server_url}/api/v1/server/info"
            params = {"password": self.password}
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("data", {})
            return {}
        except Exception:
            return {}

    # ── Status ───────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Return current push transport status."""
        return {
            "enabled": self.enabled,
            "available": self.available,
            "connected": self.connected,
            "server_url": self.server_url,
            "last_event": datetime.fromtimestamp(self._last_event_time).isoformat()
            if self._last_event_time > 0 else None,
            "chat_map_size": len(self._guid_to_chat_id),
        }
