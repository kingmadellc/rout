"""
Rout Configuration Loader — single source of truth for all config access.
==========================================================================
Replaces the duplicated _load_config() pattern across handlers, watcher,
webhook server, and proactive agent.

Features:
  - Loads ~/.openclaw/config.yaml with fallback search paths
  - Loads config/proactive_triggers.yaml for proactive/webhook config
  - Thread-safe reload via reload() method
  - Section accessors for each subsystem (kalshi, polymarket, bluebubbles, etc.)
  - TTL-based cache with manual invalidation

Usage:
    from config.loader import config

    # Access full config
    cfg = config.get()

    # Access subsections
    kalshi_cfg = config.kalshi
    bb_cfg = config.bluebubbles
    pm_cfg = config.polymarket
    proactive_cfg = config.proactive_triggers

    # Force reload from disk
    config.reload()
"""

from __future__ import annotations

import os
import threading
import time
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


# ── Paths ─────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent

OPENCLAW_DIR = Path(
    os.environ.get("ROUT_OPENCLAW_DIR", str(Path.home() / ".openclaw"))
).expanduser()

CONFIG_DIR = OPENCLAW_DIR
KEYS_DIR = OPENCLAW_DIR / "keys"
LOG_DIR = OPENCLAW_DIR / "logs"
STATE_DIR = OPENCLAW_DIR / "state"

# Config file search order (first found wins)
_CONFIG_SEARCH = [
    OPENCLAW_DIR / "config.yaml",
    PROJECT_ROOT / "config.yaml",
    Path.home() / ".config" / "imsg-watcher" / "config.yaml",
]

_TRIGGERS_YAML = PROJECT_ROOT / "config" / "proactive_triggers.yaml"


# ── Typed Config Sections ─────────────────────────────────────────────────────

@dataclass
class KalshiConfig:
    """Kalshi exchange integration configuration."""
    enabled: bool = False
    api_key_id: str = ""
    private_key_file: str = ""
    ticker_names: Dict[str, str] = field(default_factory=dict)
    research_cache_path: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> KalshiConfig:
        """Create from raw config dict."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=data.get("enabled", False),
            api_key_id=data.get("api_key_id", ""),
            private_key_file=data.get("private_key_file", ""),
            ticker_names=data.get("ticker_names", {}),
            research_cache_path=data.get("research_cache_path", ""),
        )


@dataclass
class PolymarketConfig:
    """Polymarket prediction market configuration."""
    enabled: bool = False
    watchlist: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PolymarketConfig:
        """Create from raw config dict."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=data.get("enabled", False),
            watchlist=data.get("watchlist", []),
        )


@dataclass
class BlueBubblesConfig:
    """BlueBubbles iMessage bridge configuration."""
    server_address: str = ""
    password: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> BlueBubblesConfig:
        """Create from raw config dict."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            server_address=data.get("server_address", ""),
            password=data.get("password", ""),
        )


@dataclass
class ChatsConfig:
    """Chat routing and grouping configuration."""
    personal_id: int = 1
    monitoring_groups: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ChatsConfig:
        """Create from raw config dict."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            personal_id=data.get("personal_id", 1),
            monitoring_groups=data.get("monitoring_groups", {}),
        )


@dataclass
class ProactiveConfig:
    """Proactive agent trigger configuration."""
    enabled: bool = False
    interval_seconds: int = 900
    triggers: Dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProactiveConfig:
        """Create from raw config dict."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=data.get("enabled", False),
            interval_seconds=data.get("interval_seconds", 900),
            triggers=data.get("triggers", {}),
        )


@dataclass
class WebhooksConfig:
    """Webhook server and trigger configuration."""
    enabled: bool = False
    port: int = 7888
    secret: str = ""
    triggers: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WebhooksConfig:
        """Create from raw config dict."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=data.get("enabled", False),
            port=data.get("port", 7888),
            secret=data.get("secret", ""),
            triggers=data.get("triggers", {}),
        )


# ── Config Manager ────────────────────────────────────────────────────────────

