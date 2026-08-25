#!/bin/bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
OUTPUT_DIR="${MISSION_CONTROL_CACHE_DIR:-$HOME/.hermes/cache}"
OUTPUT="$OUTPUT_DIR/mission-control-provider-usage.json"
TMP="$OUTPUT.tmp.$$"
mkdir -p "$OUTPUT_DIR"

sanitize_provider() {
  local provider="$1"
  local src_flag=""
  # Ollama's API path exposes no usage data; must come back web dashboard (Chrome cookies).
  if [[ "$provider" == "ollama" ]]; then
    src_flag="--source web"
  fi
  local raw
  raw="$(codexbar usage --provider "$provider" $src_flag --json --no-color 2>/dev/null || true)"
  if [[ -z "$raw" ]]; then
    printf '{"provider":%s,"available":false,"source":"cli","error":"CodexBar returned no data."}' "$(jq -Rn --arg value "$provider" '$value')"
    return
  fi
  jq -c --arg provider "$provider" '
    .[] | select(.provider == $provider) |
    if .error then
      {provider: $provider, available: false, source: (.source // "cli"), error: ((.error.message // "Provider unavailable.") | tostring | .[0:240])}
    else
      {
        provider: $provider,
        available: true,
        source: (.source // "cli"),
        updatedAt: (.usage.updatedAt // null),
        primary: (.usage.primary // null | if . == null then null else {usedPercent, resetsAt, windowMinutes} end),
        secondary: (.usage.secondary // null | if . == null then null else {usedPercent, resetsAt, windowMinutes} end),
        tertiary: (.usage.tertiary // null | if . == null then null else {usedPercent, resetsAt, windowMinutes} end),
        pace: (.pace // null),
        openRouter: (if $provider == "openrouter" then (.usage.openRouterUsage // null | if . == null then null else {balance, totalCredits, totalUsage, keyUsageDaily, keyUsageWeekly, keyUsageMonthly, usedPercent} end) else null end),
        creditsRemaining: (if $provider == "codex" then (.credits.remaining // null) else null end),
        resetCreditsAvailable: (if $provider == "codex" then (.usage.codexResetCredits.availableCount // null) else null end)
      }
    end
  ' <<<"$raw" | head -n 1 || printf '{"provider":"%s","available":false,"source":"cli","error":"Invalid CodexBar response."}' "$provider"
}

codex_json="$(sanitize_provider codex)"
ollama_json="$(sanitize_provider ollama)"
openrouter_json="$(sanitize_provider openrouter)"

jq -cn \
  --argjson codex "$codex_json" \
  --argjson ollama "$ollama_json" \
  --argjson openrouter "$openrouter_json" \
  '{success: ([$codex, $ollama, $openrouter] | any(.available == true)), available: true, updatedAt: (now | todateiso8601), providers: [$codex, $ollama, $openrouter]}' > "$TMP"
mv -f "$TMP" "$OUTPUT"
