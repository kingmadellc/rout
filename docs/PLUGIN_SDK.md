# Rout Plugin SDK (Typed Contract)

Rout handlers can use a lightweight typed contract to avoid ad-hoc parsing.

## Imports

```python
from sdk.command_contract import context_from_inputs, text_result
```

## Pattern

```python
def my_command(args=None, message="", sender=None, metadata=None):
    ctx = context_from_inputs(args=args or "", message=message, sender=sender or "", metadata=metadata)

    if not ctx.args.strip():
        return text_result("Usage: my: command <arg>").text

    return text_result(f"Hello {ctx.sender_name or 'there'}").text
```

## Context Fields

- `args`: parsed command arguments
- `message`: full inbound text
- `sender`: sender handle
- `chat_id`: numeric chat id if available
- `sender_name`: mapped display name
- `is_group`: group-chat flag
- `attachments`: attachment file paths

## Result Fields

- `text`: outbound iMessage body
- `metadata`: optional machine-readable extras
