# HTTP API

Mission Control exposes its own API from the telemetry sidecar
(`server/local_telemetry_server.py`, default `127.0.0.1:8765`). The browser
reaches it through the Vite proxy at `/api/local/*`. Everything else under
`/api/*` (including the `/api/ws` JSON-RPC socket) belongs to the Hermes
dashboard API and is documented by Hermes.

This page lists every sidecar route at the time of writing. The source of truth
is the `do_GET` / `do_POST` / `do_PUT` / `do_PATCH` / `do_DELETE` handlers in
`server/local_telemetry_server.py`.

## Conventions

- **Auth.** Send `Authorization: Bearer <MISSION_CONTROL_TOKEN>` (the sidecar
  also accepts `API_SERVER_KEY` when `MISSION_CONTROL_TOKEN` is unset). Missing
  or wrong tokens return `401` with `{"error": "invalid_api_key"}`. Exceptions
  are marked **open** below.
- **Read-only mode.** With `MISSION_CONTROL_READ_ONLY=1`, every `PUT`, `PATCH`,
  `DELETE` and `POST` (terminal ticket and client diagnostics included) returns
  `403 {"error": "read_only_mode"}`. The terminal WebSocket also refuses to
  spawn a shell for a ticket issued before read-only was enabled (close code
  `4403`); shells already open are not terminated.
- **Errors.** JSON bodies of the form `{"error": "<code>", "detail": "<text>"}`.
  Unknown paths return `404 {"error": "not_found"}`. JSON bodies read through
  the shared helper return `400` for a non-numeric or negative `Content-Length` or a
  non-object JSON body, and `413 {"error": "payload_too_large"}` above 8 MiB
  (provider selection keeps its 32 KiB limit).
