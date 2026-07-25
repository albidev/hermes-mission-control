#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${MISSION_CONTROL_TELEMETRY_URL:-http://127.0.0.1:8765}"
TOKEN="${MISSION_CONTROL_TOKEN:-}"

if [[ -z "$TOKEN" ]]; then
  echo "MISSION_CONTROL_TOKEN is required" >&2
  exit 2
fi

health_status="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/health")"
[[ "$health_status" == "200" ]] || { echo "health failed: HTTP $health_status" >&2; exit 1; }

unauth_status="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/api/local/system")"
[[ "$unauth_status" == "401" ]] || { echo "unauthenticated request was not rejected: HTTP $unauth_status" >&2; exit 1; }

auth_status="$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/local/system")"
[[ "$auth_status" == "200" ]] || { echo "authenticated request failed: HTTP $auth_status" >&2; exit 1; }

echo "Telemetry smoke test passed ($BASE_URL)"
