"""Integration tests for agent/providers.py — provider failover engine.

Tests the critical path: Anthropic rate limit → Codex fallback → cooldown
persistence → automatic failback to Anthropic when cooldown expires.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.providers import (
    _default_provider_state,
    _load_provider_state,
    _save_provider_state,
    _provider_cooldown_until,
    _set_provider_cooldown,
    _clear_provider_cooldown,
    _provider_attempt_order,
    _provider_enabled,
    _parse_retry_after_seconds,
    _parse_retry_hint_seconds,
    _record_provider_error,
    _classify_codex_error,
    _format_duration,
    local_model_request,
    ProviderError,
    request_with_failover,
)


# ── State Management Tests ──────────────────────────────────────────────────


def test_default_state_structure():
    """Default state has all three providers with zero cooldown."""
    state = _default_provider_state()
    assert state["active_provider"] == "anthropic"
    assert "anthropic" in state["providers"]
    assert "codex" in state["providers"]
    assert "local" in state["providers"]
    for provider in ("anthropic", "codex", "local"):
        assert state["providers"][provider]["cooldown_until"] == 0


def test_set_and_read_cooldown():
    """Setting a cooldown is readable back."""
    state = _default_provider_state()
    future = int(time.time()) + 300
    _set_provider_cooldown(state, "anthropic", future, reason="rate limited")
    assert _provider_cooldown_until(state, "anthropic") == future
    assert state["providers"]["anthropic"]["reason"] == "rate limited"


def test_clear_cooldown():
    """Clearing a cooldown resets to zero."""
    state = _default_provider_state()
    _set_provider_cooldown(state, "anthropic", int(time.time()) + 300)
    _clear_provider_cooldown(state, "anthropic")
    assert _provider_cooldown_until(state, "anthropic") == 0
    assert state["providers"]["anthropic"]["reason"] == ""


def test_cooldown_capped_at_max():
    """Cooldown can't exceed MAX_COOLDOWN_SECONDS (2 hours)."""
    state = _default_provider_state()
    absurd_future = int(time.time()) + 999999
    _set_provider_cooldown(state, "anthropic", absurd_future)
    cooldown = _provider_cooldown_until(state, "anthropic")
    # Should be capped at now + 7200 (2 hours)
    assert cooldown <= int(time.time()) + 7200 + 1


def test_state_persistence(tmp_path):
    """State survives save/load cycle."""
    state_file = tmp_path / "provider_failover.json"
    state = _default_provider_state()
    future = int(time.time()) + 120
    _set_provider_cooldown(state, "codex", future, reason="429")

    with patch("agent.providers.PROVIDER_STATE_PATH", state_file):
        _save_provider_state(state)
        assert state_file.exists()

        loaded = json.loads(state_file.read_text())
        assert loaded["providers"]["codex"]["cooldown_until"] == future
        assert loaded["providers"]["codex"]["reason"] == "429"


# ── Attempt Order Tests ─────────────────────────────────────────────────────


def test_attempt_order_all_clear():
    """When nothing is on cooldown, Anthropic is first."""
    state = _default_provider_state()
    with patch("agent.providers._provider_enabled", side_effect=lambda p: p in ("anthropic", "codex")):
        with patch("agent.providers._effective_provider_cooldown_until", return_value=0):
            order = _provider_attempt_order(state)
    assert order[0] == "anthropic"


def test_attempt_order_anthropic_on_cooldown():
    """When Anthropic is on cooldown, Codex goes first."""
    state = _default_provider_state()
    future = int(time.time()) + 300

    def mock_cooldown(state, provider):
        return future if provider == "anthropic" else 0

    with patch("agent.providers._provider_enabled", side_effect=lambda p: p in ("anthropic", "codex")):
        with patch("agent.providers._effective_provider_cooldown_until", side_effect=mock_cooldown):
            order = _provider_attempt_order(state)
    assert order[0] == "codex"
    assert "anthropic" in order  # Still in list for retry


