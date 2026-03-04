"""
Shared handler utilities — DRY config, HTTP, formatting.
=========================================================
Eliminates duplicated _load_config(), HTTP helpers, and formatting
functions across kalshi_handlers.py, polymarket_handlers.py, and
general_handlers.py.

Usage:
    from handlers.shared import get_config, http_get, format_volume, format_age
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union, Literal


# ── Config Access ─────────────────────────────────────────────────────────────

def get_config() -> Dict[str, Any]:
    """Get the main Rout config dict.

    Uses the centralized ConfigManager if available, falls back to
    direct YAML loading for backward compatibility.
    """
    try:
        from config.loader import config
        return config.get()
    except ImportError:
        import yaml
        for candidate in [
            Path.home() / ".openclaw" / "config.yaml",
            Path(__file__).parent.parent / "config.yaml",
            Path.home() / ".config" / "imsg-watcher" / "config.yaml",
        ]:
            if candidate.exists():
                with open(candidate) as f:
                    return yaml.safe_load(f) or {}
        return {}


def get_section(section: str) -> Dict[str, Any]:
    """Get a specific config section (e.g., 'kalshi', 'polymarket', 'bluebubbles')."""
    return get_config().get(section, {})


# ── HTTP Helpers ──────────────────────────────────────────────────────────────

USER_AGENT: str = "Rout/1.8.0"


def http_get(
    url: str,
    params: Optional[Dict[str, str]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 10,
) -> Optional[Union[Dict[str, Any], list[Any]]]:
    """GET request with JSON parsing. Returns parsed JSON or None on failure."""
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req_headers: Dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if headers:
        req_headers.update(headers)

    try:
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def http_post(
    url: str,
    data: Dict[str, Any],
    params: Optional[Dict[str, str]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 30,
) -> Optional[Dict[str, Any]]:
    """POST request with JSON body. Returns parsed JSON or None on failure."""
    full_url: str = url
    if params:
        full_url += "?" + urllib.parse.urlencode(params)

    req_headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if headers:
        req_headers.update(headers)

    try:
        body: bytes = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(full_url, data=body, headers=req_headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


# ── Formatting Helpers ────────────────────────────────────────────────────────

def format_volume(vol: Any) -> str:
    """Format a volume number for display (e.g., $1.2M, $450K, $500)."""
    try:
        v: float = float(vol)
        if v >= 1_000_000:
            return f"${v / 1_000_000:.1f}M"
        if v >= 1_000:
            return f"${v / 1_000:.0f}K"
        return f"${v:.0f}"
    except (ValueError, TypeError):
        return "$?"


def format_age(seconds: int) -> str:
    """Format seconds into human-readable age string."""
    if seconds < 60:
        return f"{seconds}s ago"
    elif seconds < 3600:
        return f"{seconds // 60}m ago"
    elif seconds < 86400:
        return f"{seconds // 3600}h ago"
    else:
        return f"{seconds // 86400}d ago"


def format_pct(price: float) -> str:
    """Format a 0-1 price as percentage string."""
    return f"{price * 100:.0f}%"


def days_until(end_date_str: str) -> Optional[int]:
    """Calculate days until a date string (ISO format)."""
    if not end_date_str:
        return None
    try:
        clean: str = end_date_str.replace("Z", "+00:00")
        end: datetime = datetime.fromisoformat(clean)
        now: datetime = datetime.now(timezone.utc)
        delta: int = (end - now).days
        return max(0, delta)
    except Exception:
        return None


def parse_iso_age(iso_str: str) -> Optional[str]:
    """Parse an ISO timestamp and return human-readable age string."""
    if not iso_str:
        return None
    try:
        dt: datetime = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        age_sec: int = int((datetime.now(dt.tzinfo) - dt).total_seconds())
        return format_age(age_sec)
    except Exception:
        return None


# ── Audit Logging ─────────────────────────────────────────────────────────────

def audit_log(log_path: Path, event: str, data: Dict[str, Any]) -> None:
    """Append a JSON audit entry to a JSONL file. Never raises."""
    entry: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **data,
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
