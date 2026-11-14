#!/usr/bin/env bash
# F1 evidence collector. Prints one JSON object; contains no secrets.
# Usage: collect-evidence.sh A|B|relay <relay-domain>
set -uo pipefail
ROLE="${1:?role A|B|relay}"; DOMAIN="${2:?relay domain}"
OS="$(uname -s)"

listeners() {
  if [[ $OS == Darwin ]]; then lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $1" "$9}' | sort -u
  else ss -Hltnp 2>/dev/null | awk '{print $4" "$6}' | sort -u; fi
}
nonloopback() { listeners | grep -vE '(127\.0\.0\.1|\[::1\]|localhost):' || true; }
json_lines() { python3 -c 'import json,sys;print(json.dumps([l.rstrip("\n") for l in sys.stdin if l.strip()]))'; }

EGRESS="$(curl -s -m 8 https://api.ipify.org || echo unknown)"
RELAY_IP="$(python3 -c "import socket,sys;print(socket.gethostbyname(sys.argv[1]))" "$DOMAIN" 2>/dev/null || echo unresolved)"
CERT="$(echo | openssl s_client -connect "$DOMAIN:443" -servername "$DOMAIN" 2>/dev/null | openssl x509 -noout -issuer -enddate 2>/dev/null | tr '\n' ' ')"
ESTAB="$(if [[ $OS == Darwin ]]; then lsof -nP -iTCP@"$RELAY_IP":443 -sTCP:ESTABLISHED 2>/dev/null | awk 'NR>1{print $1" "$9}'; else ss -Htnp state established "( dport = :443 )" dst "$RELAY_IP" 2>/dev/null | awk '{print $4" -> "$5" "$6}'; fi)"

EXTRA='{}'
if [[ $ROLE == relay ]]; then
  J="$(journalctl -u rooms-relay --since '-2h' --no-pager -o cat 2>/dev/null)"
  EXTRA="$(python3 - "$J" <<'PY'
import json, re, sys
journal = sys.argv[1]
print(json.dumps({
  "ufw": __import__("subprocess").run(["ufw", "status"], capture_output=True, text=True).stdout.splitlines(),
  "journal_lines": len(journal.splitlines()),
  "journal_events": sorted(set(re.findall(r'"event": ?"(\w+)"', journal))),
  "journal_contains_auth_scheme": "HermesRoom" in journal,
  "journal_contains_dispatch": "hosted_room_dispatch" in journal,
  "journal_contains_prompt_marker": "LAB_READY" in journal,
  "credential_files_left": __import__("os").listdir("/etc/rooms-relay/creds") if __import__("os").path.isdir("/etc/rooms-relay/creds") else [],
}))
PY
)"
fi

python3 - "$ROLE" "$EGRESS" "$RELAY_IP" "$CERT" "$(listeners | json_lines)" "$(nonloopback | json_lines)" "$(printf '%s\n' "$ESTAB" | json_lines)" "$EXTRA" <<'PY'
import json, platform, sys, time
role, egress, relay_ip, cert, listen, nonloop, estab, extra = sys.argv[1:]
print(json.dumps({
  "role": role, "collected_at": int(time.time()), "host": platform.node(), "os": platform.platform(),
  "egress_ip": egress, "relay_ip": relay_ip, "relay_cert": cert.strip(),
  "listeners": json.loads(listen), "non_loopback_listeners": json.loads(nonloop),
  "established_to_relay_443": json.loads(estab), **json.loads(extra),
}, indent=2))
PY
