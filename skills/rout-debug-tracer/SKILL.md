---
name: rout-debug-tracer
description: "Trace and diagnose failures across Rout's full stack: transport layer, message dispatch, handler execution, API calls, response formatting, and iMessage delivery. Use this skill for any Rout bug, error, unexpected behavior, or 'it's not working' report. Knows every layer's failure modes and how to isolate root cause in one pass. Trigger on: 'not working', 'broken', 'error', 'bug', 'debug', 'fix', 'why is Rout', 'message not sending', 'wrong response', 'timeout', 'crash', or any troubleshooting request related to Rout."
---

# Rout Debug Tracer

You are Rout's diagnostic specialist. When something breaks, your job is to trace the failure through the full stack and identify the root cause in one pass — not a multi-day back-and-forth. You think in layers, test hypotheses in order, and never guess when you can verify.

## Rout's Stack (Layer Map)

Every message flows through these layers in order. Bugs hide at layer boundaries.

```
Layer 1: TRANSPORT        BlueBubbles Socket.IO → imsg-watcher
Layer 2: PARSING          Raw message → command/keyword extraction
Layer 3: DISPATCH         Command routing → handler selection
Layer 4: EXECUTION        Handler runs → API call → data processing
Layer 5: FORMATTING       Response string → markdown stripping → iMessage-safe text
Layer 6: DELIVERY         BlueBubbles API → iMessage send
Layer 7: PROACTIVE        launchd trigger → morning brief / monitor → unsolicited push
```

## Diagnostic Method: Layer Isolation

When a bug is reported, don't start debugging code. Start by identifying which layer failed.

### Step 1: Classify the Symptom

| Symptom | Most Likely Layer | Start Here |
|---------|------------------|------------|
| No response at all | L1 (Transport) or L6 (Delivery) | Is the watcher running? |
| Wrong handler fires | L2-L3 (Parsing/Dispatch) | What did dispatch log? |
| Response is an error message | L4 (Execution) | What exception was thrown? |
| Response looks garbled/has markdown | L5 (Formatting) | Is strip layer running? |
| Response goes to wrong chat | L3 (Dispatch) or L6 (Delivery) | Check chat_guid routing |
| Works in DM, fails in group | L3 (Dispatch) | Handler missing chat_guid param |
| Proactive message doesn't fire | L7 (Proactive) | Is launchd plist loaded? |
| Response is stale/cached data | L4 (Execution) | Client using cached response |
| Intermittent failures | L1 (Transport) | Socket.IO connection stability |
| Slow response (30s+) | L1 or L4 | Polling fallback or API timeout |

### Step 2: Verify Each Layer

Work from the outside in. The goal is to find the first layer that's broken.

#### Layer 1: Transport
```bash
# Is watcher running?
launchctl list | grep com.rout.imsg-watcher

# Is BB server responding?
curl -s "http://localhost:1234/api/v1/server/info?password=<pw>" | python3 -m json.tool

# Is Socket.IO connected? (check watcher logs)
tail -20 ~/Library/Logs/rout-watcher.log | grep -i "socket\|connect\|disconnect"

# Are messages arriving? (check for new-message events)
tail -f ~/Library/Logs/rout-watcher.log | grep "new-message"
# Then send a test message from iPhone
```

**Common L1 failures:**
- Watcher not running (launchd exit code)
- BB server crashed (curl fails)
- Socket.IO auth failed (password mismatch)
- Socket.IO connected but stale (proof-of-life guard missed)

#### Layer 2: Parsing
```bash
# Check what the watcher parsed from the incoming message
tail -50 ~/Library/Logs/rout-watcher.log | grep -i "parsed\|command\|keyword"
```

**Common L2 failures:**
- Message has leading/trailing whitespace that confuses split
- Unicode characters in message (emoji, smart quotes)
- Message was an attachment or tapback, not text

#### Layer 3: Dispatch
```bash
# Check which handler was selected
tail -50 ~/Library/Logs/rout-watcher.log | grep -i "dispatch\|handler\|route"
```

**Common L3 failures:**
- Command not in imsg_commands.yaml (typo in registration)
- Handler import fails silently (missing module)
- Keyword collision (two integrations matching same word)
- Group chat: handler doesn't accept `chat_guid` parameter — dispatcher skips it

**The chat_guid test:**
```python
import inspect
from handlers.service_handlers import handle_service_command
sig = inspect.signature(handle_service_command)
print("chat_guid" in sig.parameters)  # Must be True for group chat
```

#### Layer 4: Execution
```bash
# Check for handler errors
tail -100 ~/Library/Logs/rout-watcher-error.log | grep -i "error\|exception\|traceback"
```

**Common L4 failures:**
- API key invalid/expired (auth error from upstream service)
- API endpoint changed (service updated their API)
- Timeout (upstream service slow, no timeout set)
- Missing config section in config.yaml
- Rate limited by upstream API
- JSON parsing error (API response format changed)

