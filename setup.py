#!/usr/bin/env python3
"""Rout setup wizard — entry point. See setup/ for implementation."""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from setup import main

if __name__ == "__main__":
    main()
