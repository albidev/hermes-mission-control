#!/usr/bin/env bash
#
# check-mission-control-health.sh — health checks for a deployed Mission
# Control stack (see docs/runbooks/linux-deployment.md).
#
# Checks, in order:
#   1. TCP reachability of the telemetry sidecar and the dashboard API on
#      their configured hosts/ports (same resolution as the server: canonical
#      MISSION_CONTROL_LOCAL_TELEMETRY_* names, then legacy aliases, then
#      documented defaults).
#   2. The unauthenticated /health endpoint returns 200.
#   3. An unauthenticated /api/local/* request is rejected with 401
#      (proves the bearer token gate is active).
#   4. An authenticated /api/local/* request returns 200 (proves the token in
#      the environment file is accepted).
#
# Exit codes:
#   0  all checks passed
#   1  a check failed (service down, wrong bind, auth misconfigured)
#   2  usage/environment error (missing MISSION_CONTROL_TOKEN, bad args)
#
# Usage:
#   MISSION_CONTROL_TOKEN=... bash scripts/check-mission-control-health.sh
#   bash scripts/check-mission-control-health.sh --quiet
#
# The script never requires systemd: it probes the network endpoints, so it
# also works on macOS for the equivalent launchd deployment.

set -euo pipefail

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1

# Resolve the expected telemetry bind exactly like the server does:
# canonical MISSION_CONTROL_LOCAL_TELEMETRY_* names win over the legacy
# TELEMETRY_BIND_* aliases, then the documented defaults (loopback:8765).
BIND_HOST="${MISSION_CONTROL_LOCAL_TELEMETRY_HOST:-${TELEMETRY_BIND_HOST:-127.0.0.1}}"
BIND_PORT="${MISSION_CONTROL_LOCAL_TELEMETRY_PORT:-${TELEMETRY_BIND_PORT:-8765}}"

# Dashboard API (Hermes core) — same resolution as scripts/run-dashboard-api.sh.
DASHBOARD_HOST="${MISSION_CONTROL_DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${MISSION_CONTROL_DASHBOARD_PORT:-9119}"

BASE_URL="http://$BIND_HOST:$BIND_PORT"
DASHBOARD_URL="http://$DASHBOARD_HOST:$DASHBOARD_PORT"
TOKEN="${MISSION_CONTROL_TOKEN:-}"

fail() {
  echo "health check FAILED: $*" >&2
  exit 1
}

ok() {
  [[ "$QUIET" -eq 1 ]] || echo "  [OK] $*"
}

if [[ -z "$TOKEN" ]]; then
  echo "MISSION_CONTROL_TOKEN is required (export it or set it in the environment file)" >&2
  exit 2
fi

echo "health check: telemetry $BASE_URL, dashboard $DASHBOARD_URL"

# 1a. Telemetry port reachable.
if ! python3 - "$BIND_HOST" "$BIND_PORT" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(3)
try:
    sock.connect((host, port))
except OSError as exc:
    print(f"cannot connect to {host}:{port}: {exc}", file=sys.stderr)
    sys.exit(1)
finally:
    sock.close()
PY
then
  fail "telemetry server not reachable on $BIND_HOST:$BIND_PORT (is the service running?)"
fi
ok "telemetry port $BIND_HOST:$BIND_PORT reachable"

# 1b. Dashboard API port reachable.
if ! python3 - "$DASHBOARD_HOST" "$DASHBOARD_PORT" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(3)
try:
    sock.connect((host, port))
except OSError as exc:
    print(f"cannot connect to {host}:{port}: {exc}", file=sys.stderr)
    sys.exit(1)
finally:
    sock.close()
PY
then
  fail "dashboard API not reachable on $DASHBOARD_HOST:$DASHBOARD_PORT (is the hermes-dashboard-api service running?)"
fi
ok "dashboard API port $DASHBOARD_HOST:$DASHBOARD_PORT reachable"

# 2. Unauthenticated /health returns 200.
health_status="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/health")"
[[ "$health_status" == "200" ]] || fail "/health returned HTTP $health_status (expected 200)"
ok "/health returns 200"

# 3. Unauthenticated API request is rejected (token gate active).
unauth_status="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/api/local/system")"
[[ "$unauth_status" == "401" ]] || fail "unauthenticated /api/local/system returned HTTP $unauth_status (expected 401 — token gate not active?)"
ok "unauthenticated request rejected with 401"

# 4. Authenticated API request succeeds (token accepted).
auth_status="$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/local/system")"
[[ "$auth_status" == "200" ]] || fail "authenticated /api/local/system returned HTTP $auth_status (expected 200 — check MISSION_CONTROL_TOKEN in the environment file)"
ok "authenticated request returns 200"

echo "health check passed: Mission Control stack is up and authenticated"
