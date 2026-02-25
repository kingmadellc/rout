# Build Your First Rout Handler

This guide walks you through creating a custom handler from scratch. By the end, you'll have a working command you can text to yourself via iMessage.

**Time required:** ~5 minutes.

---

## How Rout Works

Rout is a polling loop that watches your iMessage for new texts. When one arrives, it checks `imsg_commands.yaml` for a matching trigger. If it finds one, it calls the handler function. If no trigger matches, it falls back to Claude.

Every handler is a Python function that takes a string and returns a string. That's the entire contract.

```
You text "stocks: AAPL"
  → Watcher matches trigger "stocks:"
    → Calls stocks_handlers.lookup_command("AAPL")
      → Returns "AAPL: $187.42 (+1.3%)"
        → Sent back via iMessage
```

## Step 1: Create Your Handler File

Create a new file in the `handlers/` directory. Name it after your capability.

```python
# handlers/stocks_handlers.py
"""Stock price lookup via iMessage."""

import json
import urllib.request


def lookup_command(args: str = "") -> str:
    """Look up a stock price. Usage: stocks: AAPL"""
    ticker = args.strip().upper()
    if not ticker:
        return "Usage: stocks: <TICKER>\nExample: stocks: AAPL"

    try:
        # Using a free API (replace with your preferred source)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Rout/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())

        result = data["chart"]["result"][0]
        meta = result["meta"]
        price = meta["regularMarketPrice"]
        prev_close = meta["previousClose"]
        change = price - prev_close
        pct = (change / prev_close) * 100
        sign = "+" if change >= 0 else ""

        return f"{ticker}: ${price:.2f} ({sign}{pct:.1f}%)"
    except Exception as e:
        return f"Couldn't fetch {ticker}: {e}"


def watchlist_command(args: str = "") -> str:
    """Check multiple tickers. Usage: stocks: watchlist AAPL,TSLA,GOOG"""
    tickers = [t.strip().upper() for t in args.split(",") if t.strip()]
    if not tickers:
        return "Usage: stocks: watchlist AAPL,TSLA,GOOG"

    results = []
    for ticker in tickers[:5]:  # Cap at 5 to keep iMessage-friendly
        results.append(lookup_command(ticker))

    return "\n".join(results)
```

### Handler Rules

1. **Function signature**: `def name_command(args: str = "") -> str`
2. **Always return a string** — this becomes the iMessage reply
3. **Handle exceptions** — never let errors crash the watcher
4. **Keep responses short** — iMessage truncates long messages
5. **Suffix with `_command`** — the watcher uses this convention to find handlers

## Step 2: Register Your Commands

Open `imsg_commands.yaml` and add your new commands:

```yaml
# ── Stocks ──────────────────────────────────────────────────────────────────
stocks:lookup:
  trigger: "stocks:"
  description: "Look up a stock price"
  handler: "stocks_handlers.lookup_command"
  args: ["ticker"]

stocks:watchlist:
  trigger: "stocks: watchlist"
  description: "Check multiple stock prices"
  handler: "stocks_handlers.watchlist_command"
  args: ["tickers"]
```

### Trigger Rules

- The trigger string is matched against the start of incoming messages (case-insensitive)
- More specific triggers are matched first (`stocks: watchlist` before `stocks:`)
- Everything after the trigger is passed as `args`
- Set `trigger: null` and `fallback: true` for catch-all handlers (like Claude)

## Step 3: Restart and Test

```bash
./stop_watcher.sh && ./start_watcher.sh
```

Now text yourself:

```
stocks: AAPL
```

You'll get back something like `AAPL: $187.42 (+1.3%)`.

## Using the Typed SDK (Optional)

For handlers that need richer context — who sent the message, which chat it came from, whether there are attachments — use the typed contract:

```python
# handlers/team_handlers.py
"""Handlers that need sender context."""

from sdk.command_contract import context_from_inputs, text_result


def greet_command(args=None, message="", sender=None, metadata=None):
    """Greet the sender by name."""
    ctx = context_from_inputs(
        args=args or "",
        message=message,
        sender=sender or "",
        metadata=metadata,
    )

    name = ctx.sender_name or "stranger"
    if ctx.is_group:
        return text_result(f"Hey {name}! (from the group chat)").text

    return text_result(f"Hey {name}, what's up?").text
```

### Available Context Fields

| Field | Type | Description |
|---|---|---|
| `ctx.args` | `str` | Parsed command arguments |
| `ctx.message` | `str` | Full inbound message text |
| `ctx.sender` | `str` | Sender phone/handle |
| `ctx.chat_id` | `int \| None` | Numeric chat ID |
| `ctx.sender_name` | `str` | Display name from `known_senders` |
| `ctx.is_group` | `bool` | Whether this is a group chat |
| `ctx.attachments` | `list[str]` | File paths to any attached images/files |

## Patterns and Tips

### Calling Claude from Your Handler

Your handler can call Claude for structured extraction or freeform reasoning:

```python
from handlers.general_handlers import _call_claude, _extract_json

def smart_command(args: str = "") -> str:
    """Use Claude to interpret complex input."""
    # Structured extraction
    data = _extract_json(
        f'Extract the city and date from: "{args}"\n'
        f'Return JSON: {{"city": str, "date": str}}'
    )

    # Or freeform
    response = _call_claude(f"Summarize this in one sentence: {args}")
    return response
```

### Async / Long-Running Operations

Handlers should return quickly (under 10 seconds). For longer operations, return an acknowledgment and schedule follow-up:

```python
import subprocess
import tempfile
import os

def export_command(args: str = "") -> str:
    """Kick off an export and notify when done."""
    # Schedule a follow-up message
    chat_id = 1  # Or extract from metadata
    script = f'imsg send --chat-id {chat_id} --service imessage --text "Export complete!"'
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
        f.write(script)
        tmp = f.name
    os.chmod(tmp, 0o755)
    subprocess.Popen(['at', '-f', tmp, 'now + 2 minutes'])
    return "Export started — I'll text you when it's done."
```

### Testing Without iMessage

Use mock mode to test locally:

```bash
# Terminal 1: Start the watcher in mock mode
ROUT_OPENCLAW_DIR=/tmp/rout-openclaw ROUT_MOCK_MODE=1 python3 comms/imsg_watcher.py

# Terminal 2: Send a test message
ROUT_OPENCLAW_DIR=/tmp/rout-openclaw python3 comms/mock_send.py "stocks: AAPL"

# Terminal 3: Watch the output
tail -f /tmp/rout-openclaw/logs/mock_outbox.jsonl
```

### Compile Check

Before restarting, verify your handler compiles:

```bash
python3 -m py_compile handlers/stocks_handlers.py
```

## What's Next

- Browse `handlers/general_handlers.py` to see how calendar, reminders, and web search are implemented
- Check `handlers/kalshi_handlers.py` for an example of handlers that call external APIs with authentication
- Read [PLUGIN_SDK.md](PLUGIN_SDK.md) for the full typed contract reference
- Join the [OpenClaw community](https://openclaw.ai) to share your handlers

---

<p align="center">
  Built by <a href="https://github.com/kingmadellc">KingMade LLC</a> · Powered by <a href="https://openclaw.ai">OpenClaw</a>
</p>
