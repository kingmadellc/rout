# First PR Guide

## Baseline Commands

```bash
python3 -m py_compile setup.py comms/imsg_watcher.py handlers/*.py config/*.py sdk/*.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Before Opening a PR

- Confirm branch is up to date with `main`.
- Confirm tests pass after changes.
- Confirm docs changed if user-facing behavior changed.
- Confirm no secrets, local absolute paths, or generated noise files are included.

## PR Body Checklist

- Problem statement
- What changed
- Why this design
- Validation commands + outcomes
- Known limitations or follow-ups
