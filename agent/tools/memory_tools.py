"""
Memory tools for Rout agent.

Provides query and add operations for the agent's persistent memory.
Uses vector store (ChromaDB) with graceful fallback to MEMORY.md.
"""

from datetime import datetime
from pathlib import Path

OPENCLAW_DIR = Path.home() / ".openclaw"
MEMORY_DIR = OPENCLAW_DIR / "memory"
MEMORY_PATH = MEMORY_DIR / "MEMORY.md"
LEGACY_MEMORY_PATH = OPENCLAW_DIR / "MEMORY.md"


def _migrate_legacy_memory_file() -> None:
    """Move legacy ~/.openclaw/MEMORY.md to ~/.openclaw/memory/MEMORY.md once."""
    if MEMORY_PATH.exists() or not LEGACY_MEMORY_PATH.exists():
        return
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    LEGACY_MEMORY_PATH.rename(MEMORY_PATH)


def query_memory(query: str = "") -> str:
    """Search the user's persistent memory.

    Uses vector similarity search to return relevant memories.
    Falls back to MEMORY.md if vector store is unavailable.
    """
    try:
        from memory.vector_store import query as vector_query, _load_markdown_fallback

        if query:
            results = vector_query(query, top_k=5)
            if results:
                return "\n".join(results)

        # Fallback to markdown
        content = _load_markdown_fallback(max_chars=3000)
        return content
    except ImportError:
        # vector_store not available, use legacy path
        return _legacy_query()
    except Exception as e:
        return f"[Memory read error: {e}]"


def add_memory(note: str) -> str:
    """Add a note to the user's persistent memory.

    Writes to both vector store and MEMORY.md backup.
    """
    if not note or not note.strip():
        return "Cannot add empty note."
    note = note.strip()
    if len(note) > 500:
        note = note[:500]

    try:
        from memory.vector_store import add as vector_add
        return vector_add(note)
    except ImportError:
        # vector_store not available, use legacy path
        return _legacy_add(note)
    except Exception:
        # Vector store failed, fall back to legacy
        return _legacy_add(note)


def load_memory_for_context(message: str = "") -> str:
    """Load memory content for injection into system prompts.

    Uses vector retrieval for relevant context if available.
    Falls back to MEMORY.md if vector store is unavailable.
    """
    try:
        from memory.vector_store import load_for_context
        return load_for_context(message=message, max_chars=3000)
    except ImportError:
        return _legacy_load_context()
    except Exception:
        return _legacy_load_context()


# ── Legacy Fallbacks (MEMORY.md only) ───────────────────────────────────────

def _legacy_query() -> str:
    """Legacy query — full MEMORY.md dump."""
    try:
        _migrate_legacy_memory_file()
        if not MEMORY_PATH.exists():
            return "No memory stored yet."
        content = MEMORY_PATH.read_text().strip()
        if not content:
            return "Memory is empty."
        if len(content) > 3000:
            content = content[:3000] + "\n\n[... truncated]"
        return content
    except Exception as e:
        return f"[Memory read error: {e}]"


def _legacy_add(note: str) -> str:
    """Legacy add — append to MEMORY.md only."""
    try:
        _migrate_legacy_memory_file()
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not MEMORY_PATH.exists():
            MEMORY_PATH.write_text("# Rout Memory\n\n## Notes\n\n")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"- [{timestamp}] {note}\n"

        with open(MEMORY_PATH, "a") as f:
            f.write(entry)

        return f"Saved to memory: {note[:100]}"
    except Exception as e:
        return f"[Memory write error: {e}]"


def _legacy_load_context() -> str:
    """Legacy context load — full MEMORY.md truncated."""
    try:
        _migrate_legacy_memory_file()
        if not MEMORY_PATH.exists():
            return ""
        content = MEMORY_PATH.read_text().strip()
        if len(content) > 3000:
            content = content[:3000] + "\n[... truncated for context window]"
        return content
    except Exception:
        return ""
