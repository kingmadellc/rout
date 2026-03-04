"""
UI helpers for Rout setup wizard — colored output and user interaction.
"""

# ── Colors ────────────────────────────────────────────────────────────────────

GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
RED = "\033[0;31m"
BOLD = "\033[1m"
DIM = "\033[2m"
NC = "\033[0m"


def ok(msg):
    """Print success message."""
    print(f"{GREEN}  ✓ {msg}{NC}")


def warn(msg):
    """Print warning message."""
    print(f"{YELLOW}  ! {msg}{NC}")


def fail(msg):
    """Print failure message."""
    print(f"{RED}  ✗ {msg}{NC}")


def ask(prompt, default="", validate=None, required=False):
    """
    Prompt user for input with optional default and validation.

    Args:
        prompt: Question to ask
        default: Default value if user presses Enter
        validate: Optional validation function returning error message or None
        required: If True, field cannot be empty

    Returns:
        User input string
    """
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{BOLD}{prompt}{suffix}:{NC} ").strip()
        if not val and default:
            val = default
        if required and not val:
            warn("This field is required.")
            continue
        if validate and val:
            err = validate(val)
            if err:
                warn(err)
                continue
        return val


def ask_choice(prompt, options, default=1):
    """
    Prompt user to pick from numbered options.

    Args:
        prompt: Question to ask
        options: List of (label, value) tuples
        default: Default option index (1-based)

    Returns:
        The value from the selected (label, value) tuple
    """
    print(f"\n{BOLD}{prompt}{NC}")
    for i, (label, _) in enumerate(options, 1):
        marker = " (default)" if i == default else ""
        print(f"  {i}. {label}{marker}")
    while True:
        val = input(f"{BOLD}Choose [1-{len(options)}]:{NC} ").strip()
        if not val:
            return options[default - 1][1]
        try:
            idx = int(val)
            if 1 <= idx <= len(options):
                return options[idx - 1][1]
        except ValueError:
            pass
        warn(f"Enter a number 1-{len(options)}")
