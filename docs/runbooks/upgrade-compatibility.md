# Mission Control upgrade compatibility runbook

## Goal
Keep Mission Control usable across Hermes updates. Mission Control never modifies
Hermes core files and ships no patches for it; it depends on a small set of core
interfaces, listed below. An upgrade is safe when those interfaces still answer as
expected.

## Repos and boundaries
- `hermes-agent` (upstream): must stay clean before and after the update.
- `hermes-mission-control` (this repo): the UI, the telemetry sidecar, contracts,
  and smoke scripts.
- Rule: do not couple Mission Control to unstable internal payloads without a
  fallback path.

## Hermes interfaces Mission Control depends on

| Interface | Used for | When it breaks |
|-----------|----------|----------------|
| Dashboard API REST (`hermes dashboard`, default `127.0.0.1:9119`): `/api/status`, `/api/sessions`, `/api/cron/jobs`, `/api/profiles/*`, `/api/tools/toolsets` | Overview, sessions, cron, bot deletion, Tools inventory | Those views fall back to sidecar data or show an error |
| Dashboard API WebSocket `/api/ws` JSON-RPC (`session.*`, `prompt.submit`, `session.events.since`, `profiles.*`, `tools.configure`, `groups.*`) | Chat, Bot Mode, Group Rooms | Chat and Rooms cannot connect |
| Dashboard session token `HERMES_DASHBOARD_SESSION_TOKEN` | REST auth against `:9119` | Dashboard REST calls return `401` |
| `hermes_cli.kanban_db` (imported lazily by `server/kanban_bridge.py`) | Kanban | Kanban endpoints return errors |
| Hermes home layout (`state.db`, `sessions/`, `logs/`, `skills/`, `config.yaml`) | Telemetry views | Individual views degrade |

### Dashboard authentication
The dashboard API only accepts its own session token. `scripts/run-dashboard-api.sh`
exports `HERMES_DASHBOARD_SESSION_TOKEN=$MISSION_CONTROL_TOKEN` (unless it is
already set) before starting `hermes dashboard`, so a single token authenticates
the sidecar, the browser, and the dashboard REST API. If you start `hermes
dashboard` yourself, set `HERMES_DASHBOARD_SESSION_TOKEN` to the same value as
`MISSION_CONTROL_TOKEN`; otherwise REST calls to `:9119` return `401`.

## Pre-upgrade checklist
1. `hermes-agent` working tree is clean: `git status --short`.
2. Mission Control branch is clean or committed: `git status --short`.
3. `pnpm build` succeeds.
4. `bash scripts/smoke-upgrade.sh` passes.

## Update flow
1. Update `hermes-agent` to the target version.
2. Restart the dashboard API, the telemetry sidecar, and the frontend
   (systemd: `systemctl --user restart mission-control.target`; foreground:
   restart `pnpm dev:full` and the dashboard API).
3. Run `bash scripts/smoke-upgrade.sh` again. It checks the dashboard API
   directly (`:9119`) and through the Vite proxy (`:5174`).
4. Open `/agents` and verify the Live toggle, Timeline, and DAG.
5. Open `/sessions`, select a session, and use `Trace` to open
   `/agents?session=<sessionId>`.
6. Open Chat and send a message; open Rooms if you use Group Rooms.

## Expected compatibility behavior
See the [compatibility matrix](../contracts/compatibility-matrix.md).

## Fast failure diagnosis
- Blank trace cards: payload contract mismatch.
- Live mode not updating: SSE unavailable; the UI falls back to polling.
- 401 lock screen in Mission Control: `MISSION_CONTROL_TOKEN` and
  `VITE_MISSION_CONTROL_TOKEN` are missing or different.
- 401 only on dashboard-backed views (sessions, cron, bots):
  `HERMES_DASHBOARD_SESSION_TOKEN` does not match `MISSION_CONTROL_TOKEN`.
- HTTP 500 with an empty body on `/api/*` (not `/api/local/*`): the dashboard API
  on `:9119` is not running, so the Vite proxy has no upstream.

## Rollback levers
1. Keep the new backend and rely on the polling fallback.
2. Use Post mode instead of Live mode on `/agents`.
3. If the backend breaks a contract, pin Hermes to the last known-good commit and
   rerun the smoke script.

## Required artifacts in this repo
- `docs/contracts/mission-control-capabilities-v1.json`
- `docs/contracts/mission-control-trace-v1.json`
- `docs/contracts/compatibility-matrix.md`
- `scripts/smoke-upgrade.sh`
