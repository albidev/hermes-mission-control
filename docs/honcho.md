# Honcho memory in Mission Control

Mission Control does not connect to Honcho directly. Honcho remains a Hermes memory provider; Mission Control only helps the operator inspect its readiness and configure the identity boundary used by local installations.

## Availability gate

The panel is rendered only when the Honcho memory provider is actually installed
on the host. `GET /api/local/memory/honcho` reports `providerInstalled`, resolved
by Hermes's own memory-plugin discovery (`plugins.memory.find_provider_dir`),
falling back to the on-disk layout (`<hermes-agent>/plugins/memory/honcho/__init__.py`)
when no checkout is importable. `MISSION_CONTROL_MEMORY_PROVIDERS_DIR` overrides
discovery for tests and unusual deployments.

When the provider is absent the panel renders nothing — no setup form, no empty
profile list — and no endpoint is added to Hermes core. Do not replace this with
a heuristic (config presence, env var, or a hardcoded path): the point is that a
host without the provider must not be offered its configuration UI.

## Identity modes

### Local single-user

Use this mode when one person owns the Hermes installation and Mission Control is protected by its installation bearer token.

1. Configure Honcho in Hermes:

   ```bash
   hermes memory setup honcho
   ```

2. Open **Config → Honcho memory** in Mission Control.
3. Enter one stable, non-secret peer name and choose **Configure identity**.
4. Start a new chat/session.

Mission Control updates `honcho.json` atomically and creates a timestamped owner-only backup first. It applies the same human peer to the default and discovered named profiles, while preserving or assigning a distinct AI peer per profile and enabling `sessionAiPeerPrefix` to prevent profile session-name collisions.

A shared bearer token proves access to the installation; it does **not** identify an individual person. Do not use local single-user mode on a host shared by unrelated users.

## Authenticated dashboard users

For shared access by known users, enable Hermes dashboard authentication (password, OIDC, or another registered dashboard auth provider).

The identity path is server-authoritative:

```text
Dashboard login
    → POST /api/auth/ws-ticket
    → single-use WebSocket ticket carrying {provider, user_id}
    → Hermes session / AIAgent runtime identity
    → Honcho runtime peer resolution
```

Mission Control proxies `/login`, `/auth/*`, `/api/auth/*`, and `/api/ws` to the configured Hermes dashboard backend in development, keeping cookies and callbacks same-origin. Mission Control never sends a browser-selected `user_id` in `session.create`, `session.resume`, or any other RPC.

The stable identity must come from the authentication provider's immutable subject/account ID—not an email address, display name, IP address, session ID, or browser storage.

## Profile behavior

For one user with several Hermes profiles:

- the human peer remains the same;
- each profile has its own deterministic AI peer;
- `sessionAiPeerPrefix` keeps identical project/chat names separate across profiles;
- profile routing still selects the owning Hermes home and `state.db`;
- a new chat returns to the default profile unless explicitly targeted;
- resuming a chat preserves its owning profile.

The Config panel lists every discovered profile and reports one of:

- `ready` — connection and identity are configured;
- `credentials required` — common for a named profile using Honcho Cloud without profile-scoped credentials;
- `identity required` — connection exists but no local peer/runtime identity is available;
- `disabled` — Honcho is disabled for that effective profile.

Hermes profiles intentionally isolate credentials. Mission Control does not copy a default profile's cloud API key into named profiles. Configure those explicitly:

```bash
hermes -p <profile> memory setup honcho
```

Self-hosted local Honcho deployments usually share the root `baseUrl` and need no copied cloud credential.

## API

All endpoints require the normal Mission Control bearer token.

### Read status

```http
GET /api/local/memory/honcho
```

The response includes readiness, identity mode, workspace, default AI peer, session-prefix state, and per-profile status. API keys are never returned.

### Configure local identity

```http
POST /api/local/memory/honcho/local-identity
Content-Type: application/json

{"peerName":"operator-name"}
```

Allowed characters: letters, numbers, `.`, `_`, and `-`; maximum 64 characters. The endpoint is disabled by `MISSION_CONTROL_READ_ONLY=1` like every other Mission Control mutation.

## Security boundary and deferred multi-tenancy

The current supported shared-access model is authenticated users on a single-owner/operator installation. True tenant-isolated hosting requires tenant-scoped Honcho workspaces plus authorization for telemetry, sessions, profiles, logs, pointers, rooms, whiteboards, and caches.

That work is deliberately tracked separately and must not be approximated with one shared token or a client-side tenant selector:

- [Backlog: tenant-isolated Mission Control + Honcho multi-tenancy](https://github.com/albidev/hermes-mission-control/issues/78)

## Troubleshooting

### Provider is active but the session has no Honcho memory

Check the Config panel. If identity is unresolved in local mode, configure the stable local peer and start a **new session**. Hermes does not retry an unresolved-peer initialization inside the already-running session.

### Profile says credentials required

Run profile-scoped setup. Do not copy credentials by hand between profile directories:

```bash
hermes -p <profile> memory setup honcho
```

### Configured but not active

Set the Hermes memory provider in `config.yaml` and start a new session:

```yaml
memory:
  provider: honcho
```

### Self-hosted backend unavailable

Verify the Honcho API and deriver independently:

```bash
curl http://127.0.0.1:8000/health
docker compose ps
docker compose logs deriver --tail 20
```
