#!/usr/bin/env python3
"""
One-time migration from MEMORY.md to ChromaDB vector store.

Usage:
    python -m memory.memory_migrator

Safe to run multiple times — uses document IDs based on content hash,
so duplicate entries are silently skipped by ChromaDB.
"""

import sys
from pathlib import Path

# Ensure project root is on path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from memory.vector_store import migrate_from_markdown, status, MEMORY_MD_PATH


def main():
    print("Rout Memory Migrator")
    print("=" * 40)

    # Pre-flight check
    if not MEMORY_MD_PATH.exists():
        print(f"\nNo MEMORY.md found at {MEMORY_MD_PATH}")
        print("Nothing to migrate.")
        return

    line_count = len(MEMORY_MD_PATH.read_text().splitlines())
    print(f"\nSource: {MEMORY_MD_PATH}")
    print(f"Lines: {line_count}")

    # Check status
    st = status()
    print(f"\nPre-migration status:")
    print(f"  MEMORY.md lines: {st['memory_md_lines']}")
    print(f"  Vector store available: {st['vector_store_available']}")
    print(f"  Vector store entries: {st['vector_store_count']}")
    print(f"  Ollama available: {st['ollama_available']}")

    if not st["ollama_available"]:
        print("\n⚠️  Ollama is not available. Start it with: ollama serve")
        print("    Then pull the embedding model: ollama pull nomic-embed-text")
        print("    Migration requires Ollama for generating embeddings.")
        return

    if not st["vector_store_available"]:
        print("\n⚠️  ChromaDB is not available. Install it with:")
        print("    pip install chromadb")
        return

    print("\nMigrating...")
    stats = migrate_from_markdown()

    print(f"\nResults:")
    print(f"  Total entries found: {stats['total']}")
    print(f"  Successfully migrated: {stats['migrated']}")
    print(f"  Skipped (empty): {stats['skipped']}")
    print(f"  Errors: {stats['errors']}")

    # Post-migration status
    st = status()
    print(f"\nPost-migration status:")
    print(f"  Vector store entries: {st['vector_store_count']}")

    if stats["errors"] == 0 and stats["migrated"] > 0:
        print("\n✅ Migration complete!")
    elif stats["errors"] > 0:
        print(f"\n⚠️  Migration completed with {stats['errors']} errors.")
        print("    Safe to re-run — will only add missing entries.")
    else:
        print("\nNo entries to migrate.")


if __name__ == "__main__":
    main()
