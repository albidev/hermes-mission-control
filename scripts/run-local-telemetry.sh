#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Source env file so MISSION_CONTROL_TOKEN is available
if [[ -f "$SCRIPT_DIR/../.env" ]]; then
  set -a
  source "$SCRIPT_DIR/../.env"
  set +a
fi

if command -v python3 >/dev/null 2>&1 && python3 -c 'import psutil' >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif [[ -x "$HOME/.hermes/hermes-agent/venv/bin/python" ]] && "$HOME/.hermes/hermes-agent/venv/bin/python" -c 'import psutil' >/dev/null 2>&1; then
  PYTHON_BIN="$HOME/.hermes/hermes-agent/venv/bin/python"
else
  echo "[mission-control-local-telemetry] psutil non trovato."
  echo "Installa con uno di questi comandi:"
  echo "  python3 -m pip install psutil"
  echo "  ~/.hermes/hermes-agent/venv/bin/python -m pip install psutil"
  exit 1
fi

exec "$PYTHON_BIN" "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/server/local_telemetry_server.py"
