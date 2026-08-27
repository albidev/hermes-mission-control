# Contributing to Hermes Mission Control

Thanks for your interest in improving Mission Control.

## Scope

Mission Control is a **standalone satellite application** in its own repository (this repo). It must not modify Hermes core files (`hermes_cli/`, `gateway/`, `pyproject.toml`, etc.). All backend needs are served by the telemetry sidecar on port `8765`.

## Before you start

- Open an issue or discussion for large changes.
- Keep the core isolated: new data sources go into `server/local_telemetry_server.py`, not `hermes_cli/web_server.py`.
- Match the existing TypeScript/React patterns and Tailwind conventions.
- Run `pnpm build` and `pnpm test` before submitting.

## Development

```bash
pnpm install
pnpm dev:full
```

The telemetry server is a Python sidecar in `server/`. It does not hot-reload; restart it after backend changes.

## Pull requests

1. Keep commits focused and the diff minimal.
2. Do not include `.env`, tokens, or local paths.
3. Verify the dashboard still loads on both desktop and mobile widths.
