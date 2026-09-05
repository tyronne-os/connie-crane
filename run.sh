#!/usr/bin/env bash
# CRANE — start the local backend
set -euo pipefail

PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8000}"

echo "=== CRANE starting on http://127.0.0.1:${PORT} ==="

# Vault check (non-fatal — vault.py re-reads the file live)
if [ -f "$HOME/.config/nobility-depository/vault.json" ]; then
  echo "  vault ✅  ~/.config/nobility-depository/vault.json"
else
  echo "  vault ⚠️   ~/.config/nobility-depository/vault.json not found — credentials will be missing"
fi

# Dependencies
if ! $PYTHON -c "import fastapi, uvicorn" 2>/dev/null; then
  echo "  installing dependencies…"
  $PYTHON -m pip install -q -r requirements.txt
fi

exec $PYTHON -m uvicorn app:app --host 127.0.0.1 --port "$PORT" --reload
