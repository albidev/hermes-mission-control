#!/usr/bin/env bash
set -euo pipefail

API_BASE="${1:-${VITE_HERMES_API_BASE_URL:-http://127.0.0.1:9119/api}}"
TOKEN="${MISSION_CONTROL_TOKEN:-$(launchctl getenv MISSION_CONTROL_TOKEN 2>/dev/null || true)}"

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
    if [[ "$code" != "000" && "$code" != "000000" ]]; then
      echo "$code|$tmp"
      return 0
    fi
    sleep 1
  done
  echo "$code|$tmp"
}

assert_json_endpoint() {
  local label="$1"
  local path="$2"
  local mode="${3:-dict_or_list}"
  local res code body
  res=$(request "$API_BASE$path")
  code="${res%%|*}"
  body="${res#*|}"

  if [[ "$code" != "200" ]]; then
    echo "--- body for $label ---" >&2
    sed -n '1,40p' "$body" >&2 || true
    rm -f "$body"
    fail "$label returned HTTP $code"
  fi

  if python3 - "$body" "$mode" <<'PY'
import json, sys
body, mode = sys.argv[1], sys.argv[2]
with open(body, 'r', encoding='utf-8') as f:
    data = json.load(f)
if mode == 'dict' and not isinstance(data, dict):
    raise SystemExit(1)
if mode == 'list' and not isinstance(data, list):
    raise SystemExit(1)
if mode == 'dict_or_list' and not isinstance(data, (dict, list)):
    raise SystemExit(1)
print('ok')
PY
  then
    pass "$label reachable and JSON"
  else
    echo "--- body for $label ---" >&2
    sed -n '1,40p' "$body" >&2 || true
    rm -f "$body"
    fail "$label returned non-compatible JSON"
  fi
  rm -f "$body"
}

assert_knowledge_sources() {
  local res code body
  res=$(request "$API_BASE/knowledge")
  code="${res%%|*}"
  body="${res#*|}"
  if [[ "$code" != "200" ]]; then
    rm -f "$body"
    fail "knowledge source check returned HTTP $code"
  fi
  if python3 - "$body" <<'PY'
import json, sys
with open(sys.argv[1], 'r', encoding='utf-8') as f:
    data = json.load(f)
core = []
for section in data.get('sections') or []:
    if section.get('id') == 'core-memory':
        core = section.get('items') or []
        break
paths = {item.get('path') for item in core}
sources = {item.get('sourcePath') for item in core}
required_paths = {'SOUL.md', 'USER.md', 'AGENTS.md'}
required_sources = {'~/.hermes/SOUL.md', '~/.hermes/USER.md', '~/.hermes/AGENTS.md'}
if not required_paths.issubset(paths):
    raise SystemExit(f'missing core docs: {sorted(required_paths - paths)}')
if not required_sources.issubset(sources):
    raise SystemExit(f'wrong core sources: {sorted(required_sources - sources)}')
if any(str(source).endswith('/memories/USER.md') for source in sources):
    raise SystemExit('USER.md is still sourced from ~/.hermes/memories')
if any(str(source).endswith('/hermes-agent/AGENTS.md') for source in sources):
    raise SystemExit('AGENTS.md is still sourced from hermes-agent repo')
PY
  then
    pass "knowledge core docs use ~/.hermes/SOUL.md, USER.md, AGENTS.md"
  else
    echo "--- body for knowledge source check ---" >&2
    sed -n '1,80p' "$body" >&2 || true
    rm -f "$body"
    fail "knowledge core docs are not sourced from ~/.hermes"
  fi
  rm -f "$body"
}

echo "Mission Control smoke check"
echo "API_BASE=$API_BASE"
if [[ -n "$TOKEN" ]]; then
  echo "Token: provided"
else
  echo "Token: not provided"
fi

echo ""
echo "1) Dashboard backend endpoints"
assert_json_endpoint "status" "/status" dict
assert_json_endpoint "toolsets" "/tools/toolsets" list
assert_json_endpoint "skills" "/skills" list
assert_json_endpoint "config" "/config" dict
assert_json_endpoint "cron jobs" "/cron/jobs" list
assert_json_endpoint "knowledge" "/knowledge" dict
assert_knowledge_sources

if [[ "$API_BASE" == *":5174/api"* ]]; then
  echo ""
  echo "2) Optional local telemetry through Mission Control proxy"
  telemetry_base="${API_BASE%/api}/api/local/system"
  tmp=$(mktemp)
  if [[ -n "$TOKEN" ]]; then
    code=$(curl -sS -m 8 -H "Authorization: Bearer $TOKEN" -o "$tmp" -w "%{http_code}" "$telemetry_base" || echo "000")
  else
    code=$(curl -sS -m 8 -o "$tmp" -w "%{http_code}" "$telemetry_base" || echo "000")
  fi
  if [[ "$code" == "200" ]] && python3 - "$tmp" <<'PY'
import json, sys
with open(sys.argv[1], 'r', encoding='utf-8') as f:
    data = json.load(f)
if not isinstance(data, dict):
    raise SystemExit(1)
PY
  then
    pass "local telemetry reachable and JSON"
  else
    warn "local telemetry unavailable or non-JSON at $telemetry_base (HTTP $code)"
  fi
  rm -f "$tmp"
fi

echo ""
pass "smoke completed"
