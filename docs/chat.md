# Chat

Mission Control includes an **expanded Chat** that talks directly to the Hermes gateway over a WebSocket — not to a separate backend. It streams reasoning, tools, and the final response live, keeps a per-session presence pill in sync, and links the active conversation to a tldraw whiteboard (see [tldraw Agent Mode](tldraw-feature-matrix.md)).

## Architecture

```
React Chat UI (ChatDrawer / chat-messages)
        │
        ├─ chat-gateway.ts      → WebSocket RPC + event handling, the main driver
        ├─ chat-protocol.ts     → frame parsing, event → transcript apply, RPC helpers
        ├─ chat-transport.ts    → WS URL, auth ticket minting, reconnection
        ├─ chat-presence.ts     → presence pill (localStorage + BroadcastChannel)
        ├─ chat-persistence.ts  → transcript save/restore (localStorage + server)
        ├─ chat-interactions.ts → interaction request titles
        ├─ chat-commands.ts     → slash command dispatch
        ├─ todo-plan.ts         → derives and normalizes the live plan snapshot
        └─ Mission Control sessions API → exposes the sanitized plan in preview payloads
        │
        ▼
GET /api/ws  (Hermes gateway)
```

The chat connects to the gateway's WebSocket endpoint, not to the telemetry sidecar. All RPCs and streaming events flow over that single socket.

## Modules

| Module | Path | Role |
|--------|------|------|
| Gateway driver | `src/lib/chat-gateway.ts` | Owns the connection lifecycle, applies events, sends RPCs |
| Protocol | `src/lib/chat-protocol.ts` | Parses frames, maps events to transcript updates, RPC helpers |
| Transport | `src/lib/chat-transport.ts` | WS URL, auth (`ws-ticket` / loopback token), reconnect w/ backoff |
| Presence | `src/lib/chat-presence.ts` | `ChatPresencePhase` (`idle`/`running`/`completed`/`waiting`/`unread`), pill sync |
| Persistence | `src/lib/chat-persistence.ts` | `localStorage` transcript save/restore + server `last_chat` sync |
| Interactions | `src/lib/chat-interactions.ts` | Interaction-request titles |
| Commands | `src/lib/chat-commands.ts` | Slash-command output |
| Rendering | `src/components/chat-messages.tsx` | Message bubbles, reasoning bubble, attachments |
| Live plan | `src/lib/todo-plan.ts` + `src/components/chat/ChatTodoPlan.tsx` + session payload | Derives and displays the current `todo` tool snapshot in live chat and preview mode |

## Connection & auth

The chat reaches the gateway at `/api/ws`. It authenticates one of two ways (`chat-transport.ts`):

1. **Loopback session token** — fetched from `/api/gateway-root`, used when running locally.
2. **WS ticket** — POST `/api/auth/ws-ticket` with the access token, returns a one-time `ticket` used as a query param.

Reconnection uses exponential backoff (`MAX_RECONNECTS = 6`, `RPC_TIMEOUT_MS = 120000`).

## Presence pill

`chat-presence.ts` models the agent state as `ChatPresencePhase`: `idle` → `running` → `completed`, plus `waiting` (needs the user) and `unread`. The pill is persisted to `localStorage` and broadcast across tabs via `BroadcastChannel` (`mission-control:chat-presence`), so opening Mission Control in another tab keeps the same presence.

Key behaviours (all validated end-to-end):
- `completed` (the **Done** state) stays visible until the user opens the chat and scrolls to the bottom; reaching the bottom publishes an `idle` event that the gateway also clears.
- On reload, the presence is restored from `localStorage` and reconciles against the transcript: a persisted `running` with a complete assistant response resolves to `completed` → **Done**, never stuck in `Working`.
- Any activity event with `state: running` reactivates the `Working` state; only a completion event closes the turn.

## Persistence

`chat-persistence.ts` stores the transcript under `mission-control-chat-drawer-v1` in `localStorage` (session id/key, model identity, messages, `updatedAt`) and syncs the last chat to the server (`/api/local/chat/last`) so it can be restored across devices/browser restarts.

## Streaming & reasoning

Events streamed over the socket update the transcript live (`chat-protocol.ts`). Reasoning and response are separate streams — if the gateway delivers the reasoning **after** the completed response, the reasoning bubble is inserted *before* the last complete assistant reply rather than appended at the end, so it never appears as an afterthought.

## Live TODO mission capsule

When the gateway transcript contains a `todo` tool result, the ChatDrawer renders a compact mission capsule immediately above the statusline. It shows the completed/total count and current task; tapping it expands the full plan with the current and next task, progress bar, nested subtasks, and semantic item states.

`src/lib/todo-plan.ts` normalizes the core's `{ revision, todos }` snapshot. The ChatDrawer consumes `todo.updated` during live streaming and `todo_state` from `session.resume`; the transcript parser remains a compatibility fallback for older gateways. Completed tool results are authoritative; partial `merge:true` arguments without task descriptions are ignored so they cannot erase a known plan. For post-session preview, the sessions API derives the same sanitized snapshot from the full JSONL (falling back to SessionDB/request dumps) and sends only `todoPlan`, never the request dump itself. An empty TODO list hides the capsule. This keeps live chat, resume, and preview mode on one data contract without modifying Hermes Core.

The capsule is responsive: it remains inline in the desktop drawer and keeps a 44px-friendly touch target on narrow screens. All labels and accessibility copy are localized through the EN/IT catalogs.

## Whiteboard link

The active chat session's `sessionKey` binds the conversation to a tldraw whiteboard. The agent can read structured board context, receive a PNG screenshot in Chat, and apply validated actions back to the canvas through the authenticated local telemetry bridge. See [tldraw Agent Mode](tldraw-feature-matrix.md).
