#!/usr/bin/env bash
#
# restart-services.sh — cross-platform service management for Mission Control
# operational scripts.
#
# Linux (systemd user session):  systemctl --user
# macOS (launchd):               launchctl  — isolated here and documented as
#                                macOS-only, so Linux execution never touches it.
#
# Source this file from any script under scripts/:
#
#   # shellcheck source=lib/restart-services.sh
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/restart-services.sh"
#
# Service labels used by Mission Control:
#   ai.hermes.dashboard-api
#   ai.hermes.mission-control-telemetry
#   ai.hermes.mission-control

# Map a Mission Control service label to its systemd user unit name.
# systemd unit names cannot contain some characters; dots are allowed but
# the canonical units are installed as hermes-*.service (see
# systemd/README.md for the example units).
mc_systemd_unit() {
  local label="$1"
  case "$label" in
    ai.hermes.dashboard-api) printf 'hermes-dashboard-api.service' ;;
    ai.hermes.mission-control-telemetry) printf 'hermes-mission-control-telemetry.service' ;;
    ai.hermes.mission-control) printf 'hermes-mission-control.service' ;;
    *) printf '%s.service' "$label" ;;
  esac
}

# Print "linux", "darwin", or the uname value for other platforms.
mc_platform() {
  uname -s | tr '[:upper:]' '[:lower:]'
}

# Status check: exit 0 if the service is loaded/running, 1 otherwise.
# Never errors on missing tools — a missing supervisor is treated as "not loaded".
mc_service_status() {
  local label="$1"
  case "$(mc_platform)" in
    linux)
      local unit
      unit="$(mc_systemd_unit "$label")"
      if ! command -v systemctl >/dev/null 2>&1; then
        echo "[mission-control-services] systemctl not found; cannot check $unit" >&2
        return 1
      fi
      systemctl --user is-active --quiet "$unit"
      ;;
    darwin)
      # macOS-only: launchd supervision.
      launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1
      ;;
    *)
      echo "[mission-control-services] Unsupported platform $(mc_platform)" >&2
      return 1
      ;;
  esac
}

# Restart a service. On Linux uses `systemctl --user restart`; on macOS uses
# `launchctl kickstart -k`. If the service is not loaded (or the supervisor
# is unavailable), logs and skips — matching the old macOS-only behavior of
# not failing when the job is absent.
mc_restart_service() {
  local label="$1"
  case "$(mc_platform)" in
    linux)
      local unit
      unit="$(mc_systemd_unit "$label")"
      if ! command -v systemctl >/dev/null 2>&1; then
        echo "[mission-control-services][WARN] systemctl not found; cannot restart $unit on Linux" >&2
        return 0
      fi
      if systemctl --user is-active --quiet "$unit"; then
        echo "[mission-control-services] Restarting $unit"
        systemctl --user restart "$unit"
      else
        echo "[mission-control-services] systemd user unit $unit not active; skipping restart"
      fi
      ;;
    darwin)
      # macOS-only: launchd supervision.
      if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
        echo "[mission-control-services] Restarting $label"
        launchctl kickstart -k "gui/$(id -u)/$label"
      else
        echo "[mission-control-services] LaunchAgent $label not loaded; skipping restart"
      fi
      ;;
    *)
      echo "[mission-control-services][WARN] Unsupported platform $(mc_platform); cannot restart $label" >&2
      ;;
  esac
  return 0
}
