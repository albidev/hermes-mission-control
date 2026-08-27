#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Source env file so MISSION_CONTROL_TOKEN is available.
# shellcheck disable=SC1091 # path is runtime-computed via SCRIPT_DIR
source "$SCRIPT_DIR/lib/env.sh"
load_mission_control_env

# Profile-aware Hermes home (mirrors server/hermes_paths.py, issue #12):
# HERMES_HOME env, then the sticky active profile, then ~/.hermes.
HERMES_ROOT="$(resolve_hermes_home)"

# Prefer the core venv (Python 3.10+) — the system python3 (3.9) cannot
# import hermes_state, which now uses 3.10+ syntax (e.g. `str | object`).
# Only fall back to system python3 if the venv lacks psutil.
if [[ -x "$HERMES_ROOT/hermes-agent/venv/bin/python" ]] && "$HERMES_ROOT/hermes-agent/venv/bin/python" -c 'import psutil' >/dev/null 2>&1; then
  PYTHON_BIN="$HERMES_ROOT/hermes-agent/venv/bin/python"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import psutil' >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "[mission-control-local-telemetry] psutil non trovato."
  echo "Installa con uno di questi comandi:"
  echo "  python3 -m pip install psutil"
  echo "  $HERMES_ROOT/hermes-agent/venv/bin/python -m pip install psutil"
  exit 1
fi

# hermes_state.py lives in the core dir, not on the default sys.path when the
# server runs with cwd=apps/mission-control. Export the core dir so the
# telemetry server can open state.db (otherwise it falls back to the stale
# sessions.json index and shows ~11 sessions instead of the real count).
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$HERMES_ROOT/hermes-agent"
# The server resolves Hermes state through the same profile-aware home.
export HERMES_HOME="${HERMES_HOME:-$HERMES_ROOT}"

# Curate (BDH candidate curation) — opt-in via the external env file. The public
# repo default stays OFF (MC_ENABLE_BDH_CURATOR unset → disabled).
[ -n "${MC_ENABLE_BDH_CURATOR:-}" ] && export MC_ENABLE_BDH_CURATOR

exec "$PYTHON_BIN" "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/server/local_telemetry_server.py"