def test_attempt_order_all_on_cooldown_local_fallback():
    """When cloud providers are on cooldown and local is enabled, local goes first."""
    state = _default_provider_state()
    future = int(time.time()) + 300

    def mock_cooldown(state, provider):
        if provider in ("anthropic", "codex"):
            return future
        return 0

    with patch("agent.providers._provider_enabled", return_value=True):
        with patch("agent.providers._effective_provider_cooldown_until", side_effect=mock_cooldown):
            order = _provider_attempt_order(state)
    assert order[0] == "local"


# ── Retry-After Parsing Tests ───────────────────────────────────────────────


def test_parse_retry_after_header_seconds():
    """Numeric Retry-After header is parsed correctly."""
    headers = {"retry-after": "45"}
    seconds, source = _parse_retry_after_seconds(headers, "", default_seconds=60)
    assert seconds == 45
    assert source == "retry-after"


def test_parse_retry_after_falls_back_to_default():
    """When no header present, falls back to default."""
    seconds, source = _parse_retry_after_seconds({}, "", default_seconds=60)
    assert seconds == 60
    assert source == "heuristic"


def test_parse_retry_hint_from_error_text():
    """Retry hint parsed from error body text."""
    result = _parse_retry_hint_seconds("Please retry after 120 seconds")
    assert result == 120


def test_parse_retry_hint_minutes():
    """Retry hint with minutes unit."""
    result = _parse_retry_hint_seconds("try again in 5 minutes")
    assert result == 300


def test_parse_retry_hint_no_match():
    """No retry hint in unrelated text."""
    result = _parse_retry_hint_seconds("Something went wrong")
    assert result is None


# ── Error Classification Tests ──────────────────────────────────────────────


def test_classify_codex_rate_limit():
    """Rate limit errors are classified as cooldown."""
    kind, _ = _classify_codex_error("Error: rate limit exceeded (429)")
    assert kind == "cooldown"


def test_classify_codex_auth_error():
    """Auth errors are classified correctly."""
    kind, _ = _classify_codex_error("unauthorized: invalid token")
    assert kind == "auth"


def test_classify_codex_timeout():
    """Timeout errors are classified as network."""
    kind, _ = _classify_codex_error("request timed out after 120s")
    assert kind == "network"


def test_classify_codex_unknown():
    """Unknown errors get 'other' classification."""
    kind, _ = _classify_codex_error("something unexpected happened")
    assert kind == "other"


# ── Provider Error Recording ────────────────────────────────────────────────


def test_record_cooldown_error():
    """Cooldown errors update provider state."""
    state = _default_provider_state()
    error = ProviderError(
        provider="anthropic", kind="cooldown",
        message="rate limited", retry_after_seconds=60,
    )
    with patch("agent.providers._write_provider_status"):
        _record_provider_error(state, error)
    cooldown = _provider_cooldown_until(state, "anthropic")
    assert cooldown > int(time.time())


def test_record_non_cooldown_error_is_noop():
    """Non-cooldown errors don't modify state."""
    state = _default_provider_state()
    error = ProviderError(
        provider="anthropic", kind="auth", message="bad token",
    )
    _record_provider_error(state, error)
    assert _provider_cooldown_until(state, "anthropic") == 0


# ── Failover Orchestrator Tests ─────────────────────────────────────────────


