# Security Policy

## Supported Versions

Security fixes are provided for the following release lines:

| Version | Supported |
| --- | --- |
| main | Yes |
| v0.8.x | Yes |

## Reporting a Vulnerability

Please report vulnerabilities privately.

- Open a [private security advisory](https://github.com/kingmadellc/rout/security/advisories/new) on GitHub.
- Include: affected commit/version, reproduction steps, impact, and suggested fix if known.

Please do not open a public GitHub issue for security reports.

## Response Targets

- Initial acknowledgement: within 72 hours.
- Triage/update: within 7 days.
- Fix timeline: depends on severity and exploitability.

## Threat Model

Rout operates as a privileged macOS agent with access to sensitive local resources. The following trust boundaries define the attack surface.

### What Rout Has Access To

| Resource | Access Level | Mechanism |
|---|---|---|
| iMessage database | Read | SQLite via `imsg` CLI (requires Full Disk Access) |
| iMessage sending | Write | `osascript` / `imsg` CLI |
| Apple Calendar | Read/Write | AppleScript via `osascript` |
| Apple Reminders | Read/Write | AppleScript via `osascript` |
| Anthropic API | Network | OAuth token stored in `~/.openclaw/agents/` |
| Kalshi API | Network | API key stored in `~/.openclaw/keys/` |
| Polymarket API | Network | No auth required (public Gamma API) |
| Local filesystem | Read/Write | `~/.openclaw/` directory tree |
| Ollama (optional) | Localhost | HTTP on port 11434 |
| BlueBubbles (optional) | Localhost/Network | Socket.IO on configured port |

### Trust Boundaries

1. **User ↔ Rout (iMessage):** Messages arrive via the iMessage database. Rout trusts messages from configured `known_senders` phone numbers. Unknown senders are ignored. No authentication beyond phone number matching.

2. **Rout ↔ AI Provider:** System prompts and user messages (including calendar data and memory) are sent to the configured AI provider. The provider sees all context included in the request.

3. **Rout ↔ AppleScript:** Calendar and reminder operations execute AppleScript via `osascript`. Input sanitization (quote escaping) is applied to prevent injection. Destructive operations (event creation, reminder modification) require explicit user confirmation via the safety gate.

4. **Rout ↔ Trading APIs:** Kalshi trade execution (buy/sell/cancel) requires user confirmation via the safety gate. Polymarket is read-only.

5. **Rout ↔ Local Files:** Config, state, memory, and audit logs are stored under `~/.openclaw/`. Config is written with `0o600` permissions (user-only). API keys and PEM files are stored in `~/.openclaw/keys/`.

### Known Risk Areas

| Risk | Mitigation | Residual Risk |
|---|---|---|
| AppleScript injection via calendar/reminder titles | Quote escaping in `calendar_tools.py` | Edge cases in Unicode or multi-byte characters |
| AI provider prompt injection | System prompt instructs Claude to refuse harmful actions; safety gate blocks destructive tool use | Adversarial prompts via iMessage could attempt to manipulate agent behavior |
| Credential exposure in logs | Audit logs record tool names and arguments but not API keys; `.gitignore` excludes all `.key`, `.pem`, and `config.yaml` files | Log files on disk are readable by the local user |
| Rate limit amplification | Circuit breaker (8 msgs/60s, exponential backoff up to 1hr) prevents runaway message loops | A sustained attack from a known sender could exhaust provider quotas |
| Third-party skill injection | Bundled skills are version-controlled and reviewed; ClawHub marketplace skills are explicitly untrusted (see HOW_IT_WORKS.md) | User-installed third-party skills execute with full Rout privileges |
| State file tampering | Provider failover state and pending actions stored as JSON in `~/.openclaw/state/` | Any process running as the local user can modify state files |

### Safe Defaults

- **No credentials in code.** All API keys, tokens, and PEM files are stored outside the repository in `~/.openclaw/keys/` or `~/.openclaw/agents/`.
- **Destructive actions require confirmation.** The safety gate (`agent/safety_gate.py`) intercepts calendar writes, reminder modifications, and trade executions. Pending confirmations expire after 1 hour.
- **Audit trail.** All tool invocations are logged to JSONL audit files with timestamps, tool names, and arguments.
- **Pre-commit hooks.** Gitleaks scans for accidentally committed secrets on every commit.
- **Config file permissions.** `setup.py` writes `config.yaml` with `0o600` (user read/write only).
- **No network listeners by default.** The webhook server and status API are optional services that must be explicitly enabled via launchd.

## Scope Notes

Rout relies on local macOS permissions, AppleScript/iMessage automation, and
external provider APIs. Reports are most useful when they include:

- permission/automation abuse vectors,
- credential exposure risks,
- unsafe command execution or injection paths,
- prompt injection vectors that bypass the safety gate.
