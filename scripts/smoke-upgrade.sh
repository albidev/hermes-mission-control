#!/usr/bin/env bash
set -euo pipefail

API_BASE="${1:-${VITE_HERMES_API_BASE_URL:-http://localhost:8642/api}}"
TOKEN="${MISSION_CONTROL_TOKEN:-${2:-}}"

pass() { echo "[OK] $1"; }
warn() { echo "[WARN] $1"; }
fail() { echo "[FAIL] $1"; exit 1; }

request() {
  local url="$1"
  local tmp
  tmp=$(mktemp)
  local code
  if [[ -n "$TOKEN" ]]; then
    code=$(curl -sS -m 8 -H "Authorization: Bearer $TOKEN" -o "$tmp" -w "%{http_code}" "$url" || echo "000")
  else
    code=$(curl -sS -m 8 -o "$tmp" -w "%{http_code}" "$url" || echo "000")
  fi
  echo "$code|$tmp"
}

echo "Mission Control smoke check"
echo "API_BASE=$API_BASE"
if [[ -n "$TOKEN" ]]; then
  echo "Token: provided"
else
  echo "Token: not provided"
fi

echo ""
echo "1) Capabilities endpoint"
cap_res=$(request "$API_BASE/mission-control/capabilities")
cap_code="${cap_res%%|*}"
cap_body="${cap_res#*|}"
if [[ "$cap_code" == "200" ]]; then
  if python3 - <<'PY' "$cap_body"
import json,sys
p=sys.argv[1]
with open(p,'r',encoding='utf-8') as f:
    data=json.load(f)
assert isinstance(data,dict)
assert 'trace' in data
print('ok')
PY
  then
    pass "capabilities reachable and parseable"
  else
    warn "capabilities 200 but payload not parseable"
  fi
elif [[ "$cap_code" == "404" ]]; then
  warn "capabilities endpoint missing (frontend fallback should handle this)"
elif [[ "$cap_code" == "401" ]]; then
  warn "capabilities unauthorized (set MISSION_CONTROL_TOKEN to validate authenticated path)"
else
  fail "capabilities endpoint unexpected status: $cap_code"
fi

rm -f "$cap_body"

echo ""
echo "2) Trace snapshot endpoint"
trace_res=$(request "$API_BASE/mission-control/agents/trace?limit=1")
trace_code="${trace_res%%|*}"
trace_body="${trace_res#*|}"
if [[ "$trace_code" == "200" ]]; then
  if python3 - <<'PY' "$trace_body"
import json,sys
p=sys.argv[1]
with open(p,'r',encoding='utf-8') as f:
    data=json.load(f)
if isinstance(data,dict) and any(k in data for k in ('events','trace','data','payload')):
    print('ok')
else:
    raise SystemExit(1)
PY
  then
    pass "trace endpoint returned compatible payload"
  else
    fail "trace endpoint 200 but incompatible payload shape"
  fi
elif [[ "$trace_code" == "401" ]]; then
  warn "trace endpoint unauthorized (token required)"
else
  fail "trace endpoint unexpected status: $trace_code"
fi
rm -f "$trace_body"

echo ""
echo "3) SSE stream probe (3s)"
stream_url="$API_BASE/mission-control/agents/trace/stream?interval=1.5&limit=50"
if [[ -n "$TOKEN" ]]; then
  stream_url="$stream_url&access_token=$TOKEN"
fi
set +e
stream_out=$(curl -sS -N -m 3 "$stream_url" 2>/dev/null)
set -e
if [[ -z "$stream_out" ]]; then
  warn "no SSE frames captured in 3s (may still be fine if idle, UI should fall back to polling)"
else
  pass "SSE endpoint produced frames"
fi

echo ""
pass "smoke completed"
