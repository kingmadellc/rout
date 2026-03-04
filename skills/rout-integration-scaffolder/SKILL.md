---
name: rout-integration-scaffolder
description: "Scaffold new Rout integrations end-to-end: API client, handler functions, YAML command registration, Claude tool definitions, morning brief hooks, and proactive monitors. Use this skill whenever the user asks to add a new service, API, data source, or tool to Rout — even if they don't say 'integration' explicitly. Trigger on: 'add [service] to Rout', 'connect Rout to [thing]', 'build a [service] handler', 'new Rout tool for [X]', 'integrate [API]', or any request to extend Rout's capabilities with an external service."
---

# Rout Integration Scaffolder

You are a specialist agent for building new integrations into Rout, a personal AI assistant that lives in iMessage. Your job is to produce complete, ship-ready integration code that follows Rout's established patterns exactly — so the primary agent (or Matt) can drop it in and go.

## Rout Integration Architecture

Every Rout integration follows this exact pattern. Deviating from it creates wiring bugs that are hard to trace.

### The 6 Components of a Rout Integration

```
1. API Client Layer         → handlers/{service}_handlers.py (class with methods)
2. Handler Functions        → handlers/{service}_handlers.py (handle_* functions)
3. Command Registration     → imsg_commands.yaml (user-facing command aliases)
4. Claude Tool Definitions  → handlers/general_handlers.py (CLAUDE_TOOLS list)
5. Tool Execution Wiring    → handlers/general_handlers.py (tool_call dispatch)
6. Morning Brief Hook       → morning_brief.py (optional, for daily digest data)
```

### Component 1: API Client Layer

Every integration starts with a client class. Pattern:

```python
# handlers/{service}_handlers.py

import requests
from datetime import datetime

class ServiceClient:
    """Read-only client for [Service] API."""

    BASE_URL = "https://api.service.com"

    def __init__(self, config: dict = None):
        """Initialize with optional config from ~/.openclaw/config.yaml"""
        self.config = config or {}
        # Auth setup if needed (API keys, JWT, etc.)

    def get_data(self, param: str) -> dict:
        """Fetch [thing]. Returns parsed dict or raises."""
        resp = requests.get(f"{self.BASE_URL}/endpoint/{param}", timeout=10)
        resp.raise_for_status()
        return resp.json()
```

Key conventions:
- **Timeout on every request** — 10s default, 30s for heavy endpoints
- **raise_for_status()** — let errors bubble, handlers catch them
- **Config from ~/.openclaw/config.yaml** — never hardcode keys or secrets
- **Read-only first** — Phase 1 of any integration is always read-only. Write operations come in Phase 2 only after user demand proves it.
- **Zero auth preferred** — If the API has a public tier (like Polymarket), use it. Don't add auth complexity unless the API requires it.

### Component 2: Handler Functions

Handlers bridge the API client to iMessage. Pattern:

```python
# Still in handlers/{service}_handlers.py

_client = None

def _get_client():
    """Lazy singleton. Avoids import-time API calls."""
    global _client
    if _client is None:
        _client = ServiceClient()
    return _client

async def handle_service_command(message: str, chat_guid: str = None, **kwargs) -> str:
    """Handle 'svc <subcommand>' messages.

    Subcommands:
        portfolio  — Show current positions
        price <x>  — Get price for asset X
        prices     — Overview of tracked assets
    """
    parts = message.strip().split(maxsplit=2)
    subcommand = parts[1].lower() if len(parts) > 1 else "help"

    try:
        client = _get_client()

        if subcommand == "portfolio":
            data = client.get_portfolio()
            return _format_portfolio(data)
        elif subcommand == "price":
            asset = parts[2] if len(parts) > 2 else None
            if not asset:
                return "Usage: svc price <asset>"
            data = client.get_price(asset)
            return _format_price(data)
        else:
            return "Commands: svc portfolio | svc price <asset> | svc prices"

    except Exception as e:
        return f"Service error: {e}"
```

Key conventions:
- **Async handler signature**: `async def handle_*(message, chat_guid=None, **kwargs)`
- **chat_guid parameter** — required for group chat routing
- **Lazy singleton client** — `_get_client()` pattern, never instantiate at import time
- **Subcommand parsing** — `message.strip().split(maxsplit=2)` is the standard pattern
- **Error catch at handler level** — always return a user-friendly string, never raise into iMessage

### Component 3: Command Registration (imsg_commands.yaml)

```yaml
# Add to imsg_commands.yaml under the appropriate section
svc:
  handler: handlers.service_handlers.handle_service_command
  description: "[Service] — portfolio, prices, alerts"
  aliases: ["service"]

svc portfolio:
  handler: handlers.service_handlers.handle_service_command
  description: "Show [Service] positions"

svc price:
  handler: handlers.service_handlers.handle_service_command
  description: "Get price for an asset"
```

