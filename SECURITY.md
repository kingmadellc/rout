# Security Policy

## Supported Versions

Security fixes are provided for the following release lines:

| Version | Supported |
| --- | --- |
| main | Yes |
| v1.x | Yes |
| < v1.0 | No |

## Reporting a Vulnerability

Please report vulnerabilities privately.

- Email: security@kingmade.ai
- Include: affected commit/version, reproduction steps, impact, and suggested fix if known.

Please do not open a public GitHub issue for security reports.

## Response Targets

- Initial acknowledgement: within 72 hours.
- Triage/update: within 7 days.
- Fix timeline: depends on severity and exploitability.

## Scope Notes

Rout relies on local macOS permissions, AppleScript/iMessage automation, and
external provider APIs. Reports are most useful when they include:

- permission/automation abuse vectors,
- credential exposure risks,
- unsafe command execution or injection paths,
- auth/signing issues in Kalshi integrations.
