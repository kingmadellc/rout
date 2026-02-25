"""
Vector memory store for Rout.

Uses ChromaDB for semantic search with Ollama embeddings (nomic-embed-text).
Gracefully degrades to MEMORY.md full-dump if ChromaDB or Ollama are unavailable.

Storage layout:
  ~/.openclaw/memory/
    ├── chroma_db/          # ChromaDB persistent storage
    ├── MEMORY.md           # Human-readable backup (always maintained)
    └── memory_meta.json    # Migration status, entry count, timestamps

Design:
  - Every memory is stored in BOTH vector store and MEMORY.md
  - MEMORY.md is the source of truth for migration/recovery
  - Vector store enables semantic retrieval (top-K relevant memories)
  - If vector store is down, falls back to MEMORY.md injection (original behavior)
"""

import json
import logging
import time
import hashlib
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ── Config ──────────────────────────────────────────────────────────────────

MEMORY_DIR = Path.home() / ".openclaw" / "memory"
MEMORY_MD_PATH = MEMORY_DIR / "MEMORY.md"
CHROMA_DB_PATH = MEMORY_DIR / "chroma_db"
META_PATH = MEMORY_DIR / "memory_meta.json"

EMBED_MODEL = "nomic-embed-text"
OLLAMA_EMBED_URL = "http://localhost:11434/api/embeddings"
COLLECTION_NAME = "rout_memory"

# Ensure directory exists
MEMORY_DIR.mkdir(parents=True, exist_ok=True)


# ── ChromaDB Lazy Init ─────────────────────────────────────────────────────

_client = None
_collection = None
_chroma_available = None  # None = not checked, True/False = cached result


def _init_chroma():
    """Lazy-init ChromaDB client and collection."""
    global _client, _collection, _chroma_available

    if _chroma_available is False:
        return False

    try:
        import chromadb
        from chromadb.config import Settings

        _client = chromadb.Client(Settings(
            chroma_db_impl="duckdb+parquet",
            persist_directory=str(CHROMA_DB_PATH),
            anonymized_telemetry=False,
        ))

        _collection = _client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

        _chroma_available = True
        return True
    except ImportError:
        log.info("ChromaDB not installed — using MEMORY.md fallback")
        _chroma_available = False
        return False
    except Exception as e:
        log.warning("ChromaDB init failed: %s — using MEMORY.md fallback", e)
        _chroma_available = False
        return False


# ── Ollama Embeddings ───────────────────────────────────────────────────────