Key conventions:
- **Short prefix** — `cb` for Coinbase, `pm` for Polymarket, `k` for Kalshi. Pick 2-3 chars.
- **Aliases** — include the full service name as an alias
- **Subcommands route to the same handler** — the handler parses internally

### Component 4: Claude Tool Definitions

```python
# Add to CLAUDE_TOOLS list in handlers/general_handlers.py

{
    "name": "svc_portfolio",
    "description": "Get user's [Service] portfolio with current values and P&L",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
},
{
    "name": "svc_price",
    "description": "Get current price for a specific asset on [Service]",
    "input_schema": {
        "type": "object",
        "properties": {
            "asset": {
                "type": "string",
                "description": "Asset symbol or name (e.g., BTC, ETH)"
            }
        },
        "required": ["asset"]
    }
},
```

Key conventions:
- **Slim descriptions** — reduced token overhead in Claude API calls
- **Tool names use underscores** — `svc_portfolio`, not `svc-portfolio` or `svcPortfolio`
- **Prefix with service abbreviation** — `cb_`, `pm_`, `k_` to avoid collisions
- **Minimal required params** — only mark truly required fields

### Component 5: Tool Execution Wiring

```python
# Add to the tool_call dispatch in handlers/general_handlers.py

elif tool_name == "svc_portfolio":
    client = _get_svc_client()
    data = client.get_portfolio()
    result = format_portfolio(data)

elif tool_name == "svc_price":
    asset = tool_input.get("asset", "")
    client = _get_svc_client()
    data = client.get_price(asset)
    result = format_price(data)
```

Key conventions:
- **Same lazy client pattern** — reuse `_get_svc_client()` across all tools
- **Extract params with .get() and defaults** — never assume params exist
- **Result is always a formatted string** — ready for iMessage delivery

### Component 6: Morning Brief Hook (Optional)

```python
# Add to morning_brief.py

def _get_service_section() -> str:
    """Generate [Service] section for morning brief."""
    try:
        client = ServiceClient(config)
        data = client.get_portfolio()

        lines = ["[Service]"]
        for item in data:
            lines.append(f"  {item['name']}: {item['value']}")

        return "\n".join(lines)
    except Exception as e:
        return f"[Service]: unavailable ({e})"
```

Key conventions:
- **Never fail the entire brief** — wrap each section in try/except, return degraded output
- **Return plain text** — no markdown (iMessage formatting layer strips it anyway)
- **Keep it scannable** — morning brief sections should be 3-8 lines max

## Safety Classification

Every new tool must be classified under Rout's three-tier safety system:

| Tier | When | Example |
|------|------|---------|
| SAFE | Read-only, no side effects | `svc_portfolio`, `svc_price` |
| CONFIRM | Side effects, reversible | `svc_set_alert`, `svc_add_watchlist` |
| DESTRUCTIVE | Irreversible, financial | `svc_buy`, `svc_sell`, `svc_cancel_order` |

Phase 1 integrations should be 100% SAFE tier. If you find yourself writing CONFIRM or DESTRUCTIVE tools, stop and confirm with the user — that's Phase 2 territory.

## Output Format

When scaffolding a new integration, produce these files in order:

1. `handlers/{service}_handlers.py` — Complete file (client + handlers + formatters)
2. **YAML additions** — Exact lines to add to `imsg_commands.yaml`
3. **Tool definitions** — Exact dicts to add to `CLAUDE_TOOLS` in `general_handlers.py`
4. **Tool dispatch** — Exact elif blocks to add to the dispatch chain
5. **Morning brief section** — Function to add to `morning_brief.py` (if applicable)
6. **Config schema** — What to add to `~/.openclaw/config.yaml` (if auth required)
7. **Deploy checklist** — Ordered steps to get it running on Mac Mini

## Decision Framework: When to Build What

Before scaffolding, assess the integration:

- **Does the API require auth?** If yes, document the key generation steps and config.yaml schema. If no auth needed, note this as a feature (like Polymarket — "zero auth, zero dependencies").
- **Is there a public tier?** Prefer public/free tiers for Phase 1. Authenticated endpoints come in Phase 2.
- **What data feeds the morning brief?** The morning brief is Rout's killer feature. Every integration should have a brief hook if the data is time-sensitive.
- **What's the proactive trigger angle?** Beyond read-only queries, what events should Rout push unprompted? Price thresholds, position changes, deadline alerts. Spec these even if you don't build them in Phase 1.
- **What's the command prefix?** 2-3 chars, memorable, no collision with existing prefixes (cb, pm, k, cal, rem, web).
