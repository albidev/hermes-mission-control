#!/usr/bin/env bash
set -euo pipefail

# Resolve the expected bind address exactly like the server does:
# canonical MISSION_CONTROL_LOCAL_TELEMETRY_* names win over the legacy
# TELEMETRY_BIND_* aliases, then the documented defaults (loopback:8765).
BIND_HOST="${MISSION_CONTROL_LOCAL_TELEMETRY_HOST:-${TELEMETRY_BIND_HOST:-127.0.0.1}}"
BIND_PORT="${MISSION_CONTROL_LOCAL_TELEMETRY_PORT:-${TELEMETRY_BIND_PORT:-8765}}"

BASE_URL="${MISSION_CONTROL_TELEMETRY_URL:-http://$BIND_HOST:$BIND_PORT}"
TOKEN="${MISSION_CONTROL_TOKEN:-}"

if [[ -z "$TOKEN" ]]; then
  echo "MISSION_CONTROL_TOKEN is required" >&2
  exit 2
fi

# Prove the configured bind host and port: connect to the exact address the
# server should be listening on, and — for loopback-only deployments — verify
# the server is NOT reachable through the primary LAN interface.
if ! python3 - "$BIND_HOST" "$BIND_PORT" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])

probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
probe.settimeout(3)
try:
    probe.connect((host, port))
except OSError as exc:
    print(f"bind check failed: cannot connect to {host}:{port}: {exc}", file=sys.stderr)
    sys.exit(1)
finally:
    probe.close()

if host in ("127.0.0.1", "localhost"):
    lan_ip = None
    try:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.connect(("8.8.8.8", 80))
        lan_ip = udp.getsockname()[0]
        udp.close()
    except OSError:
        pass
    if lan_ip and lan_ip not in ("127.0.0.1", "::1"):
        lan_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lan_probe.settimeout(2)
        try:
            lan_probe.connect((lan_ip, port))
        except OSError:
            print(f"bind check OK: loopback-only ({host}:{port}), LAN {lan_ip} refused")
        else:
            print(
                f"bind check FAILED: server reachable on LAN {lan_ip}:{port} despite loopback bind",
                file=sys.stderr,
            )
            sys.exit(1)
        finally:
            lan_probe.close()

print(f"bind check OK: listening on {host}:{port}")
PY
then
  exit 1
fi

health_status="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/health")"
[[ "$health_status" == "200" ]] || { echo "health failed: HTTP $health_status" >&2; exit 1; }

unauth_status="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/api/local/system")"
[[ "$unauth_status" == "401" ]] || { echo "unauthenticated request was not rejected: HTTP $unauth_status" >&2; exit 1; }

auth_status="$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/local/system")"
[[ "$auth_status" == "200" ]] || { echo "authenticated request failed: HTTP $auth_status" >&2; exit 1; }

echo "Telemetry smoke test passed ($BASE_URL)"
