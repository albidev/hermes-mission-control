# Hermes Mission Control

Operational dashboard for Hermes with a responsive shell, route-based navigation, and live runtime telemetry.

## What this is
A standalone frontend app that gives you one place to operate Hermes:
- gateway and backend health
- active model, fallback model, and agent status
- sessions, knowledge, tools, skills, config, logs routes
- cron/job visibility and quick actions
- shared knowledge surfaced from Obsidian

## Current UI architecture
- Left side navigation rail with collapsible desktop mode and off-canvas mobile drawer
- Workspace header with route context + sync metadata
- Overview route as fast operations snapshot
- Dedicated routes for high-density workflows (`/sessions`, `/knowledge`, `/tools`, `/skills`, `/config`, `/logs`)

## Local development

```bash
cd apps/mission-control
npm install
npm run dev
```

The Vite dev server runs on port `5174` (`--strictPort`) and is reachable on LAN.

## API contract
The app reads from Mission Control endpoints exposed by Hermes API server:

```text
GET /api/mission-control
GET /api/mission-control/system
```

Additional route-specific data comes from dedicated Mission Control endpoints.

## Environment variables

```bash
VITE_HERMES_API_BASE_URL=http://localhost:8642/api
```

If unset, the app uses `/api` through the Vite proxy.

## Auth
If Hermes API server has `API_SERVER_KEY`, Mission Control prompts for a bearer token before unlocking the dashboard.

## Deep links
You can open selected entities with query params:

```text
/?session=<session-id>
/?cron=<job-id>
/?alert=<alert-id>
```

## Notes from 2026-03-30
- Knowledge page: removed highlights section from desktop detail and mobile modal.
- Overview mobile polish: tightened spacing and fixed wrapping for metadata rows and footer controls.
- Top status strip (`AgentStatusBar`) hardened for small screens:
  - model label truncates correctly instead of ugly multiline breaks
  - fallback label hidden on mobile to reduce crowding
  - status/agent count remain readable without clipping
- Workspace header mobile metadata now stacks vertically for readability.
- Agents route now includes full execution trace with both required views:
  - Timeline (default, mandatory): turn boundaries, thoughts, tool starts/completions, skill usage, assistant responses
  - DAG: node/edge representation of execution chain for quick dependency reading
- New backend endpoints:
  - `GET /api/mission-control/agents/trace` (`session_id`, `limit`)
  - `GET /api/mission-control/agents/trace/stream` (SSE live stream)
- Live trace now prefers SSE transport with polling fallback.

## Verification

```bash
npm run build
```

Build must pass after UI/layout changes.