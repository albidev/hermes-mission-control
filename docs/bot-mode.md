# Bot Mode

Mission Control include **Bot Mode**: un roster gestito di profili Hermes trattati come bot, ciascuno con una chat canonica (`Bot Chat`), handoff attribuiti nel flusso di chat, autocomplete `@mention`, group rooms e gestione CRUD completa dei profili. Tutto parla con il gateway Hermes via WebSocket JSON-RPC (`/api/ws`) — nessuna modifica al core Hermes.

## Architettura

```
React (ChatDrawer / BotsRoute / GroupRoomView)
        │
        ├─ bot-gateway.ts           → RPC profile-scoped (session.list/create/title, profiles.*, tools.configure)
        ├─ bot-chat-routing.ts      → canonical Bot Chat resolver (single-flight, adopt-before-mint)
        ├─ bot-chat-policy.ts       → modalità canonical vs task: /new → /compact sulla canonical
        ├─ bot-handoff*.ts          → handoff attribuiti: observer, persistence, recovery, reasons
        ├─ bot-lineage.ts           → lineage trace per sessione profile-scoped
        ├─ bot-mentions.ts          → parsing/autocomplete @mention dal roster live
        ├─ bot-create.ts / bot-delete.ts → CRUD profili con auth
        ├─ group-gateway.ts + use-group-room.ts → group rooms via RPC groups.*
        └─ chat-gateway.ts          → multiplexing eventi per sessione/bot
        │
        ▼
GET /api/ws  (Hermes gateway)
```

## Feature

### Bot roster (BotsRoute)

- Elenco profili da `profiles.list` (via `loadBotProfiles`), con `is_bot` derivato da `ui_meta.mission_control.bot === true`.
- CRUD profili: creazione (`bot-create.ts`, opzioni clone, skills, auth sharing, bot roster flag), configurazione (toolsets, MCP servers, skills), **delete con auth** (`bot-delete.ts` — guardia su `default`, verifica `payload.ok === true` dal gateway).
- Modello/provider configurabili per bot (`loadBotModelOptions` → `profiles.configure` / `tools.configure`).
- Pagina responsive mobile.

### Canonical Bot Chat

Ogni bot ha **una** chat duratura identificata da `(profile, title = "Bot Chat")`, non da un puntatore salvato:

- `createBotChatResolver` (in `bot-chat-routing.ts`): lookup per nome con `include_hidden: true`, poi create solo se assente (**adopt-before-mint**).
- Single-flight per profile: un doppio click/submit non crea due sessioni (`inFlight` map).
- Fail-closed: id registry mancante → errore; `knownCanonicalId` ma lista vuota → errore; race `already-in-use` → re-list + adozione del vincitore.
- La sessione nasce `hidden: true`; su canonical, `/new` e `/reset` vengono rimappati a `/compact` (`canonicalChatCommand`), così la relazione resta duratura.

### Handoff attribuiti

- Handoff `@bot` dalla chat origine alla sessione target, con **card di attribuzione** nel flusso (BotHandoffMessage): chi ha risposto, a quale richiesta, con `handoffId` diagnostico.
- Persistenza e recovery: `bot-handoff-persistence.ts`, `bot-handoff-recovery.ts`, `bot-handoff-observer.ts` (polling di rete con reasons), `bot-handoff-reasons.ts`.
- Timeline ordina i messaggi attribuiti (chat-timeline) e i reply persi vengono riallineati al resume.
- Lineage per sessione profile-scoped (bot-lineage) per trace e risoluzione.

### Mention autocomplete

- `@Bot` completato contro il **roster live** (`profiles.list`), non contro testo libero: mention non risolta = testo invariato.
- Popover condiviso (`ChatMentionPopover` / `ChatCompletionPopover`) nel composer.

### Group rooms

- Chat di gruppo integrate nel drawer con **vista final-only** (`GroupRoomView` + `group-room-view-model`).
- Client RPC autenticato (`group-gateway.ts`): costruito con `requestBotRpc(method, params, storedToken)` — il token MC è necessario altrimenti `groups.*` risponde 401.
- Stato vuoto onesto quando `groups.list` torna zero rooms.

## Invarianti

1. Un bot è un profilo Hermes.
2. L'identità del Bot Chat è `(profile, title = "Bot Chat")`; mai puntatori in `ui_meta`/localStorage.
3. Creazione con **adopt-before-mint**; race risolta con ri-risoluzione e adozione del vincitore.
4. Nessun endpoint bot su `local_telemetry_server.py` se il dato è già esposto dal gateway JSON-RPC.
5. Nessuna modifica al core Hermes (zero-core rule).

## Failure handling

- roster non disponibile → mention non risolta, testo invariato;
- canonical lookup fallito → **non creare** nuova sessione;
- `session.list`/`create`/`title` falliti → errori tipizzati con contesto (`BotRpcError` con `code`/`data`);
- reply persa durante reconnect → outbox/correlation, mai doppio submit cieco;
- `requestBotRpc` ha timeout (`RPC_TIMEOUT_MS`) e cleanup socket su ogni esito.

## Test

Suite dedicata in `tests/` : `bot-chat-routing.test.ts`, `bot-chat-policy.test.ts`, `bot-handoff*.test.ts`, `bot-lineage.test.ts`, `bot-mentions.test.ts`, `bot-create.test.ts`, `bot-gateway.test.ts`, `group-gateway.test.ts`, `group-room*.test.ts`, `chat-ui-contract.test.ts`, più store server-side (`server/tests/test_chat_handoff_store.py`, `test_chat_title_store.py`, `test_last_chat_store.py`).

> Implementato nella PR #51 (merged 2026-09-12, merge commit `bac8ad7`). Storico della feature (proposta originale di bot handoff) nel vault: `projects/hermes-mission-control/sections/bot-crossconnection.md` — la feature viva è ora questa doc.
