# Hermes Mission Control

Standalone operational dashboard for [Hermes](https://hermes-agent.nousresearch.com). It runs next to your Hermes agent, reads local telemetry, and gives you a cockpit for sessions, agents, usage, tools, skills, config, and logs.

## About

Mission Control is a local-first operator dashboard for Hermes. It combines a React/Vite frontend with a small Python telemetry sidecar, so the dashboard can run independently without coupling the open-source UI to Hermes core internals.

**Tags:** `hermes` · `mission-control` · `operations-dashboard` · `telemetry` · `react` · `typescript` · `vite` · `tailwindcss` · `python` · `local-first` · `self-hosted`

> **Satellite by design.** Mission Control has zero runtime dependency on the Hermes core backend. It talks to a small local telemetry sidecar (Python stdlib + psutil) on port `8765`.

## What you get

- Gateway/runtime health and system metrics
- Active model, fallback model, and agent status
- Sessions, agents, usage, knowledge, tools, skills, config, logs routes
- Cron/job visibility and quick actions
- Responsive layout: side rail on desktop, drawer on mobile
- Draggable dashboard widgets with persisted layout

## Architecture

| Component | Path | Port | Stack |
|-----------|------|------|-------|
| Telemetry server | `server/local_telemetry_server.py` | `8765` | Python stdlib + psutil |
| Frontend | `src/` | `5174` | React + Vite + TypeScript + Tailwind |

All data flows through `/api/local/*` endpoints. In development, Vite proxies those requests to the telemetry server.

## Quick start

```bash
cd apps/mission-control
npm install
pip install -r server/requirements.txt
npm run dev:full
```

This starts:
- Vite UI on `http://localhost:5174`
- Telemetry server on `http://localhost:8765`

To run them separately:

```bash
npm run dev:telemetry   # port 8765
npm run dev             # port 5174
```

## Configuration

Create `apps/mission-control/.env` from `.env.example`:

```bash
VITE_MISSION_CONTROL_LOCAL_API_BASE_URL=/api/local
VITE_MISSION_CONTROL_TOKEN=your_token
MISSION_CONTROL_DEV_HOSTS=100.84.148.17
```

The bearer token is shared between the telemetry server and the UI. The telemetry server reads it from `.env` via the launcher script.

## Tailscale / LAN access

Vite listens on all interfaces (`host: true`) and `allowedHosts` is read from `MISSION_CONTROL_DEV_HOSTS`. The telemetry server binds to `0.0.0.0`. Both accept Tailscale peer IPs.

## Building

```bash
npm run build
```

Static output lands in `dist/` and can be served by any static host.

## Testing endpoints

```bash
export MISSION_CONTROL_TOKEN=your_token
bash scripts/smoke-test-telemetry.sh
```

## Security notes

- Never commit `.env`.
- The telemetry server requires a bearer token for every `/api/local/*` request.
- Mission Control is a local tool: bind only to trusted networks.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
