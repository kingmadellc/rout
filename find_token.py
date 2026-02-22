#!/usr/bin/env python3
"""Find and configure the Anthropic API/OAuth token automatically.

Searches all known locations:
  1. Environment variables
  2. openclaw CLI commands
  3. Config files (YAML, JSON, .env)
  4. Grep across ~/.openclaw (text files only)

Rejects placeholder tokens (sk-ant-XXXXXXXXX).
Saves found token to ~/.openclaw/config.yaml under claude.api_key.
"""

import json
import os
import subprocess
import sys
import yaml
from pathlib import Path

OPENCLAW_DIR = Path.home() / ".openclaw"
CONFIG_FILE = OPENCLAW_DIR / "config.yaml"

SEARCH_LOCATIONS = [
    # Credential files
    OPENCLAW_DIR / ".env",
    OPENCLAW_DIR / "openclaw.json",
    OPENCLAW_DIR / "agent" / "auth-profiles.json",
    OPENCLAW_DIR / "credentials" / "oauth.json",
    OPENCLAW_DIR / "credentials" / "anthropic.json",
    # Config files
    OPENCLAW_DIR / "config.yaml",
    OPENCLAW_DIR / "workspace" / "config.yaml",
    OPENCLAW_DIR / "backup-20260220" / "workspace" / "config.yaml",
]

ENV_VAR_NAMES = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_API_KEY",
]


def _is_real_token(val):
    """Check if a string is a real Anthropic token (not a placeholder)."""
    if not val or not isinstance(val, str):
        return False
    val = val.strip()
    if not val.startswith("sk-"):
        return False
    if "XXXX" in val or "xxxx" in val:
        return False
    if len(val) < 30:
        return False
    return True


def find_token():
    """Search everywhere for an Anthropic token."""

    # 1. Environment variables
    for var in ENV_VAR_NAMES:
        val = os.environ.get(var, "")
        if _is_real_token(val):
            print(f"Found token in env var {var}")
            return val

    # 2. Try openclaw CLI
    for cmd in [
        ["openclaw", "config", "get", "providers.anthropic.apiKey"],
        ["openclaw", "auth", "status"],
        ["openclaw", "config", "show"],
        ["openclaw", "models", "auth", "paste-token", "--provider", "anthropic"],
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            for line in result.stdout.split("\n"):
                for word in line.split():
                    word = word.strip("'\",:=")
                    if _is_real_token(word):
                        print(f"Found token via: {' '.join(cmd)}")
                        return word
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    # 3. Search credential files
    for path in SEARCH_LOCATIONS:
        if not path.exists():
            continue
        try:
            content = path.read_text()

            # JSON files
            if path.suffix == ".json":
                data = json.loads(content)
                token = _extract_from_dict(data)
                if token:
                    print(f"Found token in {path}")
                    return token

            # YAML files
            if path.suffix in (".yaml", ".yml"):
                data = yaml.safe_load(content) or {}
                token = _extract_from_dict(data)
                if token:
                    print(f"Found token in {path}")
                    return token

            # .env files
            if path.suffix == ".env" or path.name == ".env":
                for line in content.split("\n"):
                    if "sk-ant-" in line:
                        val = line.split("=", 1)[-1].strip().strip("'\"")
                        if _is_real_token(val):
                            print(f"Found token in {path}")
                            return val

            # Raw scan for sk-ant- pattern
            for line in content.split("\n"):
                for word in line.split():
                    word = word.strip("'\",:=")
                    if _is_real_token(word):
                        print(f"Found token in {path}")
                        return word

        except Exception:
            continue

    # 4. Grep across .openclaw (text files only, skip .git)
    try:
        result = subprocess.run(
            ["grep", "-roh",
             "--include=*.yaml", "--include=*.json",
             "--include=*.env", "--include=*.conf",
             "--include=*.txt", "--include=*.toml",
             "--exclude-dir=.git",
             r"sk-ant-[A-Za-z0-9_-]\{30,\}", str(OPENCLAW_DIR)],
            capture_output=True, text=True, timeout=5
        )
        tokens = [t.strip() for t in result.stdout.strip().split("\n")
                  if _is_real_token(t.strip())]
        if tokens:
            token = max(tokens, key=len)
            print(f"Found token via grep in {OPENCLAW_DIR}")
            return token
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


def _extract_from_dict(data, depth=0):
    """Recursively search a dict for a real Anthropic token."""
    if depth > 5:
        return None
    if isinstance(data, str):
        return data if _is_real_token(data) else None
    if isinstance(data, dict):
        for key in ["api_key", "anthropic_api_key", "ANTHROPIC_API_KEY",
                     "token", "auth_token", "access_token", "key"]:
            val = data.get(key, "")
            if _is_real_token(val):
                return val
        for val in data.values():
            result = _extract_from_dict(val, depth + 1)
            if result:
                return result
    if isinstance(data, list):
        for item in data:
            result = _extract_from_dict(item, depth + 1)
            if result:
                return result
    return None


def save_token(token):
    """Save token to config.yaml under claude.api_key."""
    cfg = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = yaml.safe_load(f) or {}

    if "claude" not in cfg:
        cfg["claude"] = {}
    cfg["claude"]["api_key"] = token

    # Also set top-level for compatibility
    cfg["anthropic_api_key"] = token

    with open(CONFIG_FILE, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False)

    print(f"Saved to {CONFIG_FILE}")


if __name__ == "__main__":
    token = find_token()
    if token:
        print(f"Token: {token[:12]}...{token[-4:]}")
        save_token(token)
        print("Done. Restart the watcher.")
    else:
        print("No Anthropic token found anywhere.")
        print("")
        print("The watcher needs a real token to call Claude.")
        print("Options:")
        print("  1. Re-authenticate via OpenClaw:  openclaw auth login")
        print("  2. Set env var:  export ANTHROPIC_API_KEY=sk-ant-...")
        print(f"  3. Edit {CONFIG_FILE} and set anthropic_api_key")
        print("")
        print("If you had a working token before, check:")
        print(f"  - {OPENCLAW_DIR}/credentials/")
        print(f"  - {OPENCLAW_DIR}/workspace/config.yaml")
        print("  - macOS Keychain (search for 'anthropic' or 'openclaw')")
        sys.exit(1)
