# Contributing to Hermes Mission Control

Thanks for your interest in improving Mission Control.

## Scope

Mission Control is intentionally a **satellite application**. It lives under `apps/mission-control/` and must not modify Hermes core files (`hermes_cli/`, `gateway/`, `pyproject.toml`, etc.). All backend needs are served by the telemetry sidecar on port `8765`.

## Before you start

- Open an issue or discussion for large changes.
- Keep the core isolated: new data sources go into `server/local_telemetry_server.py`, not `hermes_cli/web_server.py`.
- Match the existing TypeScript/React patterns and Tailwind conventions.
- Run `npm run build` before submitting.

## Development

```bash
cd apps/mission-control
npm install
npm run dev:full
```

The telemetry server is a Python sidecar in `server/`. It does not hot-reload; restart it after backend changes.

## Pull requests

1. Keep commits focused and the diff minimal.
2. Do not include `.env`, tokens, or local paths.
3. Verify the dashboard still loads on both desktop and mobile widths.
