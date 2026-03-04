"""
Provider failover engine for Rout.

Manages automatic switching between Anthropic (primary), Codex (secondary),
and local Ollama (tertiary) when providers hit rate limits or errors.

Extracted from handlers/general_handlers.py for clean separation.
"""

import json
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import yaml
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional, Tuple


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load personal config from config.yaml."""
    for candidate in [
        Path(__file__).parent.parent / "config.yaml",
        Path.home() / ".config/imsg-watcher/config.yaml",
    ]:
        if candidate.exists():
            with open(candidate) as f:
                data = yaml.safe_load(f) or {}
                return data if isinstance(data, dict) else {}
    return {}

CONFIG = _load_config()
PATHS = CONFIG.get("paths", {})

AUTH_PROFILES_PATH = Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
PROVIDER_STATE_PATH = Path.home() / ".openclaw" / "state" / "provider_failover.json"
PROVIDER_STATUS_PATH = Path(__file__).resolve().parent.parent / "logs" / "provider_status.json"
TOKEN_USAGE_LOG_PATH = Path.home() / ".openclaw" / "workspace" / "token_usage.jsonl"
WATCHER_LOG_PATH = Path.home() / ".openclaw" / "workspace" / "imsg_watcher.log"

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
DEFAULT_ANTHROPIC_MAX_TOKENS = 4096
DEFAULT_ANTHROPIC_COOLDOWN_SECONDS = 60
DEFAULT_CODEX_COOLDOWN_SECONDS = 120
DEFAULT_CODEX_TIMEOUT_SECONDS = 120
DEFAULT_LOCAL_MODEL_COOLDOWN_SECONDS = 30
MAX_COOLDOWN_SECONDS = 7200  # 2 hours


# ── Utilities ────────────────────────────────────────────────────────────────

def _to_int(value) -> Optional[int]:
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


def _load_json_file(path: Path) -> dict:
    try:
        with open(path, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


# ── Logging ──────────────────────────────────────────────────────────────────

def track_token_usage(input_tokens: int = 0, output_tokens: int = 0,
                      cache_read: int = 0, cache_creation: int = 0,
                      model: str = "", provider: str = "anthropic") -> None:
    try:
        TOKEN_USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
            "total_tokens": input_tokens + output_tokens,
            "cache_hit_pct": round(cache_read / max(input_tokens, 1) * 100, 1),
        }
        with open(TOKEN_USAGE_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _append_runtime_log(event: str, details: Optional[dict] = None) -> None:
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "details": details or {},
        }
        with open(WATCHER_LOG_PATH, "a") as f:
            f.write(f"[provider_failover] {json.dumps(record, ensure_ascii=True)}\n")
    except Exception:
        pass


# ── Provider Error ───────────────────────────────────────────────────────────

@dataclass
class ProviderError(Exception):
    provider: str
    kind: str
    message: str
    retry_after_seconds: Optional[int] = None
    cooldown_source: str = "heuristic"

    def __str__(self) -> str:
        return self.message


# ── Provider State ───────────────────────────────────────────────────────────

def _default_provider_state() -> dict:
    return {
        "active_provider": "anthropic",
        "providers": {
            "anthropic": {"cooldown_until": 0, "reason": "", "updated_at": 0},
            "codex": {"cooldown_until": 0, "reason": "", "updated_at": 0},
            "local": {"cooldown_until": 0, "reason": "", "updated_at": 0},
        },
    }


def _load_provider_state() -> dict:
    state = _default_provider_state()
    raw = _load_json_file(PROVIDER_STATE_PATH)
    if not raw:
        return state
    providers = raw.get("providers")
    if isinstance(providers, dict):
        for provider in ("anthropic", "codex", "local"):
            current = providers.get(provider)
            if isinstance(current, dict):
                state["providers"][provider]["cooldown_until"] = _to_int(current.get("cooldown_until")) or 0
                state["providers"][provider]["reason"] = str(current.get("reason", "")).strip()
                state["providers"][provider]["updated_at"] = _to_int(current.get("updated_at")) or 0
    active = str(raw.get("active_provider", "")).strip().lower()
    if active in {"anthropic", "codex", "local"}:
        state["active_provider"] = active
    return state


def _save_provider_state(state: dict) -> None:
    try:
        PROVIDER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = PROVIDER_STATE_PATH.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(str(tmp_path), str(PROVIDER_STATE_PATH))
    except Exception as e:
        _append_runtime_log("provider_state_save_failed", {"error": str(e)})


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
    now = int(time.time())
    capped = min(max(0, int(cooldown_until)), now + MAX_COOLDOWN_SECONDS)
    current["cooldown_until"] = capped
    current["reason"] = reason[:500]
    current["updated_at"] = now


def _clear_provider_cooldown(state: dict, provider: str) -> None:
    providers = state.setdefault("providers", {})
    current = providers.setdefault(provider, {"cooldown_until": 0, "reason": "", "updated_at": 0})
    if not isinstance(current, dict):
        current = {"cooldown_until": 0, "reason": "", "updated_at": 0}
        providers[provider] = current
    current["cooldown_until"] = 0
    current["reason"] = ""
    current["updated_at"] = int(time.time())


def _write_provider_status(provider: str, cooldown_started_at: int, cooldown_eta: int,
                           source: str, reason: str = "") -> None:
    payload = {
        "provider": provider,
        "cooldown_started_at": int(cooldown_started_at),
        "cooldown_eta": int(cooldown_eta),
        "source": str(source or "heuristic"),
        "reason": str(reason or "")[:500],
        "updated_at": int(time.time()),
    }
    try:
        PROVIDER_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = PROVIDER_STATUS_PATH.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(str(tmp_path), str(PROVIDER_STATUS_PATH))
    except Exception as e:
        _append_runtime_log("provider_status_save_failed", {"error": str(e)})


# ── Auth ─────────────────────────────────────────────────────────────────────

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
        token = str(profile.get("token") or profile.get("api_key") or
                     profile.get("apiKey") or "").strip()
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
        return min(valid_profiles, key=lambda p: int(p.get("cooldown_until", 0)))
    raise RuntimeError(
        "Anthropic OAuth token not found. Re-auth in OpenClaw: "
        "openclaw models auth login --provider anthropic"
    )


def _effective_provider_cooldown_until(state: dict, provider: str) -> int:
    local_until = _provider_cooldown_until(state, provider)
    if provider == "anthropic":
        providers = state.get("providers", {})
        anthropic_state = providers.get("anthropic", {})
        has_own_tracking = (_to_int(anthropic_state.get("updated_at")) or 0) > 0
        if has_own_tracking:
            return local_until
        try:
            info = _get_anthropic_profile_info()
            openclaw_until = _to_int(info.get("cooldown_until")) or 0
            if openclaw_until > 0:
                return openclaw_until
        except RuntimeError:
            pass
    return local_until


# ── Retry-After Parsing ──────────────────────────────────────────────────────

def _parse_retry_hint_seconds(text: str) -> Optional[int]:
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


def _parse_retry_after_seconds(headers, error_text: str, default_seconds: int) -> Tuple[int, str]:
    retry_after = _header_value(headers, ["retry-after"])
    if retry_after:
        value = _to_int(retry_after)
        if value and value > 0:
            return value, "retry-after"
        try:
            parsed = parsedate_to_datetime(retry_after)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delta = int((parsed - datetime.now(timezone.utc)).total_seconds())
            if delta > 0:
                return delta, "retry-after"
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
            if as_int > 1_000_000_000_000:
                as_int //= 1000
            if as_int > 1_000_000_000:
                delta = as_int - int(time.time())
                if delta > 0:
                    return delta, "header"
        else:
            try:
                parsed_reset = datetime.fromisoformat(reset_value.replace("Z", "+00:00"))
                if parsed_reset.tzinfo is None:
                    parsed_reset = parsed_reset.replace(tzinfo=timezone.utc)
                delta = int((parsed_reset - datetime.now(timezone.utc)).total_seconds())
                if delta > 0:
                    return delta, "header"
            except Exception:
                pass

    hint_seconds = _parse_retry_hint_seconds(error_text)
    if hint_seconds:
        return hint_seconds, "header"
    return max(1, int(default_seconds)), "heuristic"


# ── Provider Checks ──────────────────────────────────────────────────────────

def _provider_enabled(provider: str) -> bool:
    if provider == "anthropic":
        return True
    if provider == "codex":
        cfg = CONFIG.get("codex", {})
        enabled = cfg.get("enabled", True)
        if isinstance(enabled, str):
            return enabled.strip().lower() not in {"0", "false", "no", "off"}
        return bool(enabled)
    if provider == "local":
        cfg = CONFIG.get("local_model", {})
        enabled = cfg.get("enabled", False)
        if isinstance(enabled, str):
            return enabled.strip().lower() not in {"0", "false", "no", "off"}
        return bool(enabled)
    return False


def _codex_binary() -> str:
    cfg = CONFIG.get("codex", {})
    bin_path = str(cfg.get("binary", "")).strip()
    if bin_path:
        return bin_path
    return PATHS.get("codex", "codex")


def _provider_attempt_order(state: dict) -> list[str]:
    now = int(time.time())
    anthropic_ready = _effective_provider_cooldown_until(state, "anthropic") <= now
    codex_ready = _effective_provider_cooldown_until(state, "codex") <= now and _provider_enabled("codex")
    local_enabled = _provider_enabled("local")

    if anthropic_ready:
        order = ["anthropic"]
        if _provider_enabled("codex"):
            order.append("codex")
        if local_enabled:
            order.append("local")
        return order
    if codex_ready:
        order = ["codex", "anthropic"]
        if local_enabled:
            order.append("local")
        return order
    if local_enabled:
        local_ready = _effective_provider_cooldown_until(state, "local") <= now
        if local_ready:
            return ["local", "anthropic", "codex"]
    order = ["anthropic"]
    if _provider_enabled("codex"):
        order.append("codex")
    if local_enabled:
        order.append("local")
    return order


def _record_provider_error(state: dict, error: ProviderError) -> None:
    if error.kind != "cooldown":
        return
    defaults = {
        "anthropic": DEFAULT_ANTHROPIC_COOLDOWN_SECONDS,
        "codex": DEFAULT_CODEX_COOLDOWN_SECONDS,
        "local": DEFAULT_LOCAL_MODEL_COOLDOWN_SECONDS,
    }
    fallback_seconds = defaults.get(error.provider, DEFAULT_CODEX_COOLDOWN_SECONDS)
    retry_after = max(1, int(error.retry_after_seconds or fallback_seconds))
    now_ts = int(time.time())
    cooldown_eta = now_ts + retry_after
    _set_provider_cooldown(state, error.provider, cooldown_eta, reason=error.message)
    if error.provider == "anthropic":
        _write_provider_status(
            provider="anthropic",
            cooldown_started_at=now_ts,
            cooldown_eta=cooldown_eta,
            source=(error.cooldown_source or "heuristic"),
            reason=error.message,
        )


def _classify_codex_error(error_text: str) -> Tuple[str, Optional[int]]:
    lower = (error_text or "").lower()
    retry_after = _parse_retry_hint_seconds(lower)
    if any(token in lower for token in ["rate limit", "429", "too many requests", "cooldown"]):
        return ("cooldown", retry_after)
    if any(token in lower for token in ["unauthorized", "forbidden", "login", "authentication", "auth"]):
        return ("auth", retry_after)
    if any(token in lower for token in ["timed out", "timeout", "stream disconnected", "network", "connection"]):
        return ("network", retry_after)
    return ("other", retry_after)


# ── Provider Requests ────────────────────────────────────────────────────────

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


def anthropic_request(system_prompt: str, messages: list, max_tokens: int,
                      tools: list = None) -> dict:
    """Call Anthropic API with exponential backoff retry (3 attempts).

    Returns the full response dict (content blocks + stop_reason).
    When tools are provided, includes tool definitions in the API call.
    Retries on network errors and 5xx; does NOT retry on 429 (let failover handle it).
    """
    try:
        profile = _get_anthropic_profile_info()
    except RuntimeError as error:
        raise ProviderError(provider="anthropic", kind="auth", message=str(error))

    model = CONFIG.get("anthropic", {}).get("model", DEFAULT_ANTHROPIC_MODEL)
    system_blocks = [{"type": "text", "text": system_prompt,
                      "cache_control": {"type": "ephemeral"}}]
    body = {
        "model": model,
        "max_tokens": int(max_tokens or DEFAULT_ANTHROPIC_MAX_TOKENS),
        "system": system_blocks,
        "messages": messages,
    }
    if tools:
        body["tools"] = tools

    data = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {profile['token']}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "prompt-caching-2024-07-31,oauth-2025-04-20",
    }

    max_retries = 3
    last_error = None

    for attempt in range(max_retries):
        req = urllib.request.Request(ANTHROPIC_MESSAGES_URL, data=data, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=45)
            result = json.loads(resp.read())

            usage = result.get("usage", {})
            if usage:
                track_token_usage(
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    cache_read=usage.get("cache_read_input_tokens", 0),
                    cache_creation=usage.get("cache_creation_input_tokens", 0),
                    model=model,
                )
            return result

        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            if e.code == 429:
                # Rate limited — don't retry, let failover handle it
                retry_after, source = _parse_retry_after_seconds(
                    e.headers, body_text, DEFAULT_ANTHROPIC_COOLDOWN_SECONDS)
                raise ProviderError(
                    provider="anthropic", kind="cooldown",
                    message="Anthropic OAuth is cooling down.",
                    retry_after_seconds=retry_after, cooldown_source=source)
            if e.code == 401:
                raise ProviderError(provider="anthropic", kind="auth",
                                    message="Anthropic OAuth auth failed.")
            if e.code >= 500:
                last_error = ProviderError(provider="anthropic", kind="network",
                                           message=f"Anthropic API unavailable (HTTP {e.code}), attempt {attempt + 1}/{max_retries}.")
                _append_runtime_log("anthropic_retry", {"attempt": attempt + 1, "status": e.code})
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # 1s, 2s, 4s
                    continue
                raise last_error
            raise ProviderError(provider="anthropic", kind="other",
                                message=f"Anthropic API error (HTTP {e.code}).")
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            last_error = ProviderError(provider="anthropic", kind="network",
                                       message=f"Anthropic API unreachable, attempt {attempt + 1}/{max_retries}.")
            _append_runtime_log("anthropic_retry", {"attempt": attempt + 1, "error": str(e)})
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s
                continue
            raise last_error

    raise last_error or ProviderError(provider="anthropic", kind="network",
                                       message="Anthropic API request failed after retries.")


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


def codex_request(system_prompt: str, messages: list, expect_json: bool = False) -> str:
    codex_bin = _codex_binary()
    codex_cfg = CONFIG.get("codex", {})
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
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=max(30, int(timeout_seconds)),
                                cwd=str(Path(__file__).resolve().parent.parent))
    except FileNotFoundError:
        return _codex_via_openclaw(prompt, timeout_seconds)
    except subprocess.TimeoutExpired:
        raise ProviderError(provider="codex", kind="network", message="Codex request timed out.")

    output_text = ""
    try:
        output_text = Path(output_path).read_text(encoding="utf-8").strip()
    except Exception:
        pass
    finally:
        try:
            os.unlink(output_path)
        except Exception:
            pass

    combined_error = "\n".join(
        part for part in [result.stderr.strip(), result.stdout.strip()] if part).strip()
    if result.returncode != 0:
        kind, retry_after = _classify_codex_error(combined_error)
        raise ProviderError(provider="codex", kind=kind,
                            message=combined_error or "Codex request failed.",
                            retry_after_seconds=retry_after)
    if not output_text:
        raise ProviderError(provider="codex", kind="other",
                            message="Codex returned an empty response.")
    return output_text


def _codex_via_openclaw(prompt: str, timeout_seconds: int) -> str:
    openclaw_bin = PATHS.get("openclaw", str(Path.home() / ".npm-global/bin/openclaw"))
    session_id = f"codex-fallback-{int(time.time() * 1000)}"
    cmd = [openclaw_bin, "agent", "--to", "+15555550123", "--session-id", session_id,
           "--json", "--thinking", "low", "--timeout", str(max(30, int(timeout_seconds))),
           "--message", prompt]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=max(120, int(timeout_seconds) + 90),
                                cwd=str(Path(__file__).resolve().parent.parent))
    except FileNotFoundError:
        raise ProviderError(provider="codex", kind="auth",
                            message=f"OpenClaw CLI not found at '{openclaw_bin}'.")
    except subprocess.TimeoutExpired:
        raise ProviderError(provider="codex", kind="network",
                            message="OpenClaw local Codex fallback timed out.")

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    combined_error = "\n".join(part for part in [stderr, stdout] if part).strip()
    if result.returncode != 0:
        kind, retry_after = _classify_codex_error(combined_error)
        raise ProviderError(provider="codex", kind=kind,
                            message=combined_error or "OpenClaw Codex fallback failed.",
                            retry_after_seconds=retry_after)
    try:
        payload = json.loads(stdout)
        for key in ("reply", "text", "message", "output"):
            if isinstance(payload.get(key), str) and payload.get(key).strip():
                return payload.get(key).strip()
        if isinstance(payload.get("result"), dict):
            for key in ("reply", "text", "message", "output"):
                val = payload["result"].get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    except Exception:
        pass
    if stdout:
        return stdout
    raise ProviderError(provider="codex", kind="other",
                        message="OpenClaw Codex fallback returned an empty response.")


def local_model_request(system_prompt: str, messages: list,
                        tools: list = None, **kwargs):
    """Request to local Ollama model. Returns str (chat-only) or dict (tool calls).

    When tool_capable is enabled in config and tools are provided, uses the
    full Anthropic<->OpenAI conversion layer from tool_format.py so the agent
    loop can handle tool_use responses identically to Anthropic.
    """
    cfg = CONFIG.get("local_model", {})
    if not cfg.get("enabled"):
        raise ProviderError(provider="local", kind="auth", message="Local model not enabled.")

    host = str(cfg.get("host", "http://localhost:11434")).rstrip("/")
    model = str(cfg.get("model", "llama3.2:latest"))
    timeout = _to_int(cfg.get("timeout_seconds")) or 60
    max_tokens = _to_int(cfg.get("max_tokens")) or 1024
    tool_capable = cfg.get("tool_capable", False)

    # ── Tool-capable mode: full message + tool conversion ────────────
    if tool_capable and tools:
        from agent.tool_format import (
            anthropic_tools_to_openai,
            anthropic_messages_to_openai,
            ollama_response_to_anthropic,
        )

        openai_messages = anthropic_messages_to_openai(messages, system_prompt)
        openai_tools = anthropic_tools_to_openai(tools)

        body = json.dumps({
            "model": model,
            "messages": openai_messages,
            "stream": False,
            "tools": openai_tools,
            "options": {"num_predict": max_tokens},
        }).encode("utf-8")

        result = None
        for attempt in range(3):
            req = urllib.request.Request(f"{host}/api/chat", data=body,
                                        headers={"Content-Type": "application/json"})
            try:
                resp = urllib.request.urlopen(req, timeout=timeout)
                result = json.loads(resp.read())
                break
            except urllib.error.URLError:
                if attempt < 2:
                    _append_runtime_log("local_model_retry", {"attempt": attempt + 1, "mode": "tool"})
                    time.sleep(2 ** attempt)
                    continue
                raise ProviderError(provider="local", kind="network",
                                    message="Local model server not reachable. Start with: ollama serve")
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise ProviderError(provider="local", kind="other", message=f"Local model error: {e}")

        if result is None:
            raise ProviderError(provider="local", kind="other", message="Local model returned no result after retries.")

        message = result.get("message", {})
        if message.get("tool_calls"):
            # Tool calls present — return Anthropic-format dict
            return ollama_response_to_anthropic(result)

        # No tool calls — extract text and fall through to string return
        text = str(message.get("content", "")).strip()
        if not text:
            text = str(result.get("response", "")).strip()
        if not text:
            raise ProviderError(provider="local", kind="other",
                                message="Local model returned an empty response.")
        return text

    # ── Chat-only mode: lightweight, last-6-messages ─────────────────
    assistant_name = CONFIG.get("user", {}).get("assistant_name", "Rout")
    user_name = CONFIG.get("user", {}).get("name", "the user")

    local_system = (
        f"You are {assistant_name}, a helpful AI assistant texting with {user_name} "
        f"via iMessage. Be warm, concise, and natural — like texting a friend. "
        f"Give direct answers. Never describe what you're about to say."
    )

    chat_messages = [{"role": "system", "content": local_system}]
    recent = messages[-6:] if len(messages) > 6 else messages
    for msg in recent:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content
                          if isinstance(p, dict) and p.get("type") == "text"]
            content = " ".join(text_parts) or "(image)"
        if role in ("user", "assistant"):
            chat_messages.append({"role": role, "content": content})

    body = json.dumps({
        "model": model, "messages": chat_messages,
        "stream": False, "options": {"num_predict": max_tokens},
    }).encode("utf-8")

    result = None
    for attempt in range(3):
        req = urllib.request.Request(f"{host}/api/chat", data=body,
                                    headers={"Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            result = json.loads(resp.read())
            break
        except urllib.error.URLError:
            if attempt < 2:
                _append_runtime_log("local_model_retry", {"attempt": attempt + 1, "mode": "chat"})
                time.sleep(2 ** attempt)
                continue
            raise ProviderError(provider="local", kind="network",
                                message="Local model server not reachable. Start with: ollama serve")
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise ProviderError(provider="local", kind="other", message=f"Local model error: {e}")

    if result is None:
        raise ProviderError(provider="local", kind="other", message="Local model returned no result after retries.")

    msg_obj = result.get("message", {})
    text = str(msg_obj.get("content", "")).strip() if isinstance(msg_obj, dict) else ""
    if not text:
        text = str(result.get("response", "")).strip()
    if not text:
        raise ProviderError(provider="local", kind="other",
                            message="Local model returned an empty response.")
    return text


# ── Failover Orchestrator ────────────────────────────────────────────────────

def _raise_provider_failure(errors: list, state: dict) -> None:
    now = int(time.time())
    anthropic_remaining = max(0, _effective_provider_cooldown_until(state, "anthropic") - now)
    codex_enabled = _provider_enabled("codex")
    codex_remaining = (
        max(0, _effective_provider_cooldown_until(state, "codex") - now) if codex_enabled else 0
    )
    local_enabled = _provider_enabled("local")

    cooldown_parts = []
    if anthropic_remaining > 0:
        cooldown_parts.append(f"Anthropic retry in {_format_duration(anthropic_remaining)}")
    if codex_enabled and codex_remaining > 0:
        cooldown_parts.append(f"Codex retry in {_format_duration(codex_remaining)}")

    non_cooldown_errors = [err for err in errors if err.kind != "cooldown"]
    local_errors = [err for err in non_cooldown_errors if err.provider == "local"]

    if cooldown_parts and not local_enabled:
        raise RuntimeError("All cloud providers are cooling down. " + "; ".join(cooldown_parts) + ".")
    if cooldown_parts and local_errors:
        local_msg = local_errors[0].message
        raise RuntimeError(
            "Cloud providers cooling down and local model unavailable. "
            + "; ".join(cooldown_parts) + f". Local: {local_msg}")
    if non_cooldown_errors:
        primary = non_cooldown_errors[0]
        if primary.provider == "codex" and anthropic_remaining > 0:
            if primary.kind == "auth":
                raise RuntimeError(
                    "Anthropic is cooling down and Codex OAuth is unavailable. "
                    f"Anthropic retry in {_format_duration(anthropic_remaining)}.")
            raise RuntimeError(
                "Anthropic is cooling down and Codex is temporarily unavailable. "
                f"Anthropic retry in {_format_duration(anthropic_remaining)}.")
        if primary.provider == "anthropic" and primary.kind == "auth":
            raise RuntimeError("Anthropic OAuth auth failed and Codex fallback was unavailable.")
        raise RuntimeError(primary.message)
    if anthropic_remaining > 0:
        raise RuntimeError(f"Anthropic is cooling down. Retry in {_format_duration(anthropic_remaining)}.")
    if codex_enabled and codex_remaining > 0:
        raise RuntimeError(f"Codex is cooling down. Retry in {_format_duration(codex_remaining)}.")
    raise RuntimeError("No model provider was available.")


def request_with_failover(system_prompt: str, messages: list, max_tokens: int,
                          tools: list = None, expect_json: bool = False,
                          notify_fn=None) -> dict:
    """Make a model request with automatic provider failover.

    Returns the full Anthropic response dict when using Anthropic,
    or a synthetic response dict for Codex/local providers.

    When tools are provided, they are passed to the Anthropic API.
    Codex/local fallback does not support tool use.
    """
    state = _load_provider_state()
    previous_provider = str(state.get("active_provider", "")).strip().lower()
    errors: list[ProviderError] = []

    for provider in _provider_attempt_order(state):
        if not _provider_enabled(provider):
            continue

        cooldown_until = _effective_provider_cooldown_until(state, provider)
        now = int(time.time())
        if cooldown_until > now:
            errors.append(ProviderError(
                provider=provider, kind="cooldown",
                message=f"{provider} cooldown active",
                retry_after_seconds=cooldown_until - now))
            _append_runtime_log("provider_skip_cooldown",
                                {"provider": provider, "retry_after_seconds": cooldown_until - now})
            continue

        _append_runtime_log("provider_attempt", {"provider": provider})
        try:
            if provider == "anthropic":
                result = anthropic_request(system_prompt, messages,
                                           max_tokens=max_tokens, tools=tools)
            elif provider == "codex":
                text = codex_request(system_prompt, messages, expect_json=expect_json)
                result = {
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                    "provider": "codex",
                }
            elif provider == "local":
                local_cfg = CONFIG.get("local_model", {})
                tool_capable = local_cfg.get("tool_capable", False)
                response = local_model_request(
                    system_prompt, messages,
                    tools=(tools if tool_capable else None),
                )
                if isinstance(response, dict):
                    # Tool-capable mode returned Anthropic-format dict
                    result = response
                else:
                    result = {
                        "content": [{"type": "text", "text": response}],
                        "stop_reason": "end_turn",
                        "provider": "local",
                    }
            else:
                continue

            _clear_provider_cooldown(state, provider)
            state["active_provider"] = provider
            result["_active_provider"] = provider

            if provider != previous_provider:
                _append_runtime_log("provider_switch",
                                    {"from": previous_provider or "unknown", "to": provider})
                if (provider == "anthropic" and notify_fn is not None
                        and str(CONFIG.get("provider_failover", {}).get(
                            "notify_on_failback", "true")).strip().lower()
                        not in {"0", "false", "no", "off"}):
                    notify_fn("✅ Back on Anthropic OAuth.")

            _save_provider_state(state)
            return result

        except ProviderError as error:
            errors.append(error)
            _record_provider_error(state, error)
            _append_runtime_log("provider_error",
                                {"provider": error.provider, "kind": error.kind})
            continue

    _save_provider_state(state)
    _raise_provider_failure(errors, state)
    return {}


def provider_status_line() -> str:
    state = _load_provider_state()
    now = int(time.time())
    anthropic_until = _effective_provider_cooldown_until(state, "anthropic")
    remaining = max(0, anthropic_until - now)
    if remaining > 0:
        src = "heuristic"
        try:
            raw = _load_json_file(PROVIDER_STATUS_PATH)
            if str(raw.get("provider", "")).strip().lower() == "anthropic":
                src = str(raw.get("source", "heuristic")).strip() or "heuristic"
        except Exception:
            pass
        return f"Anthropic cooldown, expected clear in ~{_format_duration(remaining)} (source: {src})."
    active = str(state.get("active_provider", "anthropic")).strip().lower() or "anthropic"
    if active == "anthropic":
        return "Anthropic is available now."
    return f"Anthropic appears available now; active provider is {active}."


def _get_api_key() -> Optional[str]:
    """Get the current Anthropic API key. Used by watcher for startup diagnostics."""
    info = _get_anthropic_profile_info()
    return info.get("token") or None
