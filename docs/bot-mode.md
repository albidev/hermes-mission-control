# Bot Mode

Mission Control includes **Bot Mode**: a managed roster of Hermes profiles treated as bots, each with a canonical chat (`Bot Chat`), attributed handoffs in the chat stream, `@mention` autocomplete, group rooms, and full profile CRUD. Everything talks to the Hermes gateway over the WebSocket JSON-RPC surface (`/api/ws`) — zero Hermes core modifications.

## Architecture

```
React (ChatDrawer / BotsRoute / GroupRoomView)
        │
        ├─ bot-gateway.ts           → profile-scoped RPC (session.list/create/title, profiles.*, tools.configure)
        ├─ bot-chat-routing.ts      → canonical Bot Chat resolver (single-flight, adopt-before-mint)
        ├─ bot-chat-policy.ts       → canonical vs task mode: /new → /compact on the canonical chat
        ├─ bot-handoff*.ts          → attributed handoffs: observer, persistence, recovery, reasons
        ├─ bot-lineage.ts           → lineage trace for profile-scoped sessions
        ├─ bot-mentions.ts          → @mention parsing/autocomplete from the live roster
        ├─ bot-create.ts / bot-delete.ts → profile CRUD with auth
        ├─ group-gateway.ts + use-group-room.ts → group rooms via authenticated groups.* RPC
        └─ chat-gateway.ts          → event multiplexing per session/bot
        │
        ▼
GET /api/ws  (Hermes gateway)
```

## Features

### Bot roster (BotsRoute)

- Profile list from `profiles.list` (via `loadBotProfiles`), with `is_bot` derived from `ui_meta.mission_control.bot === true`.
- Profile CRUD: creation (`bot-create.ts`, clone options, skills, auth sharing, bot roster flag), configuration (toolsets, MCP servers, skills), **delete with auth** (`bot-delete.ts` — guard on `default`, verifies `payload.ok === true` from the gateway).
- Per-bot model/provider configuration (`loadBotModelOptions` → `profiles.configure` / `tools.configure`).
- Mobile-responsive page.

### Canonical Bot Chat

Each bot has **one** durable chat identified by `(profile, title = "Bot Chat")`, not by a stored pointer:

- `createBotChatResolver` (in `bot-chat-routing.ts`): lookup by name with `include_hidden: true`, then create only if absent (**adopt-before-mint**).
- Single-flight per profile: a double click/submit never creates two sessions (`inFlight` map).
- Fail-closed: missing registry id → error; `knownCanonicalId` but empty list → error; `already-in-use` race → re-list + adopt the winner.
- The session is created `hidden: true`; on the canonical chat, `/new` and `/reset` are remapped to `/compact` (`canonicalChatCommand`), keeping the relationship durable.

### Attributed handoffs

- `@bot` handoff from the origin chat to the target session, with **attribution cards** in the stream (BotHandoffMessage): who answered, which request, with a diagnostic `handoffId`.
- Persistence and recovery: `bot-handoff-persistence.ts`, `bot-handoff-recovery.ts`, `bot-handoff-observer.ts` (network polling with reasons), `bot-handoff-reasons.ts`.
- The timeline orders attributed messages (chat-timeline), and lost replies are re-aligned on resume.
- Profile-scoped session lineage (bot-lineage) for traces and resolution.

### Mention autocomplete

- `@Bot` completed against the **live roster** (`profiles.list`), never free text: unresolved mention = unchanged text.
- Shared popover (`ChatMentionPopover` / `ChatCompletionPopover`) in the composer.

### Group rooms

- Group chats integrated in the drawer with a **final-only view** (`GroupRoomView` + `group-room-view-model`).
- Authenticated RPC client (`group-gateway.ts`): built with `requestBotRpc(method, params, storedToken)` — the MC token is required otherwise `groups.*` answers 401.
- Honest empty state when `groups.list` returns zero rooms.

## Invariants

1. A bot is a Hermes profile.
2. The Bot Chat identity is `(profile, title = "Bot Chat")`; never pointers in `ui_meta`/localStorage.
3. Creation uses **adopt-before-mint**; races are resolved by re-resolution and winner adoption.
4. No bot endpoint on `local_telemetry_server.py` if the data is already exposed by the gateway JSON-RPC.
5. No Hermes core modification (zero-core rule).

## Failure handling

- roster unavailable → mention unresolved, text unchanged;
- canonical lookup failed → **do not create** a new session;
- `session.list`/`create`/`title` failures → typed errors with context (`BotRpcError` with `code`/`data`);
- reply lost during reconnect → outbox/correlation, never blind double submit;
- `requestBotRpc` has a timeout (`RPC_TIMEOUT_MS`) and socket cleanup on every outcome.

## Tests

Dedicated suite in `tests/`: `bot-chat-routing.test.ts`, `bot-chat-policy.test.ts`, `bot-handoff*.test.ts`, `bot-lineage.test.ts`, `bot-mentions.test.ts`, `bot-create.test.ts`, `bot-gateway.test.ts`, `group-gateway.test.ts`, `group-room*.test.ts`, `chat-ui-contract.test.ts`, plus server-side stores (`server/tests/test_chat_handoff_store.py`, `test_chat_title_store.py`, `test_last_chat_store.py`).

> Implemented in PR #51 (merged 2026-09-12, merge commit `bac8ad7`). Feature history (original bot handoff proposal) lives in the vault: `projects/hermes-mission-control/sections/bot-crossconnection.md` — this doc is the living reference.
