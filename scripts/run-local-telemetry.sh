#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Source env file so MISSION_CONTROL_TOKEN is available
if [[ -f "$SCRIPT_DIR/../.env" ]]; then
  set -a
  source "$SCRIPT_DIR/../.env"
  set +a
fi

# Prefer the core venv (Python 3.10+) — the system python3 (3.9) cannot
# import hermes_state, which now uses 3.10+ syntax (e.g. `str | object`).
# Only fall back to system python3 if the venv lacks psutil.
if [[ -x "$HOME/.hermes/hermes-agent/venv/bin/python" ]] && "$HOME/.hermes/hermes-agent/venv/bin/python" -c 'import psutil' >/dev/null 2>&1; then
  PYTHON_BIN="$HOME/.hermes/hermes-agent/venv/bin/python"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import psutil' >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "[mission-control-local-telemetry] psutil non trovato."
  echo "Installa con uno di questi comandi:"
  echo "  python3 -m pip install psutil"
  echo "  ~/.hermes/hermes-agent/venv/bin/python -m pip install psutil"
  exit 1
fi

# hermes_state.py lives in the core dir, not on the default sys.path when the
# server runs with cwd=apps/mission-control. Export the core dir so the
# telemetry server can open state.db (otherwise it falls back to the stale
# sessions.json index and shows ~11 sessions instead of the real count).
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$HOME/.hermes/hermes-agent"

exec "$PYTHON_BIN" "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/server/local_telemetry_server.py"