def test_failover_anthropic_success():
    """When Anthropic works, returns Anthropic result."""
    mock_result = {
        "content": [{"type": "text", "text": "Hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    with patch("agent.providers._load_provider_state", return_value=_default_provider_state()):
        with patch("agent.providers._save_provider_state"):
            with patch("agent.providers.anthropic_request", return_value=mock_result):
                with patch("agent.providers._append_runtime_log"):
                    result = request_with_failover(
                        system_prompt="test", messages=[{"role": "user", "content": "hi"}],
                        max_tokens=100,
                    )
    assert result["content"][0]["text"] == "Hello"
    assert result.get("_active_provider") == "anthropic"


def test_failover_anthropic_429_falls_to_codex():
    """When Anthropic returns 429, automatically tries Codex."""
    anthropic_error = ProviderError(
        provider="anthropic", kind="cooldown",
        message="rate limited", retry_after_seconds=60,
    )

    with patch("agent.providers._load_provider_state", return_value=_default_provider_state()):
        with patch("agent.providers._save_provider_state"):
            with patch("agent.providers.anthropic_request", side_effect=anthropic_error):
                with patch("agent.providers.codex_request", return_value="Codex response"):
                    with patch("agent.providers._append_runtime_log"):
                        with patch("agent.providers._write_provider_status"):
                            result = request_with_failover(
                                system_prompt="test",
                                messages=[{"role": "user", "content": "hi"}],
                                max_tokens=100,
                            )
    assert result["content"][0]["text"] == "Codex response"
    assert result.get("_active_provider") == "codex"
    assert result.get("provider") == "codex"


def test_failover_all_providers_down_raises():
    """When all providers fail, raises RuntimeError."""
    anthropic_error = ProviderError(
        provider="anthropic", kind="cooldown",
        message="rate limited", retry_after_seconds=60,
    )
    codex_error = ProviderError(
        provider="codex", kind="cooldown",
        message="rate limited", retry_after_seconds=120,
    )

    with patch("agent.providers._load_provider_state", return_value=_default_provider_state()):
        with patch("agent.providers._save_provider_state"):
            with patch("agent.providers.anthropic_request", side_effect=anthropic_error):
                with patch("agent.providers.codex_request", side_effect=codex_error):
                    with patch("agent.providers._append_runtime_log"):
                        with patch("agent.providers._write_provider_status"):
                            try:
                                request_with_failover(
                                    system_prompt="test",
                                    messages=[{"role": "user", "content": "hi"}],
                                    max_tokens=100,
                                )
                                assert False, "Should have raised RuntimeError"
                            except RuntimeError as e:
                                assert "cooling down" in str(e).lower() or "unavailable" in str(e).lower()


def test_failover_notifies_on_failback():
    """When switching back to Anthropic from Codex, notify_fn is called."""
    mock_result = {
        "content": [{"type": "text", "text": "Back on Anthropic"}],
        "stop_reason": "end_turn",
        "usage": {},
    }
    # Simulate state where Codex was the previous active provider
    state = _default_provider_state()
    state["active_provider"] = "codex"
    notify_calls = []

    with patch("agent.providers._load_provider_state", return_value=state):
        with patch("agent.providers._save_provider_state"):
            with patch("agent.providers.anthropic_request", return_value=mock_result):
                with patch("agent.providers._append_runtime_log"):
                    with patch("agent.providers.CONFIG", {"provider_failover": {"notify_on_failback": "true"}}):
                        result = request_with_failover(
                            system_prompt="test",
                            messages=[{"role": "user", "content": "hi"}],
                            max_tokens=100,
                            notify_fn=lambda msg: notify_calls.append(msg),
                        )
    assert result.get("_active_provider") == "anthropic"
    assert len(notify_calls) == 1
    assert "Anthropic" in notify_calls[0]


# ── Utility Tests ───────────────────────────────────────────────────────────


def test_format_duration_seconds():
    assert _format_duration(45) == "45s"


def test_format_duration_minutes():
    assert _format_duration(125) == "2m 5s"


def test_format_duration_hours():
    assert _format_duration(3725) == "1h 2m"


def test_format_duration_zero():
    assert _format_duration(0) == "0s"


# ── Tool-Capable Local Provider Tests ──────────────────────────────────────


_SAMPLE_TOOLS = [
    {"name": "web_search", "description": "Search the web", "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }}
]


def test_local_tool_capable_passes_tools():
    """When tool_capable=true and tools provided, Ollama gets tool definitions."""
    ollama_response = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "function": {
                    "name": "web_search",
                    "arguments": {"query": "Kalshi shutdown odds"}
                }
            }]
        }
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(ollama_response).encode()

    cfg = {
        "local_model": {
            "enabled": True,
            "host": "http://localhost:11434",
            "model": "qwen3.5:27b",
            "timeout_seconds": 60,
            "max_tokens": 1024,
            "tool_capable": True,
        }
    }

    with patch("agent.providers.CONFIG", cfg):
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = local_model_request(
                "system prompt",
                [{"role": "user", "content": "What are the shutdown odds?"}],
                tools=_SAMPLE_TOOLS,
            )

    # Should return Anthropic-format dict with tool_use
    assert isinstance(result, dict)
    assert result["stop_reason"] == "tool_use"
    tool_block = [b for b in result["content"] if b["type"] == "tool_use"]
    assert len(tool_block) == 1
    assert tool_block[0]["name"] == "web_search"
    assert tool_block[0]["input"]["query"] == "Kalshi shutdown odds"


def test_local_tool_capable_false_no_tools():
    """When tool_capable=false, tools are NOT sent even if provided."""
    ollama_response = {
        "message": {"role": "assistant", "content": "I can't check that right now."}
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(ollama_response).encode()

    cfg = {
        "local_model": {
            "enabled": True,
            "host": "http://localhost:11434",
            "model": "qwen3.5:27b",
            "timeout_seconds": 60,
            "max_tokens": 1024,
            "tool_capable": False,
        },
        "user": {"assistant_name": "Rout", "name": "Matt"},
    }

    with patch("agent.providers.CONFIG", cfg):
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = local_model_request(
                "system prompt",
                [{"role": "user", "content": "What are the shutdown odds?"}],
                tools=_SAMPLE_TOOLS,
            )

    # Should return plain string (chat-only mode)
    assert isinstance(result, str)
    assert "can't check" in result


def test_local_tool_capable_text_only_response():
    """Tool-capable mode: when Qwen returns text without tool calls, returns string."""
    ollama_response = {
        "message": {"role": "assistant", "content": "Sure, let me tell you about that."}
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(ollama_response).encode()

    cfg = {
        "local_model": {
            "enabled": True,
            "host": "http://localhost:11434",
            "model": "qwen3.5:27b",
            "timeout_seconds": 60,
            "max_tokens": 1024,
            "tool_capable": True,
        }
    }

    with patch("agent.providers.CONFIG", cfg):
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = local_model_request(
                "system prompt",
                [{"role": "user", "content": "Tell me a joke"}],
                tools=_SAMPLE_TOOLS,
            )

    # No tool calls = returns text string
    assert isinstance(result, str)
    assert "tell you" in result


def test_failover_local_tool_capable_dict_passthrough():
    """request_with_failover passes tool-capable dict response through directly."""
    ollama_response = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "function": {
                    "name": "web_search",
                    "arguments": {"query": "test"}
                }
            }]
        }
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(ollama_response).encode()

    # All cloud providers on cooldown, local is the only option
    state = _default_provider_state()
    future = int(time.time()) + 300

    cfg = {
        "local_model": {
            "enabled": True,
            "host": "http://localhost:11434",
            "model": "qwen3.5:27b",
            "timeout_seconds": 60,
            "max_tokens": 1024,
            "tool_capable": True,
        }
    }

    def mock_cooldown(s, p):
        return future if p in ("anthropic", "codex") else 0

    with patch("agent.providers._load_provider_state", return_value=state):
        with patch("agent.providers._save_provider_state"):
            with patch("agent.providers._effective_provider_cooldown_until", side_effect=mock_cooldown):
                with patch("agent.providers._provider_enabled", return_value=True):
                    with patch("agent.providers._append_runtime_log"):
                        with patch("agent.providers._write_provider_status"):
                            with patch("agent.providers.CONFIG", cfg):
                                with patch("urllib.request.urlopen", return_value=mock_resp):
                                    result = request_with_failover(
                                        system_prompt="test",
                                        messages=[{"role": "user", "content": "test"}],
                                        max_tokens=100,
                                        tools=_SAMPLE_TOOLS,
                                    )

    assert result["stop_reason"] == "tool_use"
    assert result.get("_active_provider") == "local"
    tool_blocks = [b for b in result["content"] if b["type"] == "tool_use"]
    assert len(tool_blocks) == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✅ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
