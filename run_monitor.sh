#!/bin/bash
set -euo pipefail

REPO_DIR="/Users/steve/Desktop/Project/NikonSale"
VENV_PY="$REPO_DIR/.venv/bin/python"

exec "$VENV_PY" "$REPO_DIR/monitor.py"
