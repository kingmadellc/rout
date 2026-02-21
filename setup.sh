#!/bin/bash
# setup.sh — convenience wrapper for setup.py
#
# This script simply delegates to the Python setup wizard.
# It's kept for compatibility; you can also run: python3 setup.py

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "Starting Rout setup wizard..."
echo ""

cd "$SCRIPT_DIR"
python3 setup.py

echo ""
echo "Setup wizard complete!"
echo ""
