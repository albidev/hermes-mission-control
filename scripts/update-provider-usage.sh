#!/bin/bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091 # path is runtime-computed via SCRIPT_DIR
source "$SCRIPT_DIR/lib/env.sh"
load_mission_control_env

# Provider usage is refreshed by the shared Python writer. It emits the same
# provider-agnostic contract consumed by the telemetry sidecar; Nous is fetched
# by telemetry itself from the already-authenticated Portal session.
exec python3 "$SCRIPT_DIR/update-provider-usage.py"
