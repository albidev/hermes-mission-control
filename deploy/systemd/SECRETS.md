# Mission Control — Secrets & Environment File Guide

This document explains how to create the runtime environment file for a
Mission Control deployment and how to obtain each secret it contains.

**The only secret-bearing artifact that may live in this repository is
`deploy/systemd/env.template`. No real secrets are ever committed.** The
template contains placeholders only. The real file lives outside the repo, on
the deployment host, at:

    ~/.config/mission-control/env

## Creating the environment file

On the deployment host (Linux/macOS alike):

```bash
install -d -m 700 "$HOME/.config/mission-control"
install -m 600 deploy/systemd/env.template "$HOME/.config/mission-control/env"
```

Then edit `~/.config/mission-control/env` and replace every
`change_me_*` placeholder and empty optional value.

Requirements enforced by the deployment (systemd user units and launcher
scripts):

| Requirement | Command |
|---|---|
| File location | `~/.config/mission-control/env` (outside the repo) |
| Ownership | the deploying user (the user that runs the services) |
| Permissions | `600` (`-rw-------`) |
| Directory permissions | `700` on `~/.config/mission-control` |

```bash
chown "$USER:$USER" ~/.config/mission-control/env
chmod 600 ~/.config/mission-control/env
```

Verify:

```bash
ls -l ~/.config/mission-control/env   # -rw------- 1 albi albi ...
```

## Why 600 and outside the repo

- The file contains bearer tokens and a VAPID private key — credentials that
  let anyone with read access impersonate the operator of the control plane.
- `600` ensures only the owning user can read it; the directory `700` keeps
  other local users from even listing it.
- Keeping it outside the repository makes it impossible to accidentally
  `git add`/commit secrets, and lets the same repo deploy to multiple hosts
  with different credentials.
- The repo's `.gitignore` additionally ignores `.env`, `.env.*` (except
  `.env.example`) so even a local env file at the repo root stays untracked.

## Every variable and how to obtain it

### Authentication

| Variable | Required | How to generate / obtain |
|---|---|---|
| `MISSION_CONTROL_TOKEN` | yes | `openssl rand -base64 32`. This is the bearer token for every `/api/local/*` telemetry endpoint. |
| `API_SERVER_KEY` | yes | Same value as `MISSION_CONTROL_TOKEN` (the dashboard API accepts it as fallback credential; the telemetry server accepts it when `MISSION_CONTROL_TOKEN` is unset). |
| `VITE_MISSION_CONTROL_TOKEN` | yes | Same value as `MISSION_CONTROL_TOKEN`. Used by the frontend to bootstrap auth into `localStorage` on first visit. |

All three should be the same random value for a single-host deployment. Treat
the value as a password: never log it, never commit it, rotate it with
`openssl rand -base64 32` if it leaks, and update all three keys together.

### Telemetry server (no secrets — operational knobs)

| Variable | Default | Purpose |
|---|---|---|
| `MISSION_CONTROL_LOCAL_TELEMETRY_HOST` | `0.0.0.0` | Bind address. `0.0.0.0` needed for Tailscale/LAN access; `127.0.0.1` for local-only. |
| `MISSION_CONTROL_LOCAL_TELEMETRY_PORT` | `8765` | Sidecar port. |
| `MISSION_CONTROL_DEV_HOSTS` | *(empty)* | Comma-separated Tailscale/LAN IPs allowed by the Vite dev server (`MISSION_CONTROL_ALLOWED_HOSTS` if set). |
| `MISSION_CONTROL_ALLOWED_ORIGIN` | *(empty)* | CORS allowlist; when empty the server mirrors the request Origin. |
| `MISSION_CONTROL_READ_ONLY` | *(empty)* | `1`/`true`/`yes` rejects mutating requests. |
| `MISSION_CONTROL_LOCAL_TELEMETRY_URL` | `http://127.0.0.1:8765` | Vite proxy target for `/api/local`. |
| `HERMES_DASHBOARD_URL` | `http://127.0.0.1:9119` | Vite proxy target for `/api` and `/api/ws` (dashboard API). |
| `MISSION_CONTROL_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Strict Vite `allowedHosts` list. |

### Web Push (optional)

| Variable | Required for push | How to generate / obtain |
|---|---|---|
| `MISSION_CONTROL_VAPID_PUBLIC_KEY` | yes | VAPID public key. |
| `MISSION_CONTROL_VAPID_PRIVATE_KEY` | yes | VAPID private key. |
| `MISSION_CONTROL_VAPID_CONTACT` | yes | `mailto:` or URL identifying you as the push sender. |

Generate a VAPID keypair with `pywebpush`:

```bash
python3 -c "from py_vapid import Vapid01; v=Vapid01(); v.generate_keys(); \
print('public :', v.public_key.decode()); \
print('private:', v.private_key.decode())"
```

If either VAPID key is missing, Web Push degrades gracefully to "disabled" —
the rest of Mission Control keeps working. The contact is required by push
services (Chrome/Firefox) at subscription time.

Optional push-proxy knobs (defaults point at the Vite dev server, which
proxies `/api/ws` to the dashboard API):

| Variable | Default |
|---|---|
| `MISSION_CONTROL_GATEWAY_WS_URL` | `ws://127.0.0.1:5174/api/ws` |
| `MISSION_CONTROL_GATEWAY_ROOT_URL` | `http://127.0.0.1:5174/api/gateway-root` |
| `MISSION_CONTROL_WS_RECONNECT_DELAY` | `5` (seconds) |

### BDH candidate curation (optional, opt-in)

| Variable | Default | Purpose |
|---|---|---|
| `MC_ENABLE_BDH_CURATOR` | *(unset → off)* | `1`/`true`/`yes` enables the Curate page and `/api/local/candidates*` endpoints. |
| `VB_CANDIDATES` | `~/.hermes/vault-brain/candidates` | Candidate payloads directory. |
| `VB_VAULT` | `~/Documents/Hermes` | Vault root the curator reads/writes. |
| `VB_QUARANTINE_DAYS` | `1` | Days a promoted candidate waits in quarantine before promotion. |

No secrets here, but paths should match the vault-brain installation on the
host.

## Where the file is consumed

- **systemd user units** (`deploy/systemd/*.service`): loaded via
  `EnvironmentFile=%h/.config/mission-control/env`.
- **Launcher scripts** (`scripts/run-local-telemetry.sh`): source
  `<repo>/.env` if present — for Linux deployments the systemd
  `EnvironmentFile` path above is the canonical source.

## Rotation checklist

1. `openssl rand -base64 32` → new `MISSION_CONTROL_TOKEN`.
2. Update `API_SERVER_KEY` and `VITE_MISSION_CONTROL_TOKEN` to the same value.
3. `chmod 600` / `chown` if the file was touched by a different user.
4. Restart services: `systemctl --user restart mission-control-*`.
5. Browsers with an old `localStorage` token will 401 until the new token is
   re-entered or the page is hard-refreshed with the new
   `VITE_MISSION_CONTROL_TOKEN` baked in.
