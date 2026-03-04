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
import threading
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ── Config ──────────────────────────────────────────────────────────────────

MEMORY_DIR = Path.home() / ".openclaw" / "memory"
MEMORY_MD_PATH = MEMORY_DIR / "MEMORY.md"
CHROMA_DB_PATH = MEMORY_DIR / "chroma_db"
META_PATH = MEMORY_DIR / "memory_meta.json"
ERROR_LOG_PATH = Path.home() / ".openclaw" / "logs" / "vector_store_errors.log"

EMBED_MODEL = "nomic-embed-text"
OLLAMA_EMBED_URL = "http://localhost:11434/api/embeddings"
COLLECTION_NAME = "rout_memory"
MAX_MEMORIES = 5000

# Ensure directory exists
MEMORY_DIR.mkdir(parents=True, exist_ok=True)
(Path.home() / ".openclaw" / "logs").mkdir(parents=True, exist_ok=True)


# ── ChromaDB Lazy Init ─────────────────────────────────────────────────────

_client = None
_collection = None
_chroma_available = None  # None = not checked, True/False = cached result
_global_lock = threading.Lock()


def _log_error(msg: str) -> None:
    """Append error message to error log. Never raises."""
    try:
        ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().isoformat()
        with open(ERROR_LOG_PATH, "a") as f:
            f.write(f"[{timestamp}] {msg}\n")
    except Exception:
        # Silent fallback — never propagate errors from logging
        pass


