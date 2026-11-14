#!/usr/bin/env bash
# Bootstrap the F1 relay on a fresh Debian/Ubuntu VPS. Idempotent. Does NOT start the relay.
# Usage: sudo bash vps-bootstrap.sh relay.example.com
set -euo pipefail
DOMAIN="${1:?usage: vps-bootstrap.sh <relay-domain>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

apt-get update -qq
apt-get install -y -qq python3 python3-venv ufw debian-keyring debian-archive-keyring apt-transport-https curl gpg

id rooms-relay >/dev/null 2>&1 || useradd --system --home /opt/rooms-relay --shell /usr/sbin/nologin rooms-relay
install -d -o rooms-relay -g rooms-relay -m 0755 /opt/rooms-relay
install -d -o rooms-relay -g rooms-relay -m 0700 /etc/rooms-relay /etc/rooms-relay/creds
for f in relay.py relay_daemon.py; do install -o rooms-relay -g rooms-relay -m 0644 "$SRC/$f" /opt/rooms-relay/; done
[[ -f /etc/rooms-relay/routes.json ]] || { echo '[]' > /etc/rooms-relay/routes.json; chown rooms-relay: /etc/rooms-relay/routes.json; chmod 600 /etc/rooms-relay/routes.json; }
sudo -u rooms-relay python3 -m venv /opt/rooms-relay/venv
sudo -u rooms-relay /opt/rooms-relay/venv/bin/pip install -q 'websockets>=13,<16'

# Caddy (official repo) terminates public TLS with an ACME certificate.
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy
fi
sed "s/{{DOMAIN}}/$DOMAIN/" "$HERE/Caddyfile" > /etc/caddy/Caddyfile
install -m 0644 "$HERE/rooms-relay.service" /etc/systemd/system/rooms-relay.service
systemctl daemon-reload

# Firewall: SSH + 443 only (80 stays closed: Caddy uses TLS-ALPN-01 on 443).
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 443/tcp
ufw --force enable
ufw status verbose
echo "bootstrap ok — provision the link, then: systemctl enable --now rooms-relay caddy"
