#!/bin/bash
set -euo pipefail

cd "$HOME/.hermes/hermes-agent"

export MISSION_CONTROL_TOKEN="${MISSION_CONTROL_TOKEN:-$(launchctl getenv MISSION_CONTROL_TOKEN 2>/dev/null || true)}"
export API_SERVER_KEY="${API_SERVER_KEY:-$(launchctl getenv API_SERVER_KEY || true)}"

exec ./venv/bin/python -m hermes_cli.main dashboard --host 127.0.0.1 --port 9119 --no-open