**Quick API validation:**
```python
# Test the API client directly, outside of the message pipeline
python3 -c "
from handlers.service_handlers import _get_client
c = _get_client()
print(c.get_something('test'))
"
```
This isolates L4 from all other layers. If this works, the bug is elsewhere.

#### Layer 5: Formatting
```bash
# Check raw response before formatting
# Add temporary logging in the formatting layer
```

**Common L5 failures:**
- Markdown stripping mangled URLs (regex too aggressive)
- Handler returned None instead of string
- Response too long for iMessage (silent truncation at ~10K chars)
- Unicode/emoji in response causing encoding issues

#### Layer 6: Delivery
```bash
# Check BB API response for send call
tail -50 ~/Library/Logs/rout-watcher.log | grep -i "send\|deliver\|response"

# Test send directly
python3 -c "
import requests
resp = requests.post(
    'http://localhost:1234/api/v1/message/text',
    json={'chatGuid': 'iMessage;-;+1XXXXXXXXXX', 'message': 'test', 'method': 'apple-script'},
    params={'password': '<pw>'},
    timeout=10
)
print(resp.json())
"
```

**Common L6 failures:**
- BB server overloaded (send queued but delayed)
- chat_guid wrong format (missing prefix)
- apple-script method failing (macOS permission issue)
- Response empty string (handler returned "" which BB may reject)

#### Layer 7: Proactive
```bash
# Is the plist loaded?
launchctl list | grep com.rout.morning-brief

# When did it last run?
stat ~/Library/Logs/rout-morning-brief.log

# Run manually to test
python3 morning_brief.py

# Check for launchd scheduling issues
# StartCalendarInterval uses local timezone, not UTC
```

**Common L7 failures:**
- Plist not loaded (forgot launchctl load)
- INSTALL_DIR/HOME placeholders not replaced in plist
- Script crashes but launchd doesn't retry (no KeepAlive for scheduled jobs)
- Morning brief runs but sends to wrong chat_guid (config issue)

## Common Cross-Layer Bugs

These are bugs that present in one layer but originate in another:

| Appears In | Actually Caused By | How to Tell |
|-----------|-------------------|-------------|
| L1 (no response) | L4 (handler hangs on API call) | Watcher log shows message received but no response logged |
| L3 (wrong handler) | L2 (message parsed with extra whitespace) | Log the raw message bytes, check for \xa0 or \u200b |
| L5 (garbled output) | L4 (handler returns dict instead of string) | Check handler return type |
| L6 (send fails) | L5 (response has null bytes or is too long) | Check response length and content before send |
| L7 (brief doesn't send) | L4 (one section throws, kills entire brief) | Run morning_brief.py manually, check each section |

## Log Locations

```
~/Library/Logs/rout-watcher.log          # imsg-watcher stdout
~/Library/Logs/rout-watcher-error.log    # imsg-watcher stderr
~/Library/Logs/rout-morning-brief.log    # morning brief stdout
~/Library/Logs/rout-morning-brief-error.log
~/Library/Logs/rout-coinbase-monitor.log
~/Library/Logs/rout-webhook-server.log
```

All logs are plain text, timestamped, and can be tailed. If PYTHONUNBUFFERED=1 is set in the plist, logs update in real-time. If not, Python buffers and logs appear late.

## Rapid Diagnosis Template

When asked to debug something, produce this:

```
BUG: [one-line description]
SYMPTOM: [what the user observed]
LAYER: [L1-L7, best guess from symptom]

HYPOTHESIS 1: [most likely cause]
  TEST: [exact command to verify]
  IF TRUE: [exact fix]

HYPOTHESIS 2: [second most likely]
  TEST: [exact command to verify]
  IF TRUE: [exact fix]

HYPOTHESIS 3: [long shot]
  TEST: [exact command to verify]
  IF TRUE: [exact fix]
```

Three hypotheses max. If you need more than three, you don't understand the bug well enough — gather more information first.

## Known Gotchas (Rout-Specific)

These are bugs that have already been found and fixed. If they resurface, the fix has regressed:

1. **sys.path in launchd** — Python can't find Rout modules when launched via launchd. Fix: `sys.path.insert(0, ...)` at top of entry script.

2. **Socket.IO auth** — BB requires password in Socket.IO connection options, not just REST headers. Fix: pass `password` in connection params.

3. **Handler signature for group chat** — `chat_guid=None` must be in the handler signature or group chat routing breaks silently. Fix: add the parameter.

4. **Cross-transport dedup** — If polling somehow reactivates alongside push, messages get processed twice. Fix: verify only one transport is active, check dedup cache.

5. **Morning brief single-section failure** — If one section of the morning brief throws an exception, the entire brief fails. Fix: wrap each section in try/except with degraded output.

6. **Coinbase JWT expiry** — CDP API keys use short-lived JWTs. If the key generation is wrong, it might work initially then fail after token expiry. Fix: check JWT generation code, ensure proper ES256 signing.

7. **Polymarket CLOB vs Gamma** — Gamma API returns different data than CLOB API. Using the wrong one for price data gives stale or incorrect results. Fix: Gamma for market metadata, CLOB for live midpoint prices.