- **CORS.** See [telemetry.md](telemetry.md#cors-origin-enforcement).

## Health

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/health` | open | Liveness: `{ok, service, source, push}` |
| GET | `/api/local/health` | open | Same payload, under the proxied prefix |

## System, runtime, and usage

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/local/system` | CPU, memory, disk, thermal (see [telemetry.md](telemetry.md#thermal-telemetry)) |
| GET | `/api/local/status` | Gateway/runtime status and active model |
| GET | `/api/local/model/info` | Model details |
| GET | `/api/local/provider-usage` | Normalized provider usage (see [telemetry.md](telemetry.md#provider-usage-codexbar--nous-portal)) |
| GET | `/api/local/provider-usage/catalog` | Sanitized CodexBar provider catalog plus Mission Control-native providers and `selectionRevision`; `?refresh=1` requests a rate-limited discovery |
| PUT | `/api/local/provider-usage/selection` | Save collectable IDs with `{"selectedProviders":[...],"expectedRevision":"<catalog selectionRevision>"}`; returns canonical IDs and their new `selectionRevision`; `409 selection_conflict` if the revision changed; rejected in read-only mode |
| GET | `/api/local/sessions` | Session list |
| GET | `/api/local/sessions/usage` | Session token/cost usage |
| GET | `/api/local/logs` | Tail of recent Hermes log files (`maxFiles` 1–50, default 10; `maxLines` 1–2000, default 160; out-of-range values are clamped) |
| POST | `/api/local/gateway/restart` | Runs `hermes gateway restart` (requires the `hermes` CLI on `PATH`); answers `202` |

Selection IDs and their opaque 64-character hex revision are read together. The
sidecar compares the expected revision and atomically replaces the selection
under the same lock. Every successful write changes the revision, even if the
IDs return to an earlier value. This fences timed-out PUTs: a delayed old write
cannot overwrite a newer commit. Missing/invalid revisions return `400` without
writing. An uncertain PUT must be reconciled by a versioned catalog GET before
another save. Deploy the matching selection frontend and backend together;
unversioned catalogs remain readable but the new frontend will not issue unsafe
selection writes against them.

## Agents and traces

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/local/mission-control/agents` | Agent snapshot, including the `capabilities` object ([contract](contracts/compatibility-matrix.md)) |
| GET | `/api/local/mission-control/sessions` | Paged session metadata for Agents, Sessions and the Chat picker; see [query parameters](#session-list-queries) |
| GET | `/api/local/mission-control/agents/trace` | Trace for one session ([contract](contracts/mission-control-trace-v1.json)) |
| GET | `/api/local/mission-control/agents/trace/stream` | Same trace as Server-Sent Events (`event: trace`). Bearer header only: `?access_token=` is rejected; the browser reads it with streaming `fetch`, not `EventSource` |

### Session-list queries

`GET /api/local/mission-control/sessions` accepts:

| Parameter | Behavior |
|-----------|----------|
| `limit`, `offset` | Page size (1–500; default 100) and zero-based offset (default 0) |
| `profile` | Exact owning profile, including `default`; omitted means the default store plus discovered local profile stores |
| `session_id` | Optional exact session/reference lookup for preview consumers |
| `query` | Case-insensitive substring search over session ID, title, preview and origin/model metadata, before page slicing |
| `origin`, `model` | Exact source or model filter |
| `status` | `live`, `idle` or `ended`; omit for all statuses |
| `category`, `tab` | Existing session category/view filters (`all`, `live`, `conversation`, `automation`, `system`, where supported) |
| `include_recent_messages` | Defaults to true; false returns metadata without reading recent transcript/plan previews |
| `include_facets` | Defaults to true; includes global metadata collection for facets and counts |

The response contains `items`, `pagination` (`total`, `offset`, `limit`, `hasMore`), `stats` (`totalSessions`, `liveSessions`, `activeAgents`), `facets` and `tabCounts`. Filtering applies before pagination; `pagination.total` is the number of matching rows, not the number loaded in the browser. Within the selected profile scope, `stats` and facets describe the available unfiltered snapshot. The `/api/local/sessions` alias accepts the same list parameters except `session_id`. When the snapshot loader times out (another identical scan still in flight, or the worker pool busy) both routes return `503 {"error": "sessions_unavailable"}` with `Retry-After: 2`.

The Chat picker requests `limit=25&offset=0&status=live&include_recent_messages=false` initially, then forwards the chosen profile, origin, search and page. Runtime-presence metadata is merged with the corresponding stored row so a resumed conversation is not counted as both a historical session and an ephemeral runtime. The merge is read-only and never deduplicates different sessions merely because their titles match. See [Chat picker behavior](chat.md#sessions-picker).

## Configuration, tools, skills, memory

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/local/config` | Hermes `config.yaml` (raw text plus parsed view) |
| PUT | `/api/local/config` | Replace `config.yaml`. The YAML is validated, a `config.yaml.bak.<timestamp>` backup is written, then the file is replaced atomically |
| GET | `/api/local/skills` | Installed skills |
| GET | `/api/local/skills/catalog` | Installable skills catalog |
| GET | `/api/local/skills/files` | File tree of one skill (`skill`) |
| POST | `/api/local/skills/install` | Runs `hermes skills install <identifier> --yes` (`{"identifier"}`) |
| POST | `/api/local/profile/skills/install` | Installs a skill into one profile (`{"profile", "identifier"}`) |
| POST | `/api/local/skills/toggle` | Enables or disables a skill in `config.yaml` (`skills.disabled`) |
| GET | `/api/local/memory/honcho` | Honcho status (see [honcho.md](honcho.md)) |
| POST | `/api/local/memory/honcho/local-identity` | Configures the local Honcho peer (`{"peerName"}`) |

## Cron

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/local/cron/jobs` | List jobs |
| GET | `/api/local/cron/jobs/:id` | Job detail |
| POST | `/api/local/cron/jobs` | Create a job |
| POST | `/api/local/cron/jobs/:id/pause` | Pause (`{"reason"?, "profile"?}`) |
| POST | `/api/local/cron/jobs/:id/resume` | Resume (`{"profile"?}`) |
| POST | `/api/local/cron/jobs/:id/run` | Run now (`{"profile"?}`) |
| PATCH | `/api/local/cron/jobs/:id` | Update a job |
| DELETE | `/api/local/cron/jobs/:id` | Delete a job |

Cron operations go through the Hermes core cron module (`server/cron_bridge.py`).

## Kanban

All routes live under `/api/local/kanban/` and accept `?board=<slug>`. Full
list in [kanban.md](kanban.md#architecture).

## Chat, Bot Mode, and Rooms

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/local/chat/transcript`, `/api/local/chat/canonical` | Stored transcript for a session (`profile`, `session_id`, `session_key`) |
| GET | `/api/local/chat/timestamps`, `/api/local/chat/message-timestamps` | Per-message timestamps for a session |
| GET | `/api/local/chat/sync/stream` | Cross-device chat sync, Server-Sent Events (`client_id`, `session_id`, `since`). Bearer header only: `?access_token=` is rejected; the browser reads it with streaming `fetch`, not `EventSource` |
| GET | `/api/local/chat/sync/stats` | Sync relay counters |
| POST | `/api/local/chat/sync/publish` | Publish a sync envelope to other devices |
| GET, POST | `/api/local/chat/presence` | Runtime presence per session |
| POST | `/api/local/chat/title` | Store an MC-side session title |
| GET | `/api/local/chat/handoffs`, `/api/local/chat/handoffs/all` | Bot handoffs (by `session_id`, or all) |
| POST | `/api/local/chat/handoffs` | Record a handoff |
| POST | `/api/local/chat/handoffs/claim` | Claim a pending handoff |
| GET, POST | `/api/local/chat/last` | Cross-device last-chat pointer (POST is compare-and-swap, `409` on conflict) |
| GET, POST | `/api/local/room/last` | Cross-device last-room pointer (same CAS rules) |
| GET, POST, DELETE | `/api/local/room/vault` | Optional room → vault routing map (see [rooms.md](rooms.md#room-creation-and-the-vault-destination)) |
| GET | `/api/local/room/tools` | Read-only tool/reasoning traces for a room (`room_id`, `max_age`) |

Details: [chat.md](chat.md), [bot-mode.md](bot-mode.md), [rooms.md](rooms.md).

## Whiteboard (tldraw Agent Mode)

| Method | Path | Purpose |
|--------|------|---------|
| GET, POST | `/api/local/chat/whiteboard` | Session-bound whiteboard state and agent command queue |
| GET, POST | `/api/local/chat/canvas/:addon` | Canvas add-on protocol dispatch (registered with `register_canvas_handler`) |

See [tldraw-feature-matrix.md](tldraw-feature-matrix.md).

## Invite-only rooms

All suffixes below are under `/api/local/room-invites`. The route dispatcher is mounted by the telemetry sidecar, but the feature is disabled by default: it requires valid owner-private `MISSION_CONTROL_ROOM_INVITES_CONFIG`; pairing routes additionally require a valid `pairing` block, and joining routes need a valid joining store. Missing or invalid configuration is intentionally reported as 404. These tables describe the candidate code, not production availability: process-lifecycle wiring for the control listener, two-installation end-to-end acceptance, and deployment are still open.

All routes require the normal MC bearer token and exact configured Origin policy whenever an `Origin` header is supplied. Responses are `no-store`; request/response bodies containing invitation or pairing material are not logged. See the [MVP implementation plan](plans/invite-rooms-mc-mvp.md) and the [P3b pairing design](plans/invite-rooms-p3b-pairing.md) for safety boundaries and remaining gates.

### Host lobby and activation

| Method | Suffix | Purpose |
|--------|--------|---------|
| GET | `/paired-peers` | List redacted paired peers for roster selection |
| POST | `/lobbies` | Create a fixed-roster lobby |
| GET | `/lobbies/{lobby_id}` | Read the redacted lobby projection |
| POST | `/lobbies/{lobby_id}/invites` | Issue an invitation; this explicit response is the only host-lobby operation that returns the invite code |
| POST | `/lobbies/{lobby_id}/invites/{invite_id}/revoke` | Revoke an unused invitation |
| POST | `/lobbies/{lobby_id}/invites/{invite_id}/decision` | Host's explicit approval or rejection, bound to the displayed peer/policy binding |
| GET | `/lobbies/{lobby_id}/activation` | Read activation readiness and per-peer registration projection |
| POST | `/lobbies/{lobby_id}/activate` | Owner-explicit activation; ambiguous or incomplete native effects are not reported as success |

### Pairing

| Method | Suffix | Purpose |
|--------|--------|---------|
| GET | `/pairing` | Read redacted pairing-journal rows |
| POST | `/pairing/offers` | Create a host offer code |
| POST | `/pairing/offers/{pairing_id}/accept` | Accept an offer and return the next public code/SAS step |
| POST | `/pairing/offers/{pairing_id}/confirm` | Confirm the host-side SAS read over the independent channel |
| POST | `/pairing/offers/{pairing_id}/cancel` | Cancel an undecided host offer |
| POST | `/pairing/imports` | Import a joining-side offer and return the next public code |
| POST | `/pairing/imports/{pairing_id}/reveal` | Reveal the joining-side SAS step |
| POST | `/pairing/imports/{pairing_id}/confirm` | Confirm the joining-side SAS read over the independent channel |
| GET | `/peers` | List redacted paired peers |
| DELETE | `/peers/{peer_id}` | Unpair and advance the durable pairing generation |

Pairing codes are public transcript material, not credentials; owners must compare the SAS out of band. Never automate confirmation or treat a copied code alone as trust.

### Joining-owner imports

| Method | Suffix | Purpose |
|--------|--------|---------|
| POST | `/imports/preview` | Parse an invitation locally (`code`, optional `host_id`); no navigation or URL fetch |
| GET | `/imports/{import_id}` | Read the redacted joining state |
| POST | `/imports/{import_id}/metadata` | Refresh sanitized invitation metadata over the pinned host channel |
| POST | `/imports/{import_id}/redeem` | Explicitly redeem the imported invitation |
| POST | `/imports/{import_id}/scope` | Confirm the exact room/member scope shown to the joining owner |
| POST | `/imports/{import_id}/consent` | Record the joining owner's explicit consent (`approve` boolean) |
| POST | `/imports/{import_id}/resolve` | Owner-directed resolution of an ambiguous redemption |

### Guest-room approvals

The guest routes below use the same base path, bearer authentication, Origin policy, and feature gate. Guest approvals remain a separate operation from pairing, invite issuance, and joining-owner consent.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/guest-rooms` | List locally consented joined rooms |
| GET | `/guest-rooms/{room_id}/log?since_seq=0&limit=200` | Read the pinned host transcript verbatim; unchanged pagination and event identities |
| GET | `/guest-rooms/{room_id}/my-bot` | Snapshot only the confirmed local bot's approvals |
| POST | `/guest-rooms/{room_id}/approvals/{request_id}` | Body **only** `{"choice":"approve"}` or `{"choice":"reject"}` |
| POST | `/guest-rooms/{room_id}/leave` | Empty object; revoke the local grant and persist terminal leave (unchanged) |

Approval response shape:

```json
{
  "status": "online",
  "approvals": [{
    "request_id": "approval-<opaque-handle>",
    "title": "native redacted command",
    "detail": "native redacted description",
    "command": "native redacted command",
    "description": "native redacted description",
    "choices": ["once", "deny"],
    "allow_permanent": false
  }],
  "permissions": []
}
```

`request_id` is an opaque MC reference, **not** a native prompt/session ID. It
maps internally to the exact confirmed scope, native endpoint/credential,
`session_id`, `prompt_id`, and `execution_generation`. Handles expire after
five minutes, on process restart, or bounded-cache eviction (2048 handles).
Do not parse, synthesize, persist, or recycle them for a different prompt.
The existing frontend `request_id`/`title`/`detail` contract can carry this
without an API/schema change. A valid decision returns `200 {}` only after an
exact native `resolved` or `already_resolved` result; the latter means the
selected request settled concurrently, not that another request was approved.

Discovery uses the fixed owner-configured local native socket/token, with the
**confirmed binding's target profile**, not the launch profile or an HTTP
parameter. The adapter requires `gateway.ready` to advertise canonical session
authority, then requests `session.list {include_hidden:true,limit:200}` and
filters exact `title == "Group: " + confirmed_room_id` and `source == "bot_room"`.
It never uses the canonical title resolver, computes a room hash, trusts a
receipt's `roomlink_*`, or accepts session/generation/profile/URL input.
There is no persistent REST fallback in this implementation: a cold/missing
live row is temporarily unavailable, and a full 200-row list is inconclusive
even if it contains one apparent match. Multiple matches fail closed.

Each snapshot/decision opens one authenticated connection and mounts with
`session.resume`. A decision rediscovers and snapshots again, matches the
**exact** saved tuple, and sends canonical `approval.respond` on that same
subscribed socket (`approve -> once`, `reject -> deny`). `profile` is the
canonical routing envelope stripped by the authority before method validation.
Every network await is fenced by fresh confirmed consent/material/leave and
peer-binding checks, including immediately before the response write. The existing
target material validator rechecks grant claim scope/expiry and pinned catalog/policy
digests; the stored consent receipt must exactly match the confirmed scope/purpose.
Native stale-generation checks remain authoritative for races after that write.
Snapshot polling is used; there is no event replay or cursor continuity claim.
Cleanup detaches without `end_reason`, then closes only its socket; no turn is
submitted, interrupted, stopped or steered. No generic RPC route is exposed.
The existing bounded groups adapter and leave/transcript contracts are unchanged.

| HTTP | `code` | Meaning |
|------|--------|---------|
| 401 | Normal MC auth error | Caller unauthenticated |
| 403 | `forbidden` | Unconfirmed/left/changed consent or material, peer mismatch/unpair, profile/auth refusal |
| 403 | `forbidden_origin` | Origin not explicitly allowed |
| 404 | Normal unknown-route response | Feature off/misconfigured or unsupported guest control route |
| 409 | `request_expired` | Unknown/expired handle, changed scope/endpoint/credential, vanished exact request or stale generation; resnapshot |
| 501 | `not_supported` | No canonical adapter/authority or incompatible native session protocol; never legacy fallback on auth failure |
| 503 | `session_unavailable` | No exact live room session yet; retry snapshot later |
| 503 | `discovery_inconclusive` | Duplicate matches or saturated live listing; no ID guessing |
| 503 | `native_unavailable` | Missing config/token, offline/closed socket, bounded timeout, malformed/ambiguous native reply |

**Security restriction:** this is an authenticated-local-operator feature. It
assumes the trusted core maintains a unique, unaltered canonical room namespace
within the confirmed target profile. Public session projections do **not**
include the private `state_meta` room identity; title-based discovery is **not a
cryptographic grant-to-session binding** and does not independently attest the
hidden home/member tuple. Local confirmed material/consent and peer state are
re-read; there is no native grant-status introspection here. An operator who
revokes directly outside this local state must also revoke local consent/material
(or leave/unpair) to disable MC responses to an already-running waiter. Deployments
requiring cryptographic scoped lookup or an independent native-grant revocation
fence must keep this feature unavailable until a scoped core contract exists.
Do not use title uniqueness to claim resistance to a malicious local operator
or tampered native database. Ambiguity is always denied.

**Frontend follow-up (not implemented here):** render temporary discovery/offline
503 separately from genuine protocol 501, and handle 409 by dropping the selected
handle and fetching a fresh snapshot. Do not silently retarget or auto-retry a
decision on the next request. Polling can reuse the existing API fields. No UI
files are changed in this backend packet.

## Browser terminal

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/local/terminal/ticket` | Exchanges the bearer token for a single-use ticket valid for 30 seconds |

The terminal itself is a WebSocket served by `server/terminal_server.py` on
`MISSION_CONTROL_TERMINAL_HOST:MISSION_CONTROL_TERMINAL_PORT` (default
`127.0.0.1:8766`) and proxied by Vite at `/api/terminal?ticket=<ticket>`. A
valid ticket opens an interactive shell (`zsh` or `bash`) as the user running
the sidecar. Invalid tickets are closed with code `4401`.

## Web Push (optional)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/local/push/vapid-public-key` | Public VAPID key for `pushManager.subscribe()` |
| GET, POST, DELETE | `/api/local/push/subscriptions` | List, add, remove push subscriptions |
| POST | `/api/local/push/send` | Send a push notification to all subscriptions |

See [telemetry.md](telemetry.md#web-push-optional).

## Diagnostics and plugins

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/api/local/client-diagnostics` | bearer header **or** `_accessToken` in the JSON body | Appends browser reload breadcrumbs to `<hermes home>/logs/mission-control-client.log` (the token is never written) |
| GET | `/api/local/plugins` | bearer | Loaded plugin manifests |
| GET, POST | `/api/local/<plugin path>` | bearer | Any route declared by an installed plugin (see [plugins.md](plugins.md)) |
