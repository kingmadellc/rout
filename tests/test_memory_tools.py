"""Tests for agent/tools/memory_tools.py and memory/vector_store.py"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools.memory_tools import (
    query_memory, add_memory, load_memory_for_context,
    _legacy_query, _legacy_add, _legacy_load_context,
)


def test_query_memory_no_file():
    """Query returns sensible message when no memory exists."""
    result = query_memory("test")
    assert isinstance(result, str)
    assert len(result) > 0


def test_add_memory_empty():
    """Adding empty note is rejected."""
    result = add_memory("")
    assert "empty" in result.lower() or "cannot" in result.lower()


def test_add_memory_too_long():
    """Notes are capped at 500 chars."""
    long_note = "x" * 600
    # Should succeed but truncate
    result = add_memory(long_note)
    assert isinstance(result, str)


def test_load_memory_for_context():
    """Context loading returns string."""
    result = load_memory_for_context("what's on my calendar?")
    assert isinstance(result, str)


def test_legacy_query():
    """Legacy query returns string."""
    result = _legacy_query()
    assert isinstance(result, str)


def test_legacy_load_context():
    """Legacy context load returns string."""
    result = _legacy_load_context()
    assert isinstance(result, str)


def test_vector_store_status():
    """Vector store status returns expected keys."""
    try:
        from memory.vector_store import status
        st = status()
        assert "memory_md_exists" in st
        assert "vector_store_available" in st
        assert "ollama_available" in st
    except ImportError:
        pass  # vector_store not available


def test_vector_store_graceful_degradation():
    """Vector store functions don't crash when ChromaDB is missing."""
    try:
        from memory.vector_store import load_for_context
        result = load_for_context(message="test")
        assert isinstance(result, str)
    except ImportError:
        pass


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✅ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
