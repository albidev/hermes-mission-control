# Mission Control systemd --user units (Linux)

On Linux, Mission Control services are supervised by the systemd **user**
session (`systemctl --user`). These example units mirror the three macOS
LaunchAgents used on the reference setup, plus a `mission-control.target`
that groups them.

| Unit | What it runs | Port |
|------|--------------|------|
| `hermes-dashboard-api.service` | Hermes core dashboard backend | `127.0.0.1:9119` |
| `hermes-mission-control-telemetry.service` | Telemetry sidecar | `127.0.0.1:8765` |
| `hermes-mission-control.service` | Vite frontend dev server | `0.0.0.0:5174` |
| `mission-control.target` | Group target for the three services | — |

Startup ordering is `hermes-dashboard-api` → `hermes-mission-control-telemetry`
→ `hermes-mission-control`. Every service restarts on failure with a bounded
rate (`StartLimitIntervalSec=5min` / `StartLimitBurst=10`), so a broken
dependency fails the unit visibly instead of restart-looping.

For the full walkthrough — clean checkout, secrets, start/stop/restart,
logs, health checks, failure behavior, uninstall — read
[`docs/runbooks/linux-deployment.md`](../docs/runbooks/linux-deployment.md).

## Install

```bash
mkdir -p ~/.config/systemd/user
cp systemd/*.service systemd/*.target ~/.config/systemd/user/
systemctl --user daemon-reload
```

The units read configuration through `EnvironmentFile=`, defaulting to
`~/.hermes/mission-control.env` (adjust inside the unit files if you keep it
elsewhere). Secrets and environment values live in that file — outside the
repository, never committed. See the runbook section "Environment file" for
a template.

The telemetry unit inherits the loopback default (`127.0.0.1:8765`) unless
you set `MISSION_CONTROL_LOCAL_TELEMETRY_HOST=0.0.0.0` in the env file. The
Vite unit passes `--host 0.0.0.0` explicitly — remove it for a local-only
box. If you harden CORS, add `MISSION_CONTROL_ALLOWED_ORIGIN=http://<host>:5174`
to the env file so only that origin can read telemetry responses (see
`docs/telemetry.md`).

Start the whole stack (enable = start at login; pair with
`loginctl enable-linger "$USER"` for headless boxes):

```bash
systemctl --user enable mission-control.target
systemctl --user start mission-control.target
```

Or start services individually:

```bash
systemctl --user enable --now hermes-dashboard-api
systemctl --user enable --now hermes-mission-control-telemetry
systemctl --user enable --now hermes-mission-control
```

## Operations

Operational scripts (see `scripts/lib/restart-services.sh`) map the Mission
Control service labels to these units:

| Label | Unit |
|-------|------|
| `ai.hermes.dashboard-api` | `hermes-dashboard-api.service` |
| `ai.hermes.mission-control-telemetry` | `hermes-mission-control-telemetry.service` |
| `ai.hermes.mission-control` | `hermes-mission-control.service` |

Manual equivalents:

```bash
systemctl --user status mission-control.target
systemctl --user restart hermes-mission-control-telemetry
systemctl --user stop mission-control.target
journalctl --user -u hermes-dashboard-api -f
```

Health checks: `scripts/check-mission-control-health.sh` probes the telemetry
and dashboard ports, `/health`, and the authenticated endpoint. See the
runbook section "Health checks" for the watchdog timer setup.

The restart path in `scripts/reapply-core-mission-control-fixes.sh` uses
`systemctl --user restart` on Linux. On macOS it keeps the launchd
`launchctl kickstart -k` path, isolated in the same library file and
documented as macOS-only.
