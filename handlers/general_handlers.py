"""
General handler — thin wrapper that routes free-form iMessages to the agent loop.

Previously 1,939 lines. Now delegates all intelligence to agent/agent_loop.py.
Keeps only: message parsing, image preparation, and response delivery.
"""

import base64
import json
import os
import re
import subprocess
import tempfile
import traceback
import yaml
from pathlib import Path
from typing import Optional

from agent.agent_loop import run as agent_run, AgentContext
from agent.safety_gate import check_pending, has_pending
from agent.providers import provider_status_line


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load personal config from config.yaml."""
    for candidate in [
        Path(__file__).parent.parent / "config.yaml",
        Path.home() / ".config/imsg-watcher/config.yaml",
    ]:
        if candidate.exists():
            with open(candidate) as f:
                return yaml.safe_load(f) or {}
    return {}


_CONFIG = _load_config()
_PATHS = _CONFIG.get("paths", {})
IMSG = _PATHS.get("imsg", "/opt/homebrew/bin/imsg")

# Re-export for watcher compatibility
from agent.providers import (
    _load_provider_state,
    _effective_provider_cooldown_until,
    _provider_enabled,
    _get_api_key,
)


# ── Image Preparation ─────────────────────────────────────────────────────────

def _prepare_image(path: str) -> Optional[tuple]:
    """Convert image to JPEG if needed and return (base64_data, media_type)."""
    try:
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return None

        ext = Path(path).suffix.lower()

        if ext in ('.heic', '.heif'):
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp_file:
                tmp = tmp_file.name
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

        if ext in ('.heic', '.heif'):
            try:
                os.unlink(tmp)
            except OSError:
                pass

        return data, media_type
    except Exception:
        return None


# ── Send ──────────────────────────────────────────────────────────────────────

def _send_to_chat(text: str, chat_id: int):
    """Send a message via imsg CLI."""
    subprocess.run(
        [IMSG, "send", "--chat-id", str(chat_id),
         "--service", "imessage", "--text", text],
        timeout=10, check=False, capture_output=True
    )


# ── Main Handler ──────────────────────────────────────────────────────────────

def claude_command(args: str = "", message: str = "", sender: str = "", metadata: dict = None) -> None:
    """
    Route free-form iMessages to the Claude agent loop.
    Accepts standard handler signature (args, message, sender, metadata)
    with legacy text-tag parsing as fallback for backwards compatibility.
    """
    metadata = metadata or {}
    text = args or message

    try:
        # Extract chat_id from metadata (v4) or fallback to text tags (legacy)
        chat_id = metadata.get("chat_id", _CONFIG.get("chats", {}).get("personal_id", 1))
        m = re.match(r'\[CHAT_ID:(\d+)\]\s*', text)
        if m:
            chat_id = int(m.group(1))
            text = text[m.end():]

        # Extract attachment paths from metadata (v4) or text tags (legacy)
        attachment_paths = metadata.get("attachments", [])
        m = re.match(r'\[ATTACHMENTS:(\[.*?\])\]\s*', text)
        if m:
            try:
                attachment_paths = json.loads(m.group(1))
            except Exception:
                pass
            text = text[m.end():]

        # Strip [From Name] tag for group chats
        m = re.match(r'\[From \w+\]\s*', text)
        if m:
            text = text[m.end():]

        # Passthrough mode
        if '/ignore' in text.lower():
            return

        # Provider status shortcut
        lower = text.strip().lower()
        if lower in {"provider status", "model status", "status provider", "status model"}:
            _send_to_chat(provider_status_line(), chat_id)
            return None

        # ── Safety gate: check for pending confirmation ──────────────────
        if has_pending():
            confirmation_result = check_pending(text, chat_id=chat_id, sender=sender)
            if confirmation_result is not None:
                _send_to_chat(confirmation_result, chat_id)
                return None
            # If not a yes/no, fall through and process as normal message

        # ── Prepare images ───────────────────────────────────────────────
        images = []
        for path in attachment_paths:
            ext = Path(path).suffix.lower()
            if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif'):
                result = _prepare_image(path)
                if result:
                    images.append(result)

        # ── Build context and run agent loop ─────────────────────────────
        context = AgentContext(
            chat_id=chat_id,
            sender=sender,
            attachment_paths=attachment_paths,
            images=images,
        )

        response = agent_run(text, context)

        # Add disclaimer if response came from local model
        local_cfg = _CONFIG.get("local_model", {})
        if local_cfg.get("disclaimer", True):
            from agent.providers import _load_provider_state as _lps
            state = _lps()
            if state.get("active_provider") == "local":
                response = f"\U0001f3e0 {response}"

        _send_to_chat(response, chat_id)
        return None

    except RuntimeError as e:
        # Clean user-facing errors from provider failover
        error_msg = str(e)
        lower_err = error_msg.lower()
        if "cloud providers cooling down and local model unavailable" in lower_err:
            friendly = "⏸️ All providers exhausted. Try again shortly."
        elif "anthropic is cooling down and codex" in lower_err:
            friendly = "⏸️ Anthropic is cooling down and backup is unavailable. Try again in a minute."
        elif "all cloud providers are cooling down" in lower_err:
            friendly = "⏸️ All cloud providers are cooling down. Try again in a minute."
        elif "429" in error_msg or "rate limit" in lower_err or "cooling down" in lower_err:
            friendly = "⏸️ Hit my rate limit — try again in a minute!"
        elif "auth failed" in lower_err or "unauthorized" in lower_err:
            user_name = _CONFIG.get("user", {}).get("name", "")
            name_prefix = f"{user_name}, " if user_name else ""
            friendly = f"⚠️ I'm having trouble connecting. {name_prefix}check the OAuth token setup."
        else:
            friendly = error_msg
        _send_to_chat(friendly, chat_id)
        return None

    except Exception:
        err_detail = traceback.format_exc()
        log_path = os.path.expanduser("~/.openclaw/workspace/imsg_watcher.log")
        try:
            with open(log_path, "a") as f:
                f.write(f"[claude_command ERROR] {err_detail}\n")
        except Exception:
            pass
        _send_to_chat("Something went wrong on my end — try again?", chat_id)
        return None
