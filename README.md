# Hermes Mission Control

![Hermes Mission Control cover](docs/images/03d032f9-72e5-429d-be0f-1c26e64cd701.png)

Standalone operational dashboard for [Hermes](https://hermes-agent.nousresearch.com). It runs next to your Hermes agent, reads local telemetry, and gives you a cockpit for sessions, agents, usage, tools, skills, config, and logs.

## About

Mission Control is a local-first operator dashboard for Hermes. It combines a React/Vite frontend with a small Python telemetry sidecar, so the dashboard can run independently without coupling the open-source UI to Hermes core internals.

**Tags:** `hermes` · `mission-control` · `tldraw` · `whiteboard` · `agentic-ui` · `operations-dashboard` · `telemetry` · `react` · `typescript` · `vite` · `tailwindcss` · `python` · `local-first` · `self-hosted`

> **Satellite by design.** Mission Control has zero runtime dependency on the Hermes core backend. It talks to a small local telemetry sidecar (Python stdlib + psutil) on port `8765`.

## Features

### Operations dashboard

- Gateway/runtime health and system metrics
- Active model, fallback model, and agent status
- Sessions, agents, tools, skills, configuration, logs, and cron visibility
- Provider usage for Codex, Ollama, OpenRouter, and Nous Portal with quota/billing views
- Draggable dashboard widgets with persisted layout

### Kanban operations

- Multi-board task management backed by Hermes core `kanban_db`
- Eight workflow columns with rich task cards, priorities, IDs, ages, progress, and comments
- Drag-and-drop task movement with optimistic updates and rollback
- Board and task creation, archive/permanent deletion, comments, search, and filters
- Mobile-safe task and board creation flows
- See [docs/kanban.md](docs/kanban.md) for the complete Kanban feature and API reference

### Plugin system

- Abstract plugin architecture: self-contained modules register their own route, sidebar entry, and (future) backend endpoints
- Plugins appear in a dedicated **PLUGINS** section of the sidebar, below core navigation
- First plugin: **Curate** (nightly brain candidate approval), fully self-contained under `src/plugins/curate/`
- See [docs/plugins.md](docs/plugins.md) for the plugin contract and how to add a plugin

### Chat and agent workspace

- Streaming Chat with presence states, reasoning events, and completion recovery
- **Expanded Chat + tldraw Agent Mode**: session-bound whiteboard, authenticated bridge, screenshot-to-chat, agent actions, Mermaid import, board lints, exports, and mobile-safe persistence
- Responsive layout: side rail on desktop, drawer and bottom sheets on mobile

For Chat internals, see [docs/chat.md](docs/chat.md). For localization, see [docs/i18n.md](docs/i18n.md). For telemetry and provider usage, see [docs/telemetry.md](docs/telemetry.md).

## tldraw Agent Mode

Mission Control links the expanded Chat to a tldraw whiteboard using the current stable `sessionKey`. Hermes can read structured board context, receive a PNG screenshot in Chat, and apply validated actions back to the editable canvas through the authenticated local telemetry bridge.

- Session-bound persistence for shapes, pages, camera, and selection
- Agent modes: `draw`, `review`, `arrange`, `explain`
- Bridge protocol v2 with feature negotiation and transactional actions
- Action history surfaced in Chat
- PNG/SVG/JSON export and Mermaid flowchart import
- Mobile-safe open feedback and explicit close/unmount to keep iOS input responsive

For the Chat internals (WebSocket transport, presence pill, persistence, streaming & reasoning), see [docs/chat.md](docs/chat.md). See the [tldraw feature matrix](docs/tldraw-feature-matrix.md) and the [Mission Control tldraw Agent Mode vault note](https://github.com/albidev/hermes-vault/blob/main/wiki/concepts/mission-control-tldraw-agent-mode.md).

## Architecture

| Component | Path | Port | Stack |
|-----------|------|------|-------|
| Telemetry server | `server/local_telemetry_server.py` | `8765` | Python stdlib + psutil |
| Frontend | `src/` | `5174` | React + Vite + TypeScript + Tailwind |

All data flows through `/api/local/*` endpoints. In development, Vite proxies those requests to the telemetry server.

See [docs/telemetry.md](docs/telemetry.md) for the full telemetry overview, including the **provider-usage** pipeline (CodexBar/Nous Portal → normalized cache/API → gauges) and its troubleshooting.

See [docs/kanban.md](docs/kanban.md) for the Kanban board architecture, supported task/board operations, API endpoints, and desktop/mobile behavior. See [docs/tools.md](docs/tools.md) for the tool inventory source and discovery behavior.

## Quick start

From the repository root:

```bash
pnpm install
python3 -m pip install -r server/requirements.txt
pnpm dev:full
```

This starts:
- Vite UI on `http://localhost:5174`
- Telemetry server on `http://localhost:8765`

To run them separately:

```bash
pnpm dev:telemetry   # port 8765
pnpm dev             # port 5174
```

## Configuration

Create `./.env` from `.env.example`:

```bash
VITE_MISSION_CONTROL_LOCAL_API_BASE_URL=/api/local
VITE_MISSION_CONTROL_TOKEN=your_token
# Shared Hermes dashboard API endpoint; defaults to 127.0.0.1:9119
MISSION_CONTROL_DASHBOARD_HOST=127.0.0.1
MISSION_CONTROL_DASHBOARD_PORT=9119
# Optional explicit Vite proxy override; takes precedence over host/port
# HERMES_DASHBOARD_URL=http://127.0.0.1:9119
# Optional: comma-separated local/Tailscale hostnames or IPs
MISSION_CONTROL_DEV_HOSTS=
```

The bearer token is shared between the telemetry server and the UI. The telemetry server reads it from `.env` via the launcher script.
The dashboard API launcher and Vite proxy use the same dashboard host/port
variables. Empty values use the defaults; ports must be between 1 and 65535.

Operational scripts (`scripts/run-dashboard-api.sh`, `scripts/smoke-upgrade.sh`,
`scripts/reapply-core-mission-control-fixes.sh`) load configuration through
`scripts/lib/env.sh`: they read `<repo-root>/.env` by default, or the file
pointed to by `MISSION_CONTROL_ENV_FILE`, and otherwise fall back to the
already-exported environment. No `launchctl` lookup is used, so the same
scripts run identically on macOS and Linux.

### Hermes home & active profile resolution

Mission Control resolves all Hermes state (state DB, sessions, logs, skills,
config, cache, vault-brain candidates) through the **same profile-aware
Hermes home used by the running Hermes installation** — see
`server/hermes_paths.py` (and the bash twin `resolve_hermes_home` in
`scripts/lib/env.sh`). Precedence:

1. `HERMES_HOME` set and already profile-shaped (`<root>/profiles/<name>`) → used verbatim.
2. Sticky active profile (`<root>/active_profile` contains a name other than `default`) → `<root>/profiles/<name>`.
3. `HERMES_HOME` set (non profile-shaped) → used verbatim.
4. Platform default → `~/.hermes`.

This mirrors the Hermes core launcher exactly, so Mission Control keeps
reading the correct database, sessions, logs, skills, and configuration even
when Hermes runs from a non-default home or a named profile.

### Provider usage preferences

Provider usage visibility is local configuration. Use the external environment
file selected by `MISSION_CONTROL_ENV_FILE` (by default `~/.hermes/mission-control.env`):

```bash
MISSION_CONTROL_USAGE_PROVIDERS=codex,ollama,nous
```

The allowlist above hides OpenRouter. If the variable is unset or blank, all
built-in provider sources are enabled. To customize fields within a provider,
create `~/.hermes/mission-control-usage.json`:

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
read by the telemetry sidecar and do not modify Hermes Core or CodexBar.

## Linux (systemd --user)

On Linux the services are supervised by the systemd user session instead of
macOS launchd. Example units live in [`systemd/`](systemd/README.md) and the
full walkthrough (clean checkout, secrets, operations, health checks) is in
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
(outside the repository; see the runbook for a template). Services restart on
failure with a bounded rate and fail visibly when dependencies are missing.
Health checks: `scripts/check-mission-control-health.sh`.

`scripts/reapply-core-mission-control-fixes.sh` restarts the stack with
`systemctl --user restart` on Linux (macOS keeps its `launchctl` path, isolated
in `scripts/lib/restart-services.sh` and documented as macOS-only).

## Tailscale / LAN access

By default the telemetry server binds to loopback (`127.0.0.1`) and Vite serves locally. To expose Mission Control to Tailscale peers or your LAN you must opt in explicitly:

- Set `MISSION_CONTROL_LOCAL_TELEMETRY_HOST=0.0.0.0` (telemetry sidecar) — see `.env.example` and `docs/telemetry.md`.
- List the peer addresses in `MISSION_CONTROL_DEV_HOSTS` (Vite `allowedHosts`, e.g. `100.84.148.17,192.168.1.63`).
- Optionally harden CORS with `MISSION_CONTROL_ALLOWED_ORIGIN=http://<peer>:5174` so only that origin can read responses.

Both processes accept Tailscale peer IPs once configured. A reverse proxy (`tailscale serve`, Caddy, nginx) is the recommended alternative: it exposes the dashboard without widening the telemetry bind.

## Building

```bash
pnpm build
```

Static output lands in `dist/` and can be served by any static host.

## Testing

```bash
pnpm build
pnpm test
```

For a live telemetry sidecar, start `pnpm dev:telemetry` in one terminal, export `MISSION_CONTROL_TOKEN`, then run:

```bash
pnpm test:smoke
```

CI runs the frontend build and Python test suite on pushes and pull requests.

## Security notes

- Never commit `.env`.
- The telemetry server requires a bearer token for every `/api/local/*` request.
- Mission Control is a local tool: bind only to trusted networks.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Roadmap

Planned work, tracked as GitHub issues:

- None currently.

Completed:

- [#7 — i18n / UI localization](https://github.com/albidev/hermes-mission-control/issues/7) — shipped on `develop`; see the locale catalogs in `src/locales/` and the shared `I18nProvider`
- [#8 — Implement Kanban Board in Mission Control UI](https://github.com/albidev/hermes-mission-control/issues/8) — shipped on `develop`; see [docs/kanban.md](docs/kanban.md)

## License

MIT — see [LICENSE](LICENSE).
