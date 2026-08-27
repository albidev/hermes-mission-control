# Mission Control systemd --user units (Linux)

On Linux, Mission Control services are supervised by the systemd **user**
session (`systemctl --user`). These example units mirror the three macOS
LaunchAgents used on the reference setup.

## Install

1. Copy the units into your user unit directory:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp systemd/*.service ~/.config/systemd/user/
   ```

2. The units read the repository env file through `EnvironmentFile`.
   Point `Environment`/`WorkingDirectory`/`ExecStart` at your checkout and
   Hermes core as needed (paths shown are the default install).

   The telemetry unit inherits the loopback default (`127.0.0.1:8765`) unless
   you set `MISSION_CONTROL_LOCAL_TELEMETRY_HOST=0.0.0.0` in the env file.
   The Vite unit passes `--host 0.0.0.0` explicitly — remove it for a
   local-only box. If you harden CORS, add
   `MISSION_CONTROL_ALLOWED_ORIGIN=http://<host>:5174` to the env file so only
   that origin can read telemetry responses (see `docs/telemetry.md`).

3. Reload and enable:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now hermes-dashboard-api
   systemctl --user enable --now hermes-mission-control-telemetry
   systemctl --user enable --now hermes-mission-control
   ```

   If your session is headless (no login manager), enable lingering so user
   units survive logout:

   ```bash
   loginctl enable-linger "$USER"
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
systemctl --user status hermes-dashboard-api
systemctl --user restart hermes-mission-control-telemetry
journalctl --user -u hermes-dashboard-api -f
```

The restart path in `scripts/reapply-core-mission-control-fixes.sh` uses
`systemctl --user restart` on Linux. On macOS it keeps the launchd
`launchctl kickstart -k` path, isolated in the same library file and
documented as macOS-only.
