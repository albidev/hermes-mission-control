# Mission Control

![Mission Control cover](docs/images/mission-control-cover.png)

Standalone operational dashboard for [Hermes Agent](https://github.com/NousResearch/hermes-agent), the AI agent framework by [@NousResearch](https://github.com/NousResearch). It runs next to your Hermes agent, reads local telemetry, and gives you a cockpit for sessions, agents, usage, tools, skills, config, and logs.

## In action

- [Overview — desktop and mobile dashboard](docs/overview.md#in-action)
- [Group Rooms — multi-agent discussion](docs/rooms.md#in-action)
- [Chat — the same saved session on desktop and mobile](docs/chat.md#in-action)
- [Kanban — task boards, blockers, and planning deliverables](docs/kanban.md#in-action)

## About

Mission Control is a local-first operator dashboard for Hermes. It combines a React/Vite frontend with a small Python telemetry sidecar, and it is developed and released separately from Hermes.

### Releases

Mission Control uses [Semantic Versioning](https://semver.org/) in `package.json`. To publish a release, set its version to `MAJOR.MINOR.PATCH` (or a valid SemVer prerelease), merge that change, and push the matching `vMAJOR.MINOR.PATCH` tag. GitHub Actions runs CI first and creates the GitHub Release with generated notes only if the tag is valid SemVer and exactly matches `package.json`.

**Tags:** `hermes` · `mission-control` · `tldraw` · `whiteboard` · `agentic-ui` · `operations-dashboard` · `telemetry` · `react` · `typescript` · `vite` · `tailwindcss` · `python` · `local-first` · `self-hosted`

> **Satellite, not a fork.** Mission Control never modifies Hermes core files
> and ships no patches for it. It does rely on a few documented Hermes
> interfaces: the dashboard API (`hermes dashboard`, REST and the `/api/ws`
> JSON-RPC socket), the `hermes_cli.kanban_db` module for Kanban, the core cron
> module, and the Hermes home layout
> (`state.db`, `sessions/`, `logs/`, `skills/`, `config.yaml`). See
> [docs/runbooks/upgrade-compatibility.md](docs/runbooks/upgrade-compatibility.md)
> for the full list and what breaks when one of them changes.

## Features

### Operations dashboard

- Gateway/runtime health and system metrics
- Active model, fallback model, and agent status
- Sessions, agents, tools, skills, configuration, logs, and cron visibility
- Provider usage for Codex, Ollama, OpenRouter, and Nous Portal with quota/billing views
- Draggable dashboard widgets with persisted layout
- Browser terminal (PTY over WebSocket) for the machine running the sidecar

### Kanban operations

- Multi-board task management backed by Hermes core `kanban_db`
- Eight workflow columns with rich task cards, priorities, IDs, ages, progress, and comments
- Drag-and-drop task movement with optimistic updates and rollback
- Board and task creation, archive/permanent deletion, comments, search, and filters
- Mobile-safe task and board creation flows
- See [docs/kanban.md](docs/kanban.md) for the complete Kanban feature and API reference

### Plugin system

- External, self-contained plugins: backend, UI, manifest, and tests live in the plugin repository
- Plugins are installed with `git clone` into `~/.hermes/mc-plugins/<plugin-id>/`
- Run `scripts/setup-plugins.sh` to link installed plugin UIs for Vite; Mission Control contains no plugin implementation code
- Mission Control discovers plugin manifests and routes at runtime; an uninstalled plugin is invisible and does not affect the host
- See [docs/plugins.md](docs/plugins.md) for the plugin contract, install flow, and author requirements

#### Community plugins

Optional integrations maintained in separate repositories; these features are not bundled with Mission Control.

| Plugin | What it adds | Repository | Maintainer / credits |
|---|---|---|---|
| **Curate** | Review BDH session-synthesis and candidate proposals, with vault-aware approval and rejection. | [albidev/mc-curate-plugin](https://github.com/albidev/mc-curate-plugin) | [@albidev](https://github.com/albidev) |
| **Notifications** | A persistent notification inbox, cron-event ingestion, and notification delivery workflows. | [albidev/mc-notifications-plugin](https://github.com/albidev/mc-notifications-plugin) | [@albidev](https://github.com/albidev) |
| **Projects** | Local project, Git, and GitHub workspace monitoring. | [imbundle/mc-project-plugin](https://github.com/imbundle/mc-project-plugin) | [@imbundle](https://github.com/imbundle) |

For installation and the host contract, see [Mission Control plugins](docs/plugins.md). Check each plugin repository for its own requirements and setup instructions.

### Chat and agent workspace

- Streaming Chat with presence states, reasoning events, and completion recovery
- **Sessions picker** at the far right of the Chat/Rooms rail: search across available local profiles and origins, scrollable 25-row pages, Live by default, and one-click profile-safe resume. Background refresh keeps session selection enabled; opening the picker does not focus search or summon the mobile keyboard. See [Chat session picker](docs/chat.md#sessions-picker).
- **Bot Mode**: managed bot roster, canonical Bot Chat per bot, attributed handoffs
- **Group Rooms**: several bots on one request, with a shared timeline, per-member tool strips, driver controls (`stop` / `approve` / `retry` / `rename` / `disband`), and a cross-device last-room pointer
- **Expanded Chat + tldraw Agent Mode**: session-bound whiteboard, authenticated bridge, screenshot-to-chat, agent actions, Mermaid import, board lints, exports, and mobile-safe persistence
- Responsive layout: side rail on desktop, drawer and bottom sheets on mobile

Feature docs: [Overview](docs/overview.md) · [Chat](docs/chat.md) · [Bot Mode](docs/bot-mode.md) · [Group Rooms](docs/rooms.md) · [Localization](docs/i18n.md) · [Telemetry sidecar](docs/telemetry.md) · [Honcho identity and profile isolation](docs/honcho.md) · [Tools inventory](docs/tools.md) · [HTTP API](docs/api.md).

## tldraw Agent Mode

The whiteboard is built with the [tldraw SDK](https://github.com/tldraw/tldraw), maintained by [@tldraw](https://github.com/tldraw).

Mission Control links the expanded Chat to a tldraw whiteboard using the current stable `sessionKey`. Hermes can read structured board context, receive a PNG screenshot in Chat, and apply validated actions back to the editable canvas through the authenticated local telemetry bridge.

- Session-bound persistence for shapes, pages, camera, and selection
- Agent modes: `draw`, `review`, `arrange`, `explain`
- Bridge protocol v2 with feature negotiation and transactional actions
- Action history surfaced in Chat
- PNG/SVG/JSON export and Mermaid flowchart import
- Mobile-safe open feedback and explicit close/unmount to keep iOS input responsive

See the [tldraw feature matrix](docs/tldraw-feature-matrix.md).

## Architecture

| Component | Path | Default port | Stack |
|-----------|------|--------------|-------|
| Frontend (Vite dev server) | `src/` | `5174` | React + Vite + TypeScript + Tailwind |
| Telemetry sidecar | `server/local_telemetry_server.py` | `8765` | Python 3.10+, `psutil`, `PyYAML`, `websockets` |
| Terminal PTY WebSocket (started by the sidecar) | `server/terminal_server.py` | `8766` | `websockets` |
| Hermes dashboard API (part of Hermes) | `hermes dashboard`, launched by `scripts/run-dashboard-api.sh` | `9119` | Hermes core |

The Vite dev server proxies three paths:

- `/api/local/*` → telemetry sidecar (Mission Control's own API, see [docs/api.md](docs/api.md));
- `/api/terminal` → terminal WebSocket;
- every other `/api/*` (including the `/api/ws` JSON-RPC socket used by Chat, Bot Mode, and Group Rooms), plus `/login` and `/auth` → Hermes dashboard API.

Group Rooms use the gateway's `groups.*` JSON-RPC surface over `/api/ws`. The gateway owns room state; Mission Control owns only the last-room pointer, an optional room → vault routing map, and the read-only tool-trace collection. See [docs/rooms.md](docs/rooms.md).

## Requirements

- **Hermes** installed locally, with its checkout and virtual environment at
  `~/.hermes/hermes-agent` (override with `HERMES_HOME` or `HERMES_AGENT_DIR`).
  Without it the dashboard still loads system telemetry, but Chat, Rooms,
  Kanban, cron, and the tools inventory do not work.
- **Node.js >= 22.12** (`package.json` `engines`). The TypeScript test suites
  use Node's native type stripping. CI runs Node 22.
- **pnpm** (`packageManager: pnpm@10.33.2`).
- **Python >= 3.10** with `server/requirements.txt` (`psutil`, `PyYAML`,
  `websockets`). The sidecar launcher prefers the Hermes virtual environment
  (`~/.hermes/hermes-agent/venv/bin/python`) and falls back to `python3`.
- Optional: `server/requirements-push.txt` for Web Push.

## Quick start

```bash
git clone https://github.com/albidev/hermes-mission-control.git
cd hermes-mission-control
pnpm install

# Sidecar dependencies, into the interpreter that will run it
~/.hermes/hermes-agent/venv/bin/python -m pip install -r server/requirements.txt
# (or: python3 -m pip install -r server/requirements.txt)

# One config file for everything (see Configuration below)
cp .env.example .env
TOKEN="$(openssl rand -base64 32)"
sed -i.bak "s|your_token_here|$TOKEN|g" .env && rm .env.bak
export MISSION_CONTROL_ENV_FILE="$PWD/.env"

# Terminal 1: Hermes dashboard API on :9119 (needed by Chat, Rooms, sessions, cron)
scripts/run-dashboard-api.sh

# Terminal 2 (export MISSION_CONTROL_ENV_FILE there too): sidecar + UI
pnpm dev:full
```

Open `http://localhost:5174`. `pnpm dev:full` starts the telemetry sidecar
(`:8765`, plus the terminal socket on `:8766`) and the Vite UI (`:5174`). To run
them separately: `pnpm dev:telemetry` and `pnpm dev`.

If Hermes already runs its dashboard (`hermes dashboard`), you can skip
terminal 1, but that dashboard must accept the same token: start it with
`HERMES_DASHBOARD_SESSION_TOKEN` set to your `MISSION_CONTROL_TOKEN`, otherwise
the dashboard-backed views return `401`.

## Configuration

### Which file is read by whom

| Process | Reads |
|---------|-------|
| Vite (`pnpm dev`) | `./.env` in the repository root (plus the process environment) |
| Telemetry sidecar (`pnpm dev:telemetry`, `scripts/run-local-telemetry.sh`) | `$MISSION_CONTROL_ENV_FILE`, else `~/.hermes/mission-control.env`, else only the exported environment |
| Dashboard launcher (`scripts/run-dashboard-api.sh`) and smoke scripts | same as the sidecar |
| systemd units | `~/.hermes/mission-control.env` (`EnvironmentFile=`) |

The launcher scripts never read `./.env` on their own. You can either keep two
files (`./.env` for Vite, `~/.hermes/mission-control.env` for the rest) or use
one file by exporting `MISSION_CONTROL_ENV_FILE="$PWD/.env"`, as in the quick
start. `.env.example` lists every variable.

### Access token

One token protects everything:

| Variable | Read by | Notes |
|----------|---------|-------|
| `MISSION_CONTROL_TOKEN` | telemetry sidecar, terminal socket | Required. `API_SERVER_KEY` is accepted as a fallback name. |
| `VITE_MISSION_CONTROL_TOKEN` | browser bundle | Optional. Vite bakes it into the JavaScript, so anyone who can load the page can read it. Leave it empty when the UI is reachable from other machines and paste the token into the lock screen instead (stored in the browser's `localStorage`). |
| `HERMES_DASHBOARD_SESSION_TOKEN` | Hermes dashboard API | `scripts/run-dashboard-api.sh` sets it to `MISSION_CONTROL_TOKEN` unless it is already set. |

### Ports and hosts

| Variable | Default | Purpose |
|----------|---------|---------|
| `MISSION_CONTROL_LOCAL_TELEMETRY_HOST` / `_PORT` | `127.0.0.1` / `8765` | Sidecar bind address |
| `MISSION_CONTROL_TERMINAL_HOST` / `_PORT` | `127.0.0.1` / `8766` | Terminal socket bind (the Vite proxy always targets `127.0.0.1:8766`) |
| `MISSION_CONTROL_DASHBOARD_HOST` / `_PORT` | `127.0.0.1` / `9119` | Dashboard API, used by the launcher and the Vite proxy |
| `HERMES_DASHBOARD_URL` | — | Explicit Vite proxy target for the dashboard API |
| `MISSION_CONTROL_LOCAL_TELEMETRY_URL` | `http://127.0.0.1:8765` | Vite proxy target for the sidecar |
| `MISSION_CONTROL_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Vite `allowedHosts` base list |
| `MISSION_CONTROL_DEV_HOSTS` | — | Extra Vite `allowedHosts` (Tailscale/LAN names or IPs) |
| `MISSION_CONTROL_ALLOWED_ORIGIN` | — (mirror) | Restrict sidecar CORS to one origin |
| `MISSION_CONTROL_READ_ONLY` | off | `1` rejects every mutating sidecar request |

Ports must be between 1 and 65535; an invalid value aborts startup.

### Hermes home

Mission Control reads Hermes state from `HERMES_HOME` when it is set (a
profile-shaped path such as `~/.hermes/profiles/<name>` scopes it to that
profile), otherwise from `~/.hermes`. The sticky `active_profile` marker written
by `hermes profile use` is deliberately ignored: a running service must not
silently switch to whatever profile was last selected in a terminal. Details in
[docs/telemetry.md](docs/telemetry.md#hermes-home-and-profile-resolution).

### Provider usage preferences

```bash
MISSION_CONTROL_USAGE_PROVIDERS=codex,ollama,nous
```

The allowlist above hides OpenRouter. If the variable is unset or blank, all
built-in provider sources are enabled. To customize fields within a provider,
create `~/.hermes/mission-control-usage.json` (or point
`MISSION_CONTROL_USAGE_CONFIG_FILE` at another file):

```json
{
  "providers": {
    "codex": {
      "hidden": {
        "balances": ["credits_remaining"]
      },
      "featured": {
        "metrics": ["reset_credits_available"]
      }
    }
  }
}
```

`hidden` removes field IDs from the local telemetry response. `featured` gives
matching fields prominent rendering in the Overview card. These preferences are
read by the telemetry sidecar and do not modify Hermes or CodexBar.

## Running as a service

### macOS

There are no packaged launchd units. Run the stack in the foreground (quick
start above), or write your own LaunchAgents that run
`scripts/run-dashboard-api.sh`, `scripts/run-local-telemetry.sh`, and `pnpm dev`
from the repository root with `MISSION_CONTROL_ENV_FILE` in their environment.

### Linux (systemd --user)

Example units live in [`systemd/`](systemd/README.md) and the full walkthrough
(clean checkout, secrets, operations, health checks) is in
[`docs/runbooks/linux-deployment.md`](docs/runbooks/linux-deployment.md):

- `hermes-dashboard-api.service` — dashboard API (`:9119` by default; configurable)
- `hermes-mission-control-telemetry.service` — telemetry sidecar (`:8765`)
- `hermes-mission-control.service` — Vite frontend (`:5174`)
- `mission-control.target` — group target for the three services

```bash
mkdir -p ~/.config/systemd/user
cp systemd/*.service systemd/*.target ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable mission-control.target
systemctl --user start mission-control.target
# Optional: keep user units running after logout
loginctl enable-linger "$USER"
```

Secrets and environment values live in `~/.hermes/mission-control.env`
(outside the repository; template in
[`deploy/systemd/env.template`](deploy/systemd/env.template)). Services restart
on failure with a bounded rate and fail visibly when dependencies are missing.
Health checks: `scripts/check-mission-control-health.sh`.

After a Hermes update, follow
[docs/runbooks/upgrade-compatibility.md](docs/runbooks/upgrade-compatibility.md).

## Tailscale / LAN access

By default the telemetry sidecar binds to loopback (`127.0.0.1`). The Vite dev
server listens on all interfaces but only answers hosts in its allow-list. To
expose Mission Control to Tailscale peers or your LAN:

- List the peer hostnames or IPs in `MISSION_CONTROL_DEV_HOSTS` (for example
  `100.x.y.z,192.168.x.y`).
- Keep the sidecar on loopback: the browser reaches it through the Vite proxy.
  Set `MISSION_CONTROL_LOCAL_TELEMETRY_HOST=0.0.0.0` only if another machine
  must call the sidecar directly.
- Optionally restrict CORS with `MISSION_CONTROL_ALLOWED_ORIGIN=http://<host>:5174`.
- Leave `VITE_MISSION_CONTROL_TOKEN` empty (see [Access token](#access-token)).

A reverse proxy (`tailscale serve`, Caddy, nginx) in front of the frontend is
the recommended alternative. Remember that a valid token also opens the browser
terminal: treat it like a shell credential.

## Building

```bash
pnpm build
```

Static output lands in `dist/`. Serving `dist/` requires a reverse proxy that
reproduces the three `/api` routes listed in [Architecture](#architecture).

## Testing

What CI runs (`.github/workflows/ci.yml`):

```bash
# Frontend job (Node 22)
pnpm install --frozen-lockfile
pnpm typecheck
pnpm build
pnpm test:frontend        # every tests/*.test.{ts,mjs} file
pnpm test:vite-config
pnpm test:rooms
pnpm test:chat-profile
pnpm test:mobile-route-layout
pnpm test:navigation-palette
pnpm test:cron-model-selection
pnpm test:cron-schedule-input
pnpm test:system-health-ui

# Python job (3.11)
python -m pip install -r server/requirements.txt
python -m unittest discover -s tests -p 'test_*.py'
python -m unittest discover -s server/tests -p 'test_*.py'

# Paths job
bash scripts/check-documented-paths.sh
```

`pnpm test` runs both Python suites locally. `tests/test_kanban_bridge.py`
imports the Hermes core `kanban_db`; run it with the Hermes interpreter
(`~/.hermes/hermes-agent/venv/bin/python`) so the core's dependencies are
available.

For a live sidecar smoke test, start `pnpm dev:telemetry`, export
`MISSION_CONTROL_TOKEN`, then run `pnpm test:smoke`.

`pnpm test:navigation-palette-browser` runs the mounted-shell acceptance for the
global navigation palette (**not** part of CI: it needs Chrome and a Python venv
with `websockets`/`psutil`). It starts a loopback-only synthetic API fixture and
its own throwaway Chrome profile, drives real keystrokes against the production
`MissionControlShell` + `ChatDrawer`, and writes evidence (JSON + screenshots) to
`~/.hermes/cache/scratch/navigation-palette-browser`.

## Security notes

- Never commit `.env`.
- Every `/api/local/*` request needs the bearer token, except
  `GET /api/local/health` and `GET /health`.
- The token grants shell access through the browser terminal and can trigger
  state changes (cron, Kanban, skills install, gateway restart). See
  [SECURITY.md](SECURITY.md) and [docs/api.md](docs/api.md).
- Mission Control is a local tool: bind only to trusted networks.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