class ConfigManager:
    """Thread-safe, reload-capable configuration manager.

    Loads config once on first access. Call reload() to re-read from disk.
    All property accessors return dicts (never None).
    """

    def __init__(self) -> None:
        self._lock: threading.RLock = threading.RLock()
        self._config: Dict[str, Any] = {}
        self._triggers_config: Dict[str, Any] = {}
        self._loaded: bool = False
        self._config_path: Optional[Path] = None
        self._load_time: float = 0.0

    def _ensure_loaded(self) -> None:
        """Lazy-load config on first access."""
        if not self._loaded:
            with self._lock:
                if not self._loaded:
                    self._do_load()

    def _do_load(self) -> None:
        """Internal: load all config files from disk."""
        # Main config
        for candidate in _CONFIG_SEARCH:
            if candidate.exists():
                with open(candidate) as f:
                    self._config = yaml.safe_load(f) or {}
                self._config_path = candidate
                break
        else:
            self._config = {}
            self._config_path = None

        # Proactive triggers config
        if _TRIGGERS_YAML.exists():
            with open(_TRIGGERS_YAML) as f:
                self._triggers_config = yaml.safe_load(f) or {}
        else:
            self._triggers_config = {}

        self._loaded = True
        self._load_time = time.time()

    def reload(self) -> None:
        """Force reload all config from disk. Thread-safe."""
        with self._lock:
            self._loaded = False
            self._do_load()

    def get(self) -> Dict[str, Any]:
        """Get the full main config dict."""
        self._ensure_loaded()
        return self._config

    @property
    def config_path(self) -> Optional[Path]:
        """Path to the config file that was loaded."""
        self._ensure_loaded()
        return self._config_path

    @property
    def load_time(self) -> float:
        """Epoch timestamp of last config load."""
        self._ensure_loaded()
        return self._load_time

    # ── Section Accessors (Raw Dicts) ─────────────────────────────────────

    @property
    def kalshi(self) -> Dict[str, Any]:
        """Kalshi trading config section (raw dict)."""
        self._ensure_loaded()
        return self._config.get("kalshi", {})

    @property
    def polymarket(self) -> Dict[str, Any]:
        """Polymarket config section (raw dict)."""
        self._ensure_loaded()
        return self._config.get("polymarket", {})

    @property
    def bluebubbles(self) -> Dict[str, Any]:
        """BlueBubbles config section (raw dict)."""
        self._ensure_loaded()
        return self._config.get("bluebubbles", {})

    @property
    def chats(self) -> Dict[str, Any]:
        """Chat mapping config section (raw dict)."""
        self._ensure_loaded()
        return self._config.get("chats", {})

    @property
    def paths(self) -> Dict[str, Any]:
        """Paths config section (raw dict)."""
        self._ensure_loaded()
        return self._config.get("paths", {})

    @property
    def anthropic_api_key(self) -> str:
        """Anthropic API key."""
        self._ensure_loaded()
        return self._config.get("anthropic_api_key", "")

    @property
    def known_senders(self) -> Dict[str, Any]:
        """Known sender address -> name mappings."""
        self._ensure_loaded()
        return self._config.get("known_senders", {})

    @property
    def chat_handles(self) -> Dict[int, Tuple[str, str]]:
        """Chat ID -> (handle, type) mappings."""
        self._ensure_loaded()
        raw = self._config.get("chat_handles", {})
        return {int(k): tuple(v) for k, v in raw.items()}

    @property
    def personal_chat_id(self) -> int:
        """Default personal chat ID."""
        return self.chats.get("personal_id", 1)

    # ── Typed Section Accessors ───────────────────────────────────────────

    def kalshi_typed(self) -> KalshiConfig:
        """Get Kalshi config as typed dataclass."""
        return KalshiConfig.from_dict(self.kalshi)

    def polymarket_typed(self) -> PolymarketConfig:
        """Get Polymarket config as typed dataclass."""
        return PolymarketConfig.from_dict(self.polymarket)

    def bluebubbles_typed(self) -> BlueBubblesConfig:
        """Get BlueBubbles config as typed dataclass."""
        return BlueBubblesConfig.from_dict(self.bluebubbles)

    def chats_typed(self) -> ChatsConfig:
        """Get Chats config as typed dataclass."""
        return ChatsConfig.from_dict(self.chats)

    def proactive_typed(self) -> ProactiveConfig:
        """Get Proactive config as typed dataclass."""
        return ProactiveConfig.from_dict(self.proactive)

    def webhooks_typed(self) -> WebhooksConfig:
        """Get Webhooks config as typed dataclass."""
        return WebhooksConfig.from_dict(self.webhooks)

    # ── Proactive Triggers ────────────────────────────────────────────────

    @property
    def proactive_triggers(self) -> Dict[str, Any]:
        """Full proactive triggers config (from proactive_triggers.yaml)."""
        self._ensure_loaded()
        return self._triggers_config

    @property
    def proactive(self) -> Dict[str, Any]:
        """Proactive section from triggers config (raw dict)."""
        self._ensure_loaded()
        return self._triggers_config.get("proactive", {})

    @property
    def webhooks(self) -> Dict[str, Any]:
        """Webhooks section from triggers config (raw dict)."""
        self._ensure_loaded()
        return self._triggers_config.get("webhooks", {})

    @property
    def webhook_secret(self) -> str:
        """Webhook authentication secret."""
        return self.webhooks.get("secret", "")

    @property
    def webhook_triggers(self) -> Dict[str, Any]:
        """Registered webhook trigger definitions."""
        return self.webhooks.get("triggers", {})

    # ── Key Loading ───────────────────────────────────────────────────────

    def load_private_key(self, key_name: str) -> str:
        """Load a private key from ~/.openclaw/keys/."""
        key_path = KEYS_DIR / key_name
        if not key_path.exists():
            raise FileNotFoundError(f"Key not found: {key_path}")
        with open(key_path) as f:
            return f.read().strip()


# ── Singleton ─────────────────────────────────────────────────────────────────

config: ConfigManager = ConfigManager()
