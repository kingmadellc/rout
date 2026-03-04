# Contributing to Rout

Thanks for contributing.

## Before You Start

- Read the [Code of Conduct](CODE_OF_CONDUCT.md).
- Open an issue for substantial changes before opening a PR.
- Keep changes focused and testable.

## Local Setup

1. Clone and enter the repo.

```bash
git clone https://github.com/kingmadellc/rout.git
cd rout
```

2. Run setup.

```bash
python3 setup.py
```

3. Run tests.

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

4. Optional: run mock mode if you do not want to use iMessage permissions during development.

```bash
ROUT_OPENCLAW_DIR=/tmp/rout-openclaw ROUT_MOCK_MODE=1 python3 comms/imsg_watcher.py
ROUT_OPENCLAW_DIR=/tmp/rout-openclaw python3 comms/mock_send.py "help"
```

## Development Workflow

1. Create a branch from `main`.
2. Make small, focused commits.
3. Add or update tests for behavior changes.
4. Run tests locally.
5. Open a PR using the provided template.

## Style and Expectations

- Target Python 3.10+ compatibility.
- Keep handlers iMessage-safe (short plain-text responses).
- Avoid hardcoded credentials and machine-specific paths.
- Preserve backward compatibility for handler signatures when possible.

## Commit Message Policy

Never reference secrets, credentials, PII, or security fixes in commit messages. Commit messages are public, indexed by search engines, and persist in git history even after file changes are reverted.

- Use neutral language: `refactor`, `chore`, `fix` — never `security`, `scrub`, `redact`, `credential`, `PII`, `secret`, `password`, `key rotation`.
- Never name specific data types that were removed (e.g., "phone numbers", "API keys").
- Never reference file contents in the message (e.g., "contains credentials").
- If a commit fixes a vulnerability, describe what was improved functionally, not what was wrong.

Good: `chore: templatize service configs for portability`
Bad: `security: remove hardcoded username from all plists`

Good: `refactor: centralize configuration loading`
Bad: `security: deploy hardened codebase, replace 24 files with hardcoded keys`

## Testing Guidance

Minimum checks before PR:

```bash
python3 -m py_compile setup.py comms/imsg_watcher.py handlers/*.py config/*.py sdk/*.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

If your change affects runtime behavior, include one of:

- a new/updated automated test, or
- clear manual test notes in the PR description.

## Areas That Need Help

- Reliability and integration testing for watcher polling and message send paths.
- Docs and onboarding improvements for first-time users.
- Safe local development tooling (including mock mode workflows).

## Security

Please do not report security issues in public issues. Use [SECURITY.md](SECURITY.md).
