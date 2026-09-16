#!/usr/bin/env bash
#
# env.sh — shared environment loader for Mission Control operational scripts.
#
# Loads configuration from an explicit env file (default: ~/.hermes/mission-control.env,
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
# Priority: $MISSION_CONTROL_ENV_FILE, then ~/.hermes/mission-control.env.
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
  default_env="$HOME/.hermes/mission-control.env"
  if [[ -f "$default_env" ]]; then
    printf '%s' "$default_env"
  fi
}

# Resolve the profile-aware Hermes home (mirrors server/hermes_paths.py and
# the Hermes core launcher, issue #12).
#
# Mission Control is a SERVICE with a single identity: the default Hermes home.
# Profiles are not ambient state here — they enter only as an explicit scope (a
# selected bot, a room roster, an explicitly requested profile). The sticky
# active_profile is the interactive CLI's "where I am working now" marker;
# honouring it would silently move a serving process (state DB, cron store,
# vault-brain candidates, credentials) to whatever profile the user last
# selected in a terminal. The core draws the same line for the same reason
# (hermes_cli/main.py, _under_gateway_supervisor).
#
# Precedence (must match server/hermes_paths.get_hermes_home):
#   1. $HERMES_HOME already profile-shaped (<root>/profiles/<name>) -> verbatim
#   2. $HERMES_HOME set (non profile-shaped) -> verbatim
#   3. Platform default -> ~/.hermes
#
# The function always prints a path and never fails; callers that need the
# directory to exist should check it themselves.
resolve_hermes_home() {
  printf '%s' "${HERMES_HOME:-$HOME/.hermes}"
}

# Load environment variables from the env file (if any), exporting them.
# Never errors; falls back to the already-exported environment.
load_mission_control_env() {
  local env_file
  env_file="$(mc_env_file)"
  if [[ -z "$env_file" ]]; then
    echo "[mission-control-env] No env file found; using exported environment only" >&2
    echo "[mission-control-env] Set MISSION_CONTROL_ENV_FILE or create $HOME/.hermes/mission-control.env to configure" >&2
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
