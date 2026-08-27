# Linux deployment runbook (systemd --user)

Mission Control runs on Linux as a set of `systemd --user` services. This
runbook takes a clean checkout of the repository and turns it into an
always-available stack with automatic startup, bounded restart-on-failure,
dependency ordering, and `journalctl` log inspection. No PM2, no root
daemons, no interactive terminal required.

It covers the three services plus the stack target:

| Unit | What it runs | Port |
|------|--------------|------|
| `hermes-dashboard-api.service` | Hermes core dashboard backend (`hermes_cli.main dashboard`) | `127.0.0.1:9119` |
| `hermes-mission-control-telemetry.service` | Mission Control telemetry sidecar | `127.0.0.1:8765` |
| `hermes-mission-control.service` | Vite frontend dev server | `0.0.0.0:5174` |
| `mission-control.target` | Group target for the three services above | — |

Startup ordering: `hermes-dashboard-api` → `hermes-mission-control-telemetry`
(reads Hermes state through the core checkout) → `hermes-mission-control`
(proxies `/api/local` to the telemetry sidecar and `/api` to the dashboard
API). The frontend unit declares `Wants=` both dependencies, so if either
dependency fails to start, the frontend still starts but its `/api` proxies
fail visibly — which is the intended behavior (see "Failure behavior").

## 1. Prerequisites

- A Linux machine (Ubuntu 22.04+/24.04 tested) with `systemd` ≥ 247 (user
  units and `loginctl enable-linger`).
- `node` ≥ 18 and `pnpm` (for the frontend).
- `python3` ≥ 3.10 and `psutil` (for the telemetry sidecar and the dashboard
  API).
- The Hermes core checkout, same layout as the reference install:
  `~/.hermes/hermes-agent` with a `venv` containing `psutil`.
- `curl` (used by the health check).

## 2. Clean checkout

```bash
git clone https://github.com/albidev/hermes-mission-control.git
cd hermes-mission-control
pnpm install          # frontend dependencies (node_modules)
python3 -m pip install -r server/requirements.txt   # telemetry sidecar deps
# Optional Web Push support (see .env.example and docs/telemetry.md):
python3 -m pip install -r server/requirements-push.txt
```

Verify the checkout builds and tests pass before deploying:

```bash
pnpm build
pnpm test
```

## 3. Environment file (secrets live OUTSIDE the repository)

The units read configuration through `EnvironmentFile=`. By default they
point at `~/.hermes/mission-control.env`; you can change that path inside
each unit. The env file is **not** the repository's `.env.example` and is
**never** committed. Create it with the token required by the telemetry
server:

```bash
install -m 600 /dev/null ~/.hermes/mission-control.env
cat > ~/.hermes/mission-control.env <<'EOF'
# Required: shared bearer token for the telemetry sidecar and the UI.
# Generate with: openssl rand -base64 32
MISSION_CONTROL_TOKEN=replace_with_a_random_token

# Optional: expose the telemetry sidecar beyond loopback (default is
# loopback-only). See .env.example and docs/telemetry.md for the full list.
# MISSION_CONTROL_LOCAL_TELEMETRY_HOST=0.0.0.0
# MISSION_CONTROL_ALLOWED_ORIGIN=http://<host>:5174
# MISSION_CONTROL_VAULT_PATH=/path/to/vault
EOF
chmod 600 ~/.hermes/mission-control.env
```

The file holds the only secret in the deployment. `EnvironmentFile=-...`
(leading `-`) means a missing file is not fatal: the units start anyway and
fail only when a required value is absent at runtime.

## 4. Install the units

```bash
mkdir -p ~/.config/systemd/user
cp systemd/*.service systemd/*.target ~/.config/systemd/user/
systemctl --user daemon-reload
```

If your checkout lives somewhere other than `~/Projects/hermes-mission-control`,
edit the copied units: `WorkingDirectory`, `ExecStart` paths, and
`EnvironmentFile` (three units under `~/.config/systemd/user/`). The Hermes
core path default is `~/.hermes/hermes-agent` with a `venv` inside it; if
your core checkout differs, adjust `WorkingDirectory`/`ExecStart` in
`hermes-dashboard-api.service` and `hermes-mission-control-telemetry.service`.

Make sure the units parse:

```bash
systemctl --user cat hermes-mission-control-telemetry   # shows the loaded unit
systemctl --user show hermes-mission-control-telemetry -p FragmentPath
```

