"""
Local-only provider for Rout — Qwen via Ollama with full tool calling.

This replaces the cloud-dependent providers.py when running in local-only mode.
No Anthropic API, no Codex, no network dependency for inference.

Architecture:
  - Ollama running locally with Qwen 3.5 model
  - Full tool calling support (Qwen 3.5 native function calling)
  - Anthropic-compatible response format (so agent_loop.py works unchanged)
  - Token tracking for local usage monitoring

Usage:
  Set `local_only: true` in config.yaml and this module takes over.
"""

import json
import time
import urllib.error
import urllib.request
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.tool_format import (
    anthropic_tools_to_openai,
    anthropic_messages_to_openai,
    ollama_response_to_anthropic,
)


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    for candidate in [
        Path(__file__).parent.parent / "config.yaml",
        Path.home() / ".config/imsg-watcher/config.yaml",
    ]:
        if candidate.exists():
            with open(candidate) as f:
                return yaml.safe_load(f) or {}
    return {}


CONFIG = _load_config()
TOKEN_USAGE_LOG_PATH = Path.home() / ".openclaw" / "workspace" / "token_usage.jsonl"
WATCHER_LOG_PATH = Path.home() / ".openclaw" / "workspace" / "imsg_watcher.log"


def is_local_only() -> bool:
    """Check if we're configured for local-only mode."""
    return bool(CONFIG.get("local_only", False))


def _get_local_config() -> dict:
    """Get local model configuration with defaults."""
    cfg = CONFIG.get("local_model", {})
    return {
        "host": str(cfg.get("host", "http://localhost:11434")).rstrip("/"),
        "model": str(cfg.get("model", "qwen3.5:27b")),
        "timeout": int(cfg.get("timeout_seconds", 120)),
        "max_tokens": int(cfg.get("max_tokens", 4096)),
        "temperature": float(cfg.get("temperature", 0.7)),
        "context_length": int(cfg.get("context_length", 32768)),
        "gpu_memory_cap_mb": int(cfg.get("gpu_memory_cap_mb", 24576)),
    }


# ── Logging ──────────────────────────────────────────────────────────────────

def _track_local_usage(model: str, prompt_eval_count: int = 0,
                       eval_count: int = 0, total_duration_ns: int = 0) -> None:
    """Track token usage for local model inference."""
    try:
        TOKEN_USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": "local",
            "model": model,
            "input_tokens": prompt_eval_count,
            "output_tokens": eval_count,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "total_tokens": prompt_eval_count + eval_count,
            "duration_ms": total_duration_ns // 1_000_000 if total_duration_ns else 0,
            "tokens_per_second": round(eval_count / (total_duration_ns / 1e9), 1) if total_duration_ns else 0,
        }
        with open(TOKEN_USAGE_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _log(event: str, details: Optional[dict] = None) -> None:
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "details": details or {},
        }
        with open(WATCHER_LOG_PATH, "a") as f:
            f.write(f"[local_provider] {json.dumps(record, ensure_ascii=True)}\n")
    except Exception:
        pass


# ── Ollama Health Check ──────────────────────────────────────────────────────

def check_ollama_health() -> tuple[bool, str]:
    """Check if Ollama is running and the model is loaded."""
    cfg = _get_local_config()
    host = cfg["host"]
    model = cfg["model"]

    # Check Ollama is running
    try:
        req = urllib.request.Request(f"{host}/api/tags")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
    except Exception:
        return False, f"Ollama not reachable at {host}. Run: ollama serve"

    # Check model is available
    models = [m.get("name", "") for m in data.get("models", [])]
    # Ollama model names can have :latest suffix
    model_base = model.split(":")[0]
    found = any(model_base in m for m in models)
    if not found:
        return False, f"Model '{model}' not found. Run: ollama pull {model}"

    return True, "OK"


# ── Core Request ─────────────────────────────────────────────────────────────

def local_request_with_tools(system_prompt: str, messages: list,
                              max_tokens: int, tools: list = None) -> dict:
    """Make a request to Ollama with full tool calling support.

    Converts:
    - Anthropic tool definitions -> OpenAI format
    - Anthropic message history -> OpenAI chat format
    - Ollama response -> Anthropic response format

    Returns an Anthropic-compatible response dict that agent_loop.py
    can consume without modification.
    """
    cfg = _get_local_config()
    host = cfg["host"]
    model = cfg["model"]
    timeout = cfg["timeout"]
    temperature = cfg["temperature"]
    context_length = cfg["context_length"]

    # Convert message format
    openai_messages = anthropic_messages_to_openai(messages, system_prompt)

    # Build request body
    body = {
        "model": model,
        "messages": openai_messages,
        "stream": False,
        "options": {
            "num_predict": max_tokens,
            "temperature": temperature,
            "num_ctx": context_length,
        },
    }

    # Add tools if provided (Ollama native tool calling)
    if tools:
        openai_tools = anthropic_tools_to_openai(tools)
        body["tools"] = openai_tools

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
    )

    _log("local_request", {
        "model": model,
        "message_count": len(openai_messages),
        "has_tools": bool(tools),
        "tool_count": len(tools) if tools else 0,
    })

    start_time = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        result = json.loads(resp.read())
    except urllib.error.URLError as e:
        _log("local_error", {"error": str(e)})
        raise RuntimeError(
            f"Local model not reachable at {host}. "
            "Start Ollama with: ollama serve"
        )
    except Exception as e:
        _log("local_error", {"error": str(e)})
        raise RuntimeError(f"Local model error: {e}")

    elapsed = time.time() - start_time
    _log("local_response", {
        "model": model,
        "elapsed_s": round(elapsed, 2),
        "has_tool_calls": bool(result.get("message", {}).get("tool_calls")),
    })

    # Track usage
    _track_local_usage(
        model=model,
        prompt_eval_count=result.get("prompt_eval_count", 0),
        eval_count=result.get("eval_count", 0),
        total_duration_ns=result.get("total_duration", 0),
    )

    # Convert to Anthropic format
    anthropic_response = ollama_response_to_anthropic(result)
    anthropic_response["_active_provider"] = "local"
    anthropic_response["_model"] = model
    anthropic_response["_elapsed_s"] = round(elapsed, 2)

    return anthropic_response


# ── Failover-Compatible Interface ────────────────────────────────────────────

def request_with_failover(system_prompt: str, messages: list, max_tokens: int,
                          tools: list = None, expect_json: bool = False,
                          notify_fn=None) -> dict:
    """Drop-in replacement for providers.request_with_failover() in local-only mode.

    Single provider (Ollama), no failover chain — if it's down, it's down.
    This is the price of sovereignty.
    """
    return local_request_with_tools(
        system_prompt=system_prompt,
        messages=messages,
        max_tokens=max_tokens,
        tools=tools,
    )


def provider_status_line() -> str:
    """Status line for local-only mode."""
    cfg = _get_local_config()
    healthy, msg = check_ollama_health()
    if healthy:
        return f"Local mode: {cfg['model']} via Ollama — operational."
    return f"Local mode: {cfg['model']} — {msg}"
