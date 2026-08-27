#!/usr/bin/env bash
#
# env.sh — shared environment loader for Mission Control operational scripts.
#
# Loads configuration from an explicit env file (default: <repo-root>/.env,
# override with $MISSION_CONTROL_ENV_FILE) and falls back to the already
# exported environment. Platform-neutral: no launchctl, no macOS-only tools.
#
# Source this file from any script under scripts/:
#
#   # shellcheck source=lib/env.sh
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/env.sh"
#   load_mission_control_env
#
# The loader never fails on a missing env file: it prints a warning to stderr
# and continues with whatever is already exported.

# Resolve the repository root from the location of this file
# (scripts/lib/env.sh -> repo root is two levels up).
mc_repo_root() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  printf '%s' "$(cd "$script_dir/../.." && pwd)"
}

# Echo the path of the env file to load, or nothing if none exists.
# Priority: $MISSION_CONTROL_ENV_FILE, then <repo-root>/.env.
mc_env_file() {
  if [[ -n "${MISSION_CONTROL_ENV_FILE:-}" ]]; then
    if [[ -f "$MISSION_CONTROL_ENV_FILE" ]]; then
      printf '%s' "$MISSION_CONTROL_ENV_FILE"
    else
      echo "[mission-control-env] MISSION_CONTROL_ENV_FILE set but not found: $MISSION_CONTROL_ENV_FILE" >&2
    fi
    return
  fi
  local default_env
  default_env="$(mc_repo_root)/.env"
  if [[ -f "$default_env" ]]; then
    printf '%s' "$default_env"
  fi
}

# Resolve the profile-aware Hermes home (mirrors server/hermes_paths.py and
# the Hermes core launcher, issue #12).
#
# Precedence:
#   1. $HERMES_HOME already profile-shaped (<root>/profiles/<name>) -> verbatim
#   2. Sticky active profile (<root>/active_profile != default) -> <root>/profiles/<name>
#   3. $HERMES_HOME set (non profile-shaped) -> verbatim
#   4. Platform default -> ~/.hermes
#
# The function always prints a path and never fails; callers that need the
# directory to exist should check it themselves.
resolve_hermes_home() {
  local env_home="${HERMES_HOME:-}"
  if [[ -n "$env_home" ]]; then
    if [[ "$(basename "$(dirname "$env_home")")" == "profiles" ]]; then
      printf '%s' "$env_home"
      return
    fi
  fi
  local root="${HERMES_HOME:-$HOME/.hermes}"
  local active=""
  if [[ -f "$root/active_profile" ]]; then
    active="$(tr -d '[:space:]' < "$root/active_profile" 2>/dev/null || true)"
  fi
  if [[ -n "$active" && "$active" != "default" ]]; then
    printf '%s' "$root/profiles/$active"
    return
  fi
  printf '%s' "$root"
}

# Load environment variables from the env file (if any), exporting them.
# Never errors; falls back to the already-exported environment.
load_mission_control_env() {
  local env_file
  env_file="$(mc_env_file)"
  if [[ -z "$env_file" ]]; then
    echo "[mission-control-env] No env file found; using exported environment only" >&2
    echo "[mission-control-env] Set MISSION_CONTROL_ENV_FILE or create $(mc_repo_root)/.env to configure" >&2
    return 0
  fi
  # shellcheck disable=SC1090 # dynamic path from mc_env_file()
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
  echo "[mission-control-env] Loaded env file: $env_file" >&2
}

# Error out if a required variable is unset or empty.
mc_require_var() {
  local name="$1"
  local description="${2:-$name}"
  if [[ -z "${!name:-}" ]]; then
    echo "[mission-control-env][FAIL] $description is required but not set (export it or add it to $(mc_env_file))" >&2
    return 1
  fi
  return 0
}