## 5. Start, stop, restart, status

Enable the target so the whole stack starts at login (and with `enable-linger`,
stays up after logout):

```bash
systemctl --user enable mission-control.target
loginctl enable-linger "$USER"        # keep user units running headless
systemctl --user start mission-control.target
```

Operate the stack:

```bash
systemctl --user status  mission-control.target            # whole stack
systemctl --user restart mission-control.target            # restart everything
systemctl --user stop    mission-control.target            # stop everything

# Or one service at a time:
systemctl --user status  hermes-mission-control-telemetry
systemctl --user restart hermes-dashboard-api
systemctl --user stop    hermes-mission-control
```

Because every service declares `PartOf=mission-control.target`, stopping the
target stops all three. Stopping a single service does not stop the target —
`PartOf` propagation is one-directional, so you can take one service down
without collapsing the rest of the stack.

## 6. Logs

All services log to the user journal:

```bash
journalctl --user -u hermes-mission-control-telemetry -f    # follow
journalctl --user -u hermes-dashboard-api --since "1 hour ago"
journalctl --user -u hermes-mission-control --no-pager
journalctl --user -u mission-control.target                 # boot/stop events
```

## 7. Health checks

The stack ships `scripts/check-mission-control-health.sh`. It verifies:

1. TCP reachability of the telemetry sidecar and the dashboard API on their
   configured hosts/ports;
2. `/health` returns `200`;
3. an unauthenticated `/api/local/*` request is rejected with `401` (token
   gate active);
4. an authenticated `/api/local/*` request returns `200` (token accepted).

```bash
# Load the env file so the token is available:
set -a; source ~/.hermes/mission-control.env; set +a
bash scripts/check-mission-control-health.sh
```

For an automatic watchdog, run it from a `systemd --user` timer (or cron):

```bash
cat > ~/.config/systemd/user/mission-control-healthcheck.service <<'EOF'
[Unit]
Description=Mission Control health check

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'set -a; source %h/.hermes/mission-control.env; set +a; exec %h/Projects/hermes-mission-control/scripts/check-mission-control-health.sh --quiet'
EOF
cat > ~/.config/systemd/user/mission-control-healthcheck.timer <<'EOF'
[Unit]
Description=Run Mission Control health check every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now mission-control-healthcheck.timer
journalctl --user -u mission-control-healthcheck.service --since "10 min ago"
```

The health check is network-only and does not require systemd, so the same
script also works on macOS for the equivalent launchd deployment.

## 8. Failure behavior

- **Restart on failure, bounded.** Every service uses `Restart=on-failure`
  with `RestartSec=5` and `StartLimitIntervalSec=5min` /
  `StartLimitBurst=10`. Transient crashes restart; a permanently broken
  service (missing core checkout, port conflict, bad env file) stops after
  10 attempts in 5 minutes and lands in a visible `failed` state instead of
  restart-looping forever.
- **Dependencies fail visibly.** The frontend starts even if a backend
  dependency is down (`Wants=`, not `Requires=`); its `/api` proxies then
  fail, and `systemctl --user status hermes-mission-control` shows the
  `WantedBy` dependency failures. If you prefer hard dependency semantics —
  the frontend refuses to start unless both backends are up — change the
  `Wants=` line in `hermes-mission-control.service` to `Requires=`.
- **No PM2.** Supervision, restart policy, ordering, and log rotation are all
  handled by systemd. Nothing calls `pm2 save` or `pm2 startup`.

## 9. Uninstall

```bash
systemctl --user stop mission-control.target
systemctl --user disable mission-control.target
rm ~/.config/systemd/user/mission-control.target \
   ~/.config/systemd/user/hermes-dashboard-api.service \
   ~/.config/systemd/user/hermes-mission-control-telemetry.service \
   ~/.config/systemd/user/hermes-mission-control.service
systemctl --user daemon-reload
```

## 10. Reference (macOS)

The equivalent macOS deployment uses launchd LaunchAgents
(`ai.hermes.dashboard-api`, `ai.hermes.mission-control-telemetry`,
`ai.hermes.mission-control`). The operational scripts under `scripts/`
(`scripts/lib/restart-services.sh`, `scripts/reapply-core-mission-control-fixes.sh`)
detect the platform and use `systemctl --user` on Linux and `launchctl` on
macOS.
