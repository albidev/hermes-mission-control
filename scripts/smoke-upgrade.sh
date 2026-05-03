#!/usr/bin/env bash
set -euo pipefail

API_BASE="${1:-${VITE_HERMES_API_BASE_URL:-http://127.0.0.1:9119/api}}"
TOKEN="${MISSION_CONTROL_TOKEN:-$(launchctl getenv MISSION_CONTROL_TOKEN 2>/dev/null || true)}"
if [[ -z "$TOKEN" ]]; then
  TOKEN="${API_SERVER_KEY:-$(launchctl getenv API_SERVER_KEY 2>/dev/null || true)}"
fi

PROXY_BASE="http://127.0.0.1:5174/api"

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

echo "[smoke] Running health checks..."
echo "[smoke] API base (direct): $API_BASE"
echo "[smoke] API base (proxy) : $PROXY_BASE"
if [[ -n "$TOKEN" ]]; then
  echo "[smoke] Auth token: present"
else
  echo "[smoke] Auth token: MISSING — tokenless tests only"
fi

echo ""
echo "--- Direct backend tests (:9119) ---"
request "$API_BASE/status" >/dev/null       && pass "status reachable"       || fail "status unreachable"
request "$API_BASE/tools/toolsets" >/dev/null && pass "toolsets reachable"     || fail "toolsets unreachable"
request "$API_BASE/skills" >/dev/null       && pass "skills reachable"       || fail "skills unreachable"
request "$API_BASE/config" >/dev/null       && pass "config reachable"       || fail "config unreachable"
request "$API_BASE/cron/jobs" >/dev/null    && pass "cron reachable"         || fail "cron unreachable"
request "$API_BASE/local/system" >/dev/null && pass "system reachable"       || fail "system unreachable"

echo ""
echo "--- Frontend proxy tests (:5174) ---"
request "$PROXY_BASE/status" >/dev/null       && pass "proxy status reachable"       || fail "proxy status unreachable"
request "$PROXY_BASE/tools/toolsets" >/dev/null && pass "proxy toolsets reachable"   || fail "proxy toolsets unreachable"
request "$PROXY_BASE/skills" >/dev/null       && pass "proxy skills reachable"       || fail "proxy skills unreachable"
request "$PROXY_BASE/config" >/dev/null       && pass "proxy config reachable"       || fail "proxy config unreachable"
request "$PROXY_BASE/cron/jobs" >/dev/null    && pass "proxy cron reachable"         || fail "proxy cron unreachable"
request "$PROXY_BASE/local/system" >/dev/null && pass "proxy system reachable"       || fail "proxy system unreachable"

echo ""
echo "[smoke] All tests passed."