def _init_chroma():
    """Lazy-init ChromaDB client and collection. Thread-safe."""
    global _client, _collection, _chroma_available

    with _global_lock:
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
        except (ImportError, RuntimeError) as e:
            _log_error(f"ChromaDB init failed (import/runtime): {e}")
            log.info("ChromaDB not installed — using MEMORY.md fallback")
            _chroma_available = False
            return False
        except Exception as e:
            _log_error(f"ChromaDB init failed (unknown): {e}")
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
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError) as e:
        return None
    except Exception as e:
        _log_error(f"Ollama embedding failed: {e}")
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
        # Check collection size before adding
        try:
            with _global_lock:
                count = _collection.count()
                if count >= MAX_MEMORIES:
                    _log_error(f"Collection at capacity ({count}/{MAX_MEMORIES}), skipping vector add")
                    log.warning("Memory vector store at capacity (%d/%d)", count, MAX_MEMORIES)
                    _update_meta("add_md_only", note)
                    return f"Saved to memory (backup only — store at capacity)."
        except Exception as e:
            _log_error(f"Failed to check collection size: {e}")

        embedding = _get_embedding(note)
        if embedding:
            try:
                with _global_lock:
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
            except Exception as e:
                _log_error(f"Failed to add to ChromaDB: {e}")
                _update_meta("add_md_only", note)
                return f"Saved to memory (backup only — vector store error)."
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
    Falls back to keyword search on MEMORY.md if vector store unavailable.
    """
    # Try vector search first
    if _init_chroma() and _collection is not None:
        embedding = _get_embedding(message)
        if embedding:
            try:
                with _global_lock:
                    count = _collection.count()
                    if count > 0:
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

                        if formatted:
                            return formatted
            except (KeyError, TypeError, ValueError) as e:
                _log_error(f"Vector query parse error: {e}")
            except Exception as e:
                _log_error(f"Vector query failed: {e}")

    # Fallback: keyword search on MEMORY.md
    return _keyword_search(message, top_k)


def _keyword_search(message: str, top_k: int = 5) -> list[str]:
    """Search MEMORY.md by keyword overlap. Used when vector store is unavailable."""
    try:
        if not MEMORY_MD_PATH.exists():
            return []
        content = MEMORY_MD_PATH.read_text().strip()
        if not content:
            return []

        # Extract query keywords (3+ chars, lowercase)
        query_words = set(
            w.lower() for w in message.split()
            if len(w) >= 3 and w.lower() not in {
                "the", "and", "for", "are", "but", "not", "you", "all",
                "can", "had", "her", "was", "one", "our", "out", "has",
                "what", "when", "how", "who", "this", "that", "with",
                "from", "they", "been", "have", "does", "will", "about",
            }
        )
        if not query_words:
            return []

        # Score each line by keyword overlap
        scored = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line_lower = line.lower()
            hits = sum(1 for w in query_words if w in line_lower)
            if hits > 0:
                scored.append((hits, line))

        # Sort by hit count descending, take top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        return [line for _, line in scored[:top_k]]

    except (IOError, OSError) as e:
        _log_error(f"Keyword search failed: {e}")
        return []
    except Exception as e:
        _log_error(f"Keyword search unknown error: {e}")
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
    """Append a timestamped note to MEMORY.md. Atomic write using temp file."""
    try:
        MEMORY_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
        cat_tag = f" [{category}]" if category != "general" else ""
        line = f"- [{timestamp}]{cat_tag} {note}\n"

        # Atomic write: write to temp file then rename
        temp_path = MEMORY_MD_PATH.parent / f"{MEMORY_MD_PATH.name}.tmp"

        # Read existing content (if any) to preserve it
        existing = ""
        if MEMORY_MD_PATH.exists():
            try:
                existing = MEMORY_MD_PATH.read_text()
            except Exception as e:
                _log_error(f"Failed to read MEMORY.md for atomic write: {e}")
                # Fall back to append mode
                with open(MEMORY_MD_PATH, "a") as f:
                    f.write(line)
                return

        # Write to temp file
        with open(temp_path, "w") as f:
            f.write(existing)
            f.write(line)

        # Atomic rename
        os.replace(temp_path, MEMORY_MD_PATH)

    except (IOError, OSError) as e:
        _log_error(f"Failed to append to MEMORY.md: {e}")
    except Exception as e:
        _log_error(f"Unexpected error in _append_to_markdown: {e}")


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
    except (RuntimeError, AttributeError, OSError) as e:
        _log_error(f"Failed to load MEMORY.md fallback: {e}")
        return "(Memory file unreadable.)"
    except Exception as e:
        _log_error(f"Unexpected error loading MEMORY.md: {e}")
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

    try:
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

                # Add to ChromaDB (thread-safe)
                with _global_lock:
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

            except (ValueError, IndexError, KeyError) as e:
                _log_error(f"Migration line parse error: {e}")
                stats["errors"] += 1
            except Exception as e:
                _log_error(f"Migration line add failed: {e}")
                stats["errors"] += 1

        _persist()
        _update_meta("migration", json.dumps(stats))

    except (IOError, OSError) as e:
        _log_error(f"Migration read failed: {e}")
        stats["errors"] = 1
    except Exception as e:
        _log_error(f"Migration failed: {e}")
        stats["errors"] = 1

    return stats


# ── Meta ───────────────────────────────────────────────────────────────────

def _persist():
    """Persist ChromaDB to disk. Thread-safe."""
    try:
        with _global_lock:
            if _client:
                _client.persist()
    except Exception as e:
        _log_error(f"Failed to persist ChromaDB: {e}")


def _update_meta(event: str, detail: str = ""):
    """Update memory metadata file. Atomic write using temp file."""
    try:
        meta = {}
        if META_PATH.exists():
            with open(META_PATH, "r") as f:
                meta = json.load(f)

        meta["last_event"] = event
        meta["last_event_detail"] = detail[:200]
        meta["last_updated"] = datetime.now().isoformat()
        meta["entry_count"] = meta.get("entry_count", 0) + (1 if event.startswith("add") else 0)

        # Atomic write: write to temp file then rename
        temp_path = META_PATH.parent / f"{META_PATH.name}.tmp"
        with open(temp_path, "w") as f:
            json.dump(meta, f, indent=2)
        os.replace(temp_path, META_PATH)

    except (IOError, OSError, json.JSONDecodeError) as e:
        _log_error(f"Failed to update metadata: {e}")
    except Exception as e:
        _log_error(f"Unexpected error updating metadata: {e}")


# ── Status ─────────────────────────────────────────────────────────────────

def status() -> dict:
    """Return memory system status for diagnostics. Thread-safe."""
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
        except (IOError, OSError) as e:
            _log_error(f"Failed to read MEMORY.md for status: {e}")
        except Exception as e:
            _log_error(f"Unexpected error reading MEMORY.md for status: {e}")

    if _init_chroma() and _collection:
        result["vector_store_available"] = True
        try:
            with _global_lock:
                result["vector_store_count"] = _collection.count()
        except Exception as e:
            _log_error(f"Failed to get collection count for status: {e}")

    # Quick Ollama check
    test_embed = _get_embedding("test")
    result["ollama_available"] = test_embed is not None

    return result
