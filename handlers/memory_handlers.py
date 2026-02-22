"""Memory management handlers for persistent user context."""

from datetime import datetime
from pathlib import Path


OPENCLAW_DIR = Path.home() / ".openclaw"
MEMORY_FILE = OPENCLAW_DIR / "MEMORY.md"


def _ensure_memory_file() -> None:
    OPENCLAW_DIR.mkdir(parents=True, exist_ok=True)
    if MEMORY_FILE.exists():
        return

    MEMORY_FILE.write_text(
        "# Rout Memory\n"
        "# Persistent context loaded into responses.\n\n"
        "## About Me\n"
        "- (add your personal context)\n\n"
        "## Notes\n"
        "- (add durable notes here)\n",
        encoding="utf-8",
    )


def _read_memory() -> str:
    _ensure_memory_file()
    return MEMORY_FILE.read_text(encoding="utf-8")


def memory_view_command(args=None, message="", sender=None, metadata=None):
    """View current memory content (trimmed for iMessage)."""
    content = _read_memory().strip()
    query = (args or "").strip().lower()

    if query:
        matching = [line for line in content.splitlines() if query in line.lower()]
        if not matching:
            return f"No memory lines match '{query}'."
        preview = "\n".join(matching[:20])
        if len(matching) > 20:
            preview += f"\n... ({len(matching) - 20} more)"
        return f"Memory matches for '{query}':\n{preview}"

    if len(content) > 1400:
        content = content[:1400] + "\n... (truncated)"
    return f"Current MEMORY.md:\n{content}"


def memory_add_command(args=None, message="", sender=None, metadata=None):
    """Append a note to MEMORY.md safely."""
    note = (args or "").strip()
    if not note:
        return "Usage: memory: add <note>"

    if len(note) > 300:
        return "Note is too long (max 300 chars)."

    content = _read_memory()
    lines = content.splitlines()

    notes_header = None
    for idx, line in enumerate(lines):
        if line.strip().lower() == "## notes":
            notes_header = idx
            break

    timestamp = datetime.now().strftime("%Y-%m-%d")
    new_line = f"- [{timestamp}] {note}"

    if notes_header is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("## Notes")
        lines.append(new_line)
    else:
        insert_at = notes_header + 1
        while insert_at < len(lines) and lines[insert_at].strip().startswith("-"):
            insert_at += 1
        lines.insert(insert_at, new_line)

    MEMORY_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return "Added to memory."


def memory_clear_command(args=None, message="", sender=None, metadata=None):
    """Clear memory only with explicit confirmation."""
    confirm = (args or "").strip().upper()
    if confirm != "CONFIRM":
        return "Refusing to clear memory. Re-run with: memory: clear CONFIRM"

    OPENCLAW_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(
        "# Rout Memory\n"
        "# Memory was reset via memory: clear CONFIRM\n\n"
        "## About Me\n"
        "- (add your personal context)\n\n"
        "## Notes\n"
        "- (add durable notes here)\n",
        encoding="utf-8",
    )
    return "Memory cleared and reset."
