"""
Example handler module — shows the pattern for adding new commands.

To add your own commands:
1. Copy this file (e.g. weather_handlers.py)
2. Implement functions ending in _command
3. Register them in imsg_commands.yaml
4. Restart the watcher

Rules:
- Function signature: def mycommand_command(args: str = "") -> str
- Always return a string
- Handle exceptions gracefully
- Keep responses short (iMessage-friendly)
"""


def hello_command(args: str = "") -> str:
    """Say hello"""
    name = args.strip() if args.strip() else "friend"
    return f"👋 Hello, {name}!"


def echo_command(args: str = "") -> str:
    """Echo back whatever you send"""
    if not args.strip():
        return "Echo: (nothing sent)"
    return f"Echo: {args}"


def compute_command(args: str = "") -> str:
    """Add and multiply two numbers. Usage: example: compute 5 10"""
    try:
        parts = args.strip().split()
        if len(parts) < 2:
            return "Usage: example: compute <a> <b>"
        a, b = int(parts[0]), int(parts[1])
        return f"Sum: {a + b}, Product: {a * b}"
    except ValueError:
        return "Error: both arguments must be numbers"
    except Exception as e:
        return f"Error: {e}"


# ─── Pattern for new commands ────────────────────────────────────────────────
#
# def myfeature_command(args: str = "") -> str:
#     """Short description (shown in help)"""
#     try:
#         if not args.strip():
#             return "Usage: mybot: myfeature <argument>"
#         result = do_something(args)
#         return result
#     except Exception as e:
#         return f"Error: {e}"
#
# Then in imsg_commands.yaml:
#   mybot:myfeature:
#     desc: "Short description"
#     handler: "example_handlers.myfeature_command"
#     example: "mybot: myfeature hello"
