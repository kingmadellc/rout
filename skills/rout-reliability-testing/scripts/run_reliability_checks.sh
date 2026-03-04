#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT_DIR"

export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/rout-pycache}"
mkdir -p "$PYTHONPYCACHEPREFIX"

echo "[1/2] Compile check"
python3 -m py_compile setup.py comms/imsg_watcher.py handlers/*.py config/*.py agent/*.py agent/tools/*.py sdk/*.py

echo "[2/2] Unit tests"
python3 -m unittest discover -s tests -p 'test_*.py'

echo "Reliability checks passed."
