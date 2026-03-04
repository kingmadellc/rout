"""
Rout Test Fixtures — shared mocks and test infrastructure.
============================================================
Provides pytest fixtures for all external dependencies:
  - Config loading (no real filesystem access)
  - Kalshi API client (mocked responses)
  - Polymarket API (mocked HTTP)
  - BlueBubbles (mocked Socket.IO + REST)
  - iMessage sending (mocked subprocess)
  - File system state (temp dirs)

Usage:
    def test_portfolio(mock_kalshi_client):
        result = portfolio_command()
        assert "P&L" in result
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add workspace root so tests can import project modules
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
# Add scripts/ so `from proactive import ...` resolves (mirrors CLI behavior)
sys.path.insert(0, str(_REPO_ROOT / "scripts"))


# ── Test Config ───────────────────────────────────────────────────────────────

SAMPLE_CONFIG = {
    "anthropic_api_key": "test-key-fake-000",
    "kalshi": {
        "enabled": True,
        "api_key_id": "test-kalshi-key",
        "private_key_file": "test_kalshi.pem",
        "ticker_names": {"KXTEST-27": "Test Market"},
    },
    "polymarket": {
        "enabled": True,
        "watchlist": ["test-market-slug"],
    },
    "bluebubbles": {
        "enabled": True,
        "server_url": "http://localhost:1234",
        "password": "test-bb-password",
        "send_method": "private-api",
        "chat_map": {
            "1": "iMessage;-;+15551234567",
            "2": "iMessage;-;chat123456",
        },
    },
    "chats": {
        "personal_id": 1,
    },
    "known_senders": {
        "+15559876543": "Test User",
    },
    "paths": {
        "imsg": "/usr/local/bin/imsg",
    },
}

SAMPLE_PROACTIVE_TRIGGERS = {
    "proactive": {
        "enabled": True,
        "max_messages_per_hour": 3,
        "morning_briefing": {"enabled": True, "time": "08:00"},
    },
    "webhooks": {
        "enabled": True,
        "port": 7888,
        "secret": "test-webhook-secret-000",
        "rate_limit": 5,
        "triggers": {
            "test-alert": {
                "template": "Test: {message}",
                "chat_id": 1,
            },
        },
    },
}


@pytest.fixture
def sample_config():
    """Return a copy of the sample config dict."""
    return SAMPLE_CONFIG.copy()


@pytest.fixture
def sample_triggers():
    """Return a copy of the sample proactive triggers config."""
    return SAMPLE_PROACTIVE_TRIGGERS.copy()


@pytest.fixture
def temp_config_dir(tmp_path):
    """Create a temporary config directory with sample configs."""
    config_dir = tmp_path / ".openclaw"
    config_dir.mkdir()

    # Write main config
    config_file = config_dir / "config.yaml"
    import yaml
    config_file.write_text(yaml.dump(SAMPLE_CONFIG))
    config_file.chmod(0o600)

    # Write keys dir
    keys_dir = config_dir / "keys"
    keys_dir.mkdir()
    keys_dir.chmod(0o700)

    # Write a fake key file (content is intentionally not a real PEM structure
    # to avoid pre-commit detect-private-key hook false positives)
    fake_key = keys_dir / "test_kalshi.pem"
    fake_key.write_text("FAKE_TEST_KEY_FOR_CI\nnot-a-real-private-key\n")
    fake_key.chmod(0o600)

    # Write logs dir
    (config_dir / "logs").mkdir()
    (config_dir / "state").mkdir()

    return config_dir


# ── Mock Kalshi Client ────────────────────────────────────────────────────────

@pytest.fixture
def mock_kalshi_client():
    """Mock Kalshi API client with realistic responses."""
    client = MagicMock()

    # Balance
    balance_resp = MagicMock()
    balance_resp.balance = 5000  # $50.00 in cents
    client._portfolio_api.get_balance.return_value = balance_resp

    # Positions (raw API response)
    positions_resp = MagicMock()
    positions_resp.read.return_value = json.dumps({
        "market_positions": [
            {
                "ticker": "KXTEST-27",
                "position": 10,
                "market_exposure_dollars": 5.50,
            },
        ],
    }).encode()
    client._portfolio_api.get_positions_without_preload_content.return_value = positions_resp

    # Market data
    def mock_call_api(method, url, body=None):
        resp = MagicMock()
        if "/markets/" in url and method == "GET":
            resp.read.return_value = json.dumps({
                "market": {
                    "title": "Test Market",
                    "yes_bid": 55,
                    "yes_ask": 58,
                    "no_bid": 42,
                    "no_ask": 45,
                    "last_price": 56,
                    "volume_24h": 1000,
                    "volume": 50000,
                    "status": "open",
                    "close_time": "2027-01-01T00:00:00Z",
                },
            }).encode()
        elif "/portfolio/orders" in url and method == "POST":
            resp.read.return_value = json.dumps({
                "order": {
                    "order_id": "test-order-123",
                    "status": "resting",
                },
            }).encode()
        elif "/portfolio/orders" in url and method == "GET":
            resp.read.return_value = json.dumps({
                "orders": [],
            }).encode()
        elif "/portfolio/orders/" in url and method == "DELETE":
            resp.read.return_value = json.dumps({
                "order": {"status": "cancelled"},
            }).encode()
        else:
            resp.read.return_value = b"{}"
        return resp

    client.call_api = mock_call_api
    return client


# ── Mock HTTP Responses ───────────────────────────────────────────────────────

@pytest.fixture
def mock_gamma_api():
    """Mock Polymarket Gamma API responses."""
    sample_markets = [
        {
            "question": "Will Bitcoin hit $100K by 2027?",
            "slug": "bitcoin-100k-2027",
            "outcomePrices": "[0.65, 0.35]",
            "outcomes": '["Yes", "No"]',
            "volumeNum": 1500000,
            "endDate": "2027-01-01T00:00:00Z",
            "active": True,
        },
        {
            "question": "Will there be a US recession in 2026?",
            "slug": "us-recession-2026",
            "outcomePrices": "[0.25, 0.75]",
            "outcomes": '["Yes", "No"]',
            "volumeNum": 800000,
            "endDate": "2026-12-31T00:00:00Z",
            "active": True,
        },
    ]

    def mock_urlopen(req, timeout=10):
        resp = MagicMock()
        resp.read.return_value = json.dumps(sample_markets).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        yield sample_markets


# ── Mock BlueBubbles ──────────────────────────────────────────────────────────

@pytest.fixture
def mock_bb_send():
    """Mock BlueBubbles REST API send."""
    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        yield mock_post


@pytest.fixture
def mock_bb_get():
    """Mock BlueBubbles REST API GET."""
    with patch("requests.get") as mock_get:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": {"os_version": "14.0", "server_version": "1.9.0"}},
        )
        yield mock_get


# ── Mock Subprocess (iMessage CLI) ────────────────────────────────────────────

@pytest.fixture
def mock_imsg_send():
    """Mock imsg CLI send."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        yield mock_run


# ── Utility Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def temp_state_dir(tmp_path):
    """Create a temporary state directory for proactive agent."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def temp_log_dir(tmp_path):
    """Create a temporary log directory."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return log_dir
