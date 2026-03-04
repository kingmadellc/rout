# Rout Reliability Test Matrix

## Parser and Routing

- Bare command parsing
- Namespace command parsing
- Swapped command parsing
- Unknown command handling

## Runtime Paths

- Workspace resolution
- Config path resolution
- `imsg` binary resolution

## Mock Mode

- Read inbox JSONL
- Write outbox JSONL
- Preserve normal-mode behavior when disabled

## Core Commands

- `help`, `status`, `doctor`, `ping`
- Memory handlers (`view`, `add`, `clear CONFIRM`)

## Standard Validation

```bash
python3 -m py_compile setup.py comms/imsg_watcher.py handlers/*.py config/*.py sdk/*.py
python3 -m unittest discover -s tests -p 'test_*.py'
```
