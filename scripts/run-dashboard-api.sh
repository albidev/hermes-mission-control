#!/usr/bin/env bash
set -euo pipefail

# Start the Mission Control dashboard API (Hermes dashboard on :9119).
#
# Configuration:
#   - Token/config is loaded from an explicit env file (see scripts/lib/env.sh).
#     Default: ~/.hermes/mission-control.env, override with $MISSION_CONTROL_ENV_FILE.
#     No launchctl lookup — works identically on Linux and macOS.
#   - The dashboard must run from the Hermes core checkout. The core is
#     resolved profile-aware: $HERMES_HOME, then the sticky active profile,
#     then ~/.hermes (scripts/lib/env.sh resolve_hermes_home, issue #12).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091 # path is runtime-computed via SCRIPT_DIR
source "$SCRIPT_DIR/lib/env.sh"
load_mission_control_env

HERMES_ROOT="$(resolve_hermes_home)"
HERMES_AGENT_DIR="${HERMES_AGENT_DIR:-$HERMES_ROOT/hermes-agent}"
if [[ ! -d "$HERMES_AGENT_DIR" ]]; then
  echo "[run-dashboard-api][FAIL] Hermes core not found at $HERMES_AGENT_DIR" >&2
  echo "  Set HERMES_AGENT_DIR or HERMES_HOME, or run from the default install." >&2
  exit 1
fi

cd "$HERMES_AGENT_DIR"

export MISSION_CONTROL_TOKEN="${MISSION_CONTROL_TOKEN:-}"
export API_SERVER_KEY="${API_SERVER_KEY:-}"

exec ./venv/bin/python -m hermes_cli.main dashboard --host 127.0.0.1 --port 9119 --no-open