def _get_embedding(text: str) -> Optional[list]:
    """Get embedding vector from Ollama. Returns None if unavailable."""
    try:
        payload = json.dumps({
            "model": EMBED_MODEL,
            "prompt": text,
        }).encode("utf-8")

        req = urllib.request.Request(
            OLLAMA_EMBED_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("embedding")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
        return None


# ── Vector Store Operations ─────────────────────────────────────────────────

def add(note: str, category: str = "general") -> str:
    """
    Add a memory entry.
    Writes to both vector store (if available) and MEMORY.md backup.
    Returns confirmation string.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    doc_id = hashlib.sha256(f"{timestamp}:{note}".encode()).hexdigest()[:16]

    # Always write to MEMORY.md
    _append_to_markdown(note, timestamp, category)

    # Try vector store
    if _init_chroma() and _collection is not None:
        embedding = _get_embedding(note)
        if embedding:
            _collection.add(
                documents=[note],
                embeddings=[embedding],
                metadatas=[{
                    "category": category,
                    "timestamp": timestamp,
                    "source": "user",
                }],
                ids=[doc_id],
            )
            _persist()
            _update_meta("add", note)
            return f"Saved to memory (vector + backup)."
        else:
            _update_meta("add_md_only", note)
            return f"Saved to memory (backup only — embeddings unavailable)."
    else:
        _update_meta("add_md_only", note)
        return f"Saved to memory."


def query(message: str, top_k: int = 5) -> list[str]:
    """
    Retrieve top-K relevant memories for this message.
    Returns list of memory strings, most relevant first.
    Falls back to empty list if vector store unavailable.
    """
    if not _init_chroma() or _collection is None:
        return []

    embedding = _get_embedding(message)
    if not embedding:
        return []

    try:
        count = _collection.count()
        if count == 0:
            return []

        results = _collection.query(
            query_embeddings=[embedding],
            n_results=min(top_k, count),
        )

        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]

        formatted = []
        for doc, meta in zip(documents, metadatas):
            ts = meta.get("timestamp", "")
            cat = meta.get("category", "")
            prefix = f"[{ts}]" if ts else ""
            if cat and cat != "general":
                prefix += f" [{cat}]"
            formatted.append(f"{prefix} {doc}".strip())

        return formatted

    except Exception:
        return []


def load_for_context(message: str = "", max_chars: int = 3000) -> str:
    """
    Load memory for system prompt injection.

    If vector store is available: returns top-K relevant memories.
    If not: falls back to MEMORY.md (truncated).

    This is the function called by agent_loop to build the system prompt.
    """
    # Try vector retrieval first
    if message:
        relevant = query(message, top_k=8)
        if relevant:
            result = "\n".join(relevant)
            if len(result) > max_chars:
                result = result[:max_chars] + "\n[... more memories available]"
            return result

    # Fallback: MEMORY.md
    return _load_markdown_fallback(max_chars)


# ── MEMORY.md Operations ───────────────────────────────────────────────────

def _append_to_markdown(note: str, timestamp: str, category: str = "general") -> None:
    """Append a timestamped note to MEMORY.md."""
    try:
        MEMORY_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
        cat_tag = f" [{category}]" if category != "general" else ""
        line = f"- [{timestamp}]{cat_tag} {note}\n"
        with open(MEMORY_MD_PATH, "a") as f:
            f.write(line)
    except Exception:
        pass


def _load_markdown_fallback(max_chars: int = 3000) -> str:
    """Load MEMORY.md as fallback when vector store is unavailable."""
    try:
        if not MEMORY_MD_PATH.exists():
            return "(No memories saved yet.)"
        content = MEMORY_MD_PATH.read_text().strip()
        if not content:
            return "(No memories saved yet.)"
        if len(content) > max_chars:
            content = content[-max_chars:]
            # Find first complete line
            nl = content.find('\n')
            if nl > 0:
                content = content[nl + 1:]
            content = "[earlier memories truncated]\n" + content
        return content
    except Exception:
        return "(Memory file unreadable.)"


# ── Migration ──────────────────────────────────────────────────────────────

def migrate_from_markdown() -> dict:
    """
    One-time migration from MEMORY.md to vector store.
    Returns stats dict with count, errors, etc.
    """
    stats = {"total": 0, "migrated": 0, "skipped": 0, "errors": 0}

    if not MEMORY_MD_PATH.exists():
        return stats

    if not _init_chroma():
        stats["errors"] = 1
        return stats

    content = MEMORY_MD_PATH.read_text()
    lines = [l.strip() for l in content.splitlines() if l.strip().startswith("- [")]

    stats["total"] = len(lines)

    for line in lines:
        try:
            # Parse "- [2025-01-15 10:30] [category] note text"
            line = line.lstrip("- ").strip()

            # Extract timestamp
            ts_match = line.startswith("[")
            if ts_match:
                end = line.index("]")
                timestamp = line[1:end]
                rest = line[end + 1:].strip()
            else:
                timestamp = ""
                rest = line

            # Extract category if present
            category = "general"
            if rest.startswith("["):
                cat_end = rest.index("]")
                category = rest[1:cat_end]
                rest = rest[cat_end + 1:].strip()

            note = rest
            if not note:
                stats["skipped"] += 1
                continue

            # Generate ID
            doc_id = hashlib.sha256(f"{timestamp}:{note}".encode()).hexdigest()[:16]

            # Get embedding
            embedding = _get_embedding(note)
            if not embedding:
                stats["errors"] += 1
                continue

            # Add to ChromaDB
            _collection.add(
                documents=[note],
                embeddings=[embedding],
                metadatas=[{
                    "category": category,
                    "timestamp": timestamp,
                    "source": "migration",
                }],
                ids=[doc_id],
            )
            stats["migrated"] += 1

        except Exception:
            stats["errors"] += 1

    _persist()
    _update_meta("migration", json.dumps(stats))

    return stats


# ── Meta ───────────────────────────────────────────────────────────────────

def _persist():
    """Persist ChromaDB to disk."""
    try:
        if _client:
            _client.persist()
    except Exception:
        pass


def _update_meta(event: str, detail: str = ""):
    """Update memory metadata file."""
    try:
        meta = {}
        if META_PATH.exists():
            with open(META_PATH, "r") as f:
                meta = json.load(f)

        meta["last_event"] = event
        meta["last_event_detail"] = detail[:200]
        meta["last_updated"] = datetime.now().isoformat()
        meta["entry_count"] = meta.get("entry_count", 0) + (1 if event.startswith("add") else 0)

        with open(META_PATH, "w") as f:
            json.dump(meta, f, indent=2)
    except Exception:
        pass


# ── Status ─────────────────────────────────────────────────────────────────

def status() -> dict:
    """Return memory system status for diagnostics."""
    result = {
        "memory_md_exists": MEMORY_MD_PATH.exists(),
        "memory_md_lines": 0,
        "vector_store_available": False,
        "vector_store_count": 0,
        "ollama_available": False,
    }

    if MEMORY_MD_PATH.exists():
        try:
            result["memory_md_lines"] = len(MEMORY_MD_PATH.read_text().splitlines())
        except Exception:
            pass

    if _init_chroma() and _collection:
        result["vector_store_available"] = True
        try:
            result["vector_store_count"] = _collection.count()
        except Exception:
            pass

    # Quick Ollama check
    test_embed = _get_embedding("test")
    result["ollama_available"] = test_embed is not None

    return result
