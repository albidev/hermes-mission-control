#!/usr/bin/env bash
set -euo pipefail

API_BASE="${1:-${VITE_HERMES_API_BASE_URL:-http://127.0.0.1:9119/api}}"
TOKEN="${MISSION_CONTROL_TOKEN:-$(launchctl getenv MISSION_CONTROL_TOKEN 2>/dev/null || true)}"
if [[ -z "$TOKEN" ]]; then
  TOKEN="${API_SERVER_KEY:-$(launchctl getenv API_SERVER_KEY 2>/dev/null || true)}"
fi

pass() { echo "[OK] $1"; }
warn() { echo "[WARN] $1"; }
fail() { echo "[FAIL] $1"; exit 1; }

request() {
  local url="$1"
  local tmp code attempt
  tmp=$(mktemp)
  for attempt in {1..15}; do
    : > "$tmp"
    if [[ -n "$TOKEN" ]]; then
      code=$(curl -sS -m 8 -H "Authorization: Bearer $TOKEN" -o "$tmp" -w "%{http_code}" "$url" 2>/dev/null || echo "000")
    else
      code=$(curl -sS -m 8 -o "$tmp" -w "%{http_code}" "$url" 2>/dev/null || echo "000")
    fi
    if [[ "$code" != "000" ]]; then
      cat "$tmp"
      rm -f "$tmp"
      return 0
    fi
    sleep 1
  done
  cat "$tmp"
  rm -f "$tmp"
  return 1
}

echo "[smoke] API base: $API_BASE"
if [[ -n "$TOKEN" ]]; then
  echo "[smoke] auth token present"
else
  echo "[smoke] no auth token present"
fi

request "$API_BASE/status" >/dev/null && pass "status reachable" || fail "status unreachable"
request "$API_BASE/mission-control/agents" >/dev/null && pass "agents reachable" || fail "agents unreachable"
request "$API_BASE/mission-control/sessions?limit=3" >/dev/null && pass "sessions reachable" || fail "sessions unreachable"
request "$API_BASE/mission-control/agents/trace?limit=5&compact=1" >/dev/null && pass "trace reachable" || fail "trace unreachable"
