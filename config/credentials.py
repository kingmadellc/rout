"""
Rout Credential Manager
========================
Loads credentials from ~/.openclaw/config.yaml.
All scripts import from here — zero hardcoded secrets.

Usage:
    from config.credentials import get_kalshi_config, get_claude_config
"""

import yaml
import sys
from pathlib import Path
from functools import lru_cache

CONFIG_DIR = Path.home() / ".openclaw"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
KEYS_DIR = CONFIG_DIR / "keys"


class CredentialError(Exception):
    """Raised when credentials are missing or misconfigured."""
    pass


def _check_file_permissions(filepath: Path, max_mode: int = 0o600) -> None:
    """Verify file permissions are restrictive enough."""
    if not filepath.exists():
        raise CredentialError(f"File not found: {filepath}")
    current_mode = filepath.stat().st_mode & 0o777
    if current_mode & ~max_mode:
        raise CredentialError(
            f"Insecure permissions on {filepath}: {oct(current_mode)}. "
            f"Fix with: chmod {oct(max_mode)[2:]} {filepath}"
        )


@lru_cache(maxsize=1)
def _load_config() -> dict:
    """Load config.yaml with permission check. Cached after first load."""
    _check_file_permissions(CONFIG_FILE, 0o600)
    with open(CONFIG_FILE, "r") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict):
        raise CredentialError(f"Invalid config format in {CONFIG_FILE}")
    return config


def _load_private_key(key_name: str) -> str:
    """Load a private key from ~/.openclaw/keys/."""
    key_path = KEYS_DIR / key_name
    _check_file_permissions(key_path, 0o600)
    with open(key_path, "r") as f:
        return f.read().strip()


def get_kalshi_config() -> dict:
    """Returns Kalshi API configuration."""
    config = _load_config()
    kalshi = config.get("kalshi")
    if not kalshi:
        raise CredentialError("Missing 'kalshi' section in config.yaml")

    for field in ["api_key_id", "private_key_file"]:
        if field not in kalshi:
            raise CredentialError(f"Missing 'kalshi.{field}' in config.yaml")

    private_key = _load_private_key(kalshi["private_key_file"])

    return {
        "api_key_id": kalshi["api_key_id"],
        "private_key_pem": private_key,
        "base_url": kalshi.get("base_url", "https://api.elections.kalshi.com"),
        "environment": kalshi.get("environment", "production"),
    }


def get_claude_config() -> dict:
    """Returns Claude/Anthropic API configuration."""
    config = _load_config()
    api_key = config.get("anthropic_api_key", "")
    if not api_key:
        raise CredentialError("Missing 'anthropic_api_key' in config.yaml")
    return {
        "api_key": api_key,
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
    }


def validate_environment() -> list:
    """Run environment checks. Returns list of warnings."""
    warnings = []

    if not CONFIG_DIR.exists():
        raise CredentialError(f"Config directory not found: {CONFIG_DIR}")
    if not CONFIG_FILE.exists():
        raise CredentialError(f"Config file not found: {CONFIG_FILE}")

    try:
        _check_file_permissions(CONFIG_FILE, 0o600)
    except CredentialError as e:
        warnings.append(str(e))

    if KEYS_DIR.exists():
        try:
            current_mode = KEYS_DIR.stat().st_mode & 0o777
            if current_mode & ~0o700:
                warnings.append(
                    f"Insecure permissions on {KEYS_DIR}: {oct(current_mode)}. "
                    f"Fix with: chmod 700 {KEYS_DIR}"
                )
        except Exception as e:
            warnings.append(str(e))

    return warnings


if __name__ == "__main__":
    print("Rout — Environment Check")
    print("=" * 40)
    try:
        warnings = validate_environment()
        if warnings:
            for w in warnings:
                print(f"  WARNING: {w}")
        else:
            print("  All checks passed.")

        for name, loader in [("Kalshi", get_kalshi_config), ("Claude", get_claude_config)]:
            try:
                loader()
                print(f"  {name}: OK")
            except CredentialError as e:
                print(f"  {name}: {e}")
    except CredentialError as e:
        print(f"  CRITICAL: {e}")
        sys.exit(1)
