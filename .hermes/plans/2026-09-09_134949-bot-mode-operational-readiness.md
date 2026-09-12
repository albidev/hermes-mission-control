# Bot Mode — Operational Readiness Plan

> **For Hermes:** implement in focused tasks; preserve the Mission Control / Hermes-core boundary.

**Goal:** trasformare il roster Bot già presente in Mission Control in un routing layer operativo e verificabile: un utente può indirizzare una richiesta a un Bot, il Bot lavora nella propria canonical chat e MC restituisce una risposta attribuita senza corrompere il transcript d’origine.

**Architecture:** il roster resta un adapter sottile sul JSON-RPC gateway. Mission Control possiede il routing **utente → Bot** e la rappresentazione UI dell’handoff; Hermes possiede la coordinazione **Bot → Bot** tramite `message_agent` dal canonical `Bot Chat`. Nessun endpoint Bot va aggiunto al core o al telemetry sidecar quando il gateway espone già il dato.

**Tech stack:** React/TypeScript, Vite, MC `/api/ws` JSON-RPC, Hermes profile/session RPC.

---

## Stato effettivo — 2026-09-09

Il branch `feat/bot-mode` contiene il foundation layer:

- route `/bots`, nav e localizzazione;
- roster live da `profiles.list` con marker esplicito `ui_meta.mission_control.bot`;
- create/edit Bot (= profilo Hermes), `SOUL.md`, provider/model, toolset e skill installabili scoped al profilo;
- modal UX, provider/model picker e toolset catalog per il create flow;
- profilo `crossnection` già creato e configurato separatamente;
- bridge BDH profile-scoped già risolto con `BDH_VAULT_ID` esterno.

**Non è ancora implementato:** canonical Bot Chat, mention autocomplete, user-to-Bot handoff, reply attribuita, dedupe/retry, context policy e cross-device lifecycle.

## Definizione di “100% ready” per il primo release

Non significa multi-gateway o group chat. Per la prima release significa:

1. un Bot marcato nel roster può aprire/riprendere la propria canonical `Bot Chat`;
2. `@bot` nella Chat MC invia una richiesta nel profilo target, senza alterare il transcript origine;
3. MC mostra stato e risposta attribuita con correlazione verificabile;
4. retry/reconnect/doppio click non duplicano né Bot Chat né richiesta;
5. il contesto condiviso è esplicito e bounded;
6. Crossnection può coordinare specialisti **solo dal proprio Bot Chat**;
7. test automatici e manuali coprono desktop, mobile, refresh e reconnect;
8. nessuna modifica Hermes core richiesta.

P2 (multi-gateway, relay, group chat) resta deliberatamente fuori: aggiungerlo ora trasformerebbe un prodotto funzionante in una bella demo distribuita che mente.

---

## Phase 0 — Stabilizzare il foundation branch

### Task 0.1: Split e review del branch attuale

**Files:**
- Review: `src/routes/BotsRoute.tsx`
- Review: `src/lib/bot-gateway.ts`
- Review: `src/components/Modal.tsx`
- Review: `src/components/ui/Dropdown.tsx`

**Actions:**
1. Aprire una PR da `feat/bot-mode` verso `main` con una descrizione esplicita: roster/editor foundation, non routing operativo.
2. Rieseguire `node ./node_modules/vite/bin/vite.js build` e `git diff --check` sul commit della PR.
3. Aggiungere test puri per i normalizer del roster (`is_bot`, model option, toolset pin) prima di estendere il trasporto.
4. Non mescolare nel primo commit handoff il refactor UI già fatto.

**Acceptance:** la PR foundation è recensibile da sola e non finge di includere la feature end-to-end.

### Task 0.2: Ridurre il trasporto Bot a un client RPC testabile

**Files:**
- Modify: `src/lib/bot-gateway.ts`
- Create: `src/lib/bot-gateway.test.ts` (o usare il test runner già scelto dal repo)

**Actions:**
1. Estrarre/centralizzare timeout e error conversion per i request one-shot.
2. Testare risposta, errore, close, timeout e profile parameter invariato.
3. Esporre solo helper domain-level (`loadProfiles`, `describeProfile`, `configureToolsets`, `installSkill`), non un RPC generico esportato alla UI.

**Acceptance:** un fallimento gateway diventa un errore UI leggibile e non uno spinner di due minuti.

---

## Phase 1 — Canonical Bot Chat

### Task 1.1: Implementare resolver canonico

**Files:**
- Create: `src/lib/bot-chat-routing.ts`
- Modify: `src/lib/chat-protocol.ts` solo se servono tipi RPC condivisi
- Test: `src/lib/bot-chat-routing.test.ts`

**Contract:**

```ts
type CanonicalBotChat = {
  profile: string;
  title: 'Bot Chat';
  sessionId: string;
  resolvedId?: string;
  created: boolean;
};

resolveCanonicalBotChat(profile, accessToken): Promise<CanonicalBotChat>
```

**Algorithm:**
1. `session.list` profile-scoped con `include_hidden: true`.
2. Cercare esattamente `title === 'Bot Chat'` per il profilo richiesto.
3. Se esiste, `session.resume` e restituire il vincitore.
4. Se non esiste, `session.create` con `{ profile, title: 'Bot Chat', hidden: true }`.
5. Dopo create, rileggere/riprendere; in una race adottare la sessione già esistente, mai inventare un ID persistito client-side.
6. Normalizzare `/new` a `/compact` come da contratto desktop Hermes, se richiesto dal payload.

**Tests:**
- sessione esistente riusata;
- create quando assente;
- race create → adoption;
- due richieste parallele non espongono due identità UI;
- profile passato letteralmente, senza fallback al default.

**Acceptance:** canonical identity = `(profile, 'Bot Chat')`, mai localStorage/session-id come identità.

### Task 1.2: Entrypoint roster → Bot Chat

**Files:**
- Modify: `src/routes/BotsRoute.tsx`
- Modify/create: `src/components/bots/BotRosterItem.tsx`
- Modify: `src/lib/bot-chat-routing.ts`

**Actions:**
1. Aggiungere CTA esplicita `Apri Bot Chat` su ogni Bot roster item o dentro la detail modal.
2. Aprire il target in ChatDrawer usando session/profile target, non cambiando la chat utente corrente in modo silenzioso.
3. Renderizzare loading/error per canonical lookup.

**Acceptance:** da roster posso entrare in Crossnection Bot Chat, refreshare e ritrovare la stessa sessione canonical.

---

## Phase 2 — User → Bot handoff via mention

### Task 2.1: Parsing e autocomplete del roster live

**Files:**
- Create: `src/lib/bot-mentions.ts`
- Modify: componente composer in `src/components/ChatDrawer.tsx` o il componente composer effettivamente usato
- Test: `src/lib/bot-mentions.test.ts`

**Rules:**
1. Intercettare solo `@handle` che coincidono con un Bot marcato nel roster live.
2. `@email`, handle sconosciuti e testo normale passano invariati al prompt ordinario.
3. Autocomplete mostra display name, profile handle e description; tastiera e mobile devono funzionare.
4. Il parser estrae il solo testo dopo la mention come request iniziale.

**Acceptance:** `@crossnection analizza questo trace` è un handoff; `ciao@azienda.it` non lo è.

### Task 2.2: Handoff envelope + dedupe

**Files:**
- Create: `src/lib/bot-handoff.ts`
- Modify: `src/lib/bot-chat-routing.ts`
- Test: `src/lib/bot-handoff.test.ts`

**Envelope:**

```ts
{
  handoffId: 'mc-handoff-<uuid>',
  origin: { connectionId: 'local', profile, sessionId },
  target: { profile, canonicalTitle: 'Bot Chat' },
  request: string,
  context: { mode: 'none', messages: [] }
}
```

**Actions:**
1. Risolvere la canonical target prima del submit.
2. Inviare `prompt.submit` alla sessione target con `profile` esplicito e envelope leggibile nel contenuto/metadata supportato.
3. Conservare una correlation record locale scoped `(connection, originSession, handoffId)` solo per la durata dell’handoff; non usarla come identità canonical.
4. Dedupe: stesso handoffId non può fare un secondo `prompt.submit` dopo retry/reconnect.

**Acceptance:** doppio click, StrictMode/reconnect e refresh non producono un doppio lavoro Bot.

### Task 2.3: Status e reply attribuita nella chat origine

**Files:**
- Modify: `src/lib/chat-gateway.ts`
- Modify: `src/components/ChatDrawer.tsx`
- Create: `src/components/chat/BotHandoffMessage.tsx`
- Test: unit/component test del reducer di handoff

**Actions:**
1. Osservare eventi della target session con correlazione `handoffId`.
2. Mostrare in origine `queued → running → completed | failed`.
3. Mostrare la risposta finale come card/messaggio UI attribuito al Bot, separato dai messaggi Hermes della chat origine.
4. Non inserire un falso messaggio `user` o `assistant` nel transcript originale.
5. Error state deve esporre `Retry`, mai retry automatico cieco.

**Acceptance:** l’utente vede chi ha risposto, dove la richiesta gira e può distinguere un errore da un Bot lento.

---

## Phase 3 — Context policy e sicurezza

### Task 3.1: Context opt-in bounded

**Files:**
- Create: `src/components/chat/HandoffContextPicker.tsx`
- Modify: `src/components/ChatDrawer.tsx`
- Modify: `src/lib/bot-handoff.ts`

**Modes:**
- `none` (default): solo testo esplicito dopo mention;
- `selected`: messaggi scelti dall’utente;
- `session-summary`: riassunto già disponibile;
- `full`: richiede conferma esplicita.

**Rules:**
- mai inoltrare automaticamente tool output, secrets, SOUL, system prompt o conversazioni private;
- visualizzare esattamente cosa sta partendo prima del submit;
- limitare messaggi/byte/token budget.

**Acceptance:** il contesto è una scelta visibile, non una fuga di dati mascherata da UX comoda.

### Task 3.2: Permission/lifecycle guard

**Files:**
- Modify: `src/lib/bot-gateway.ts`
- Modify: `src/routes/BotsRoute.tsx`
- Modify: `src/lib/bot-chat-routing.ts`

**Actions:**
1. Bot non marcato → non mentionabile.
2. Profile delete/disable → CTA e autocomplete spariscono al refresh roster.
3. Gateway che non supporta RPC profile-scoped → feature disabilitata con errore esplicito.
4. `message_agent` è disponibile/raccomandato solo nel canonical Bot Chat, non come shortcut nella chat origine.

**Acceptance:** nessun routing “per nome” fragile, nessun fallback silenzioso al profile default.

---

## Phase 4 — Crossnection P1: Bot → specialist coordination

### Task 4.1: Validare il runtime Crossnection

**Files/config:**
- Profile: `~/.hermes/profiles/crossnection/SOUL.md`
- Profile: `~/.hermes/profiles/crossnection/.env`
- MC test harness/documentation

**Actions:**
1. Verificare dal canonical Bot Chat che Crossnection possieda toolset e policy necessari a `message_agent`.
2. Verificare che ogni `bdh_query` / `bdh_stats` usi `vault_id: 'crossnection'` esplicito.
3. Eseguire una handoff reale a Crossnection e una delega a un solo specialista rilevante.
4. Verificare che Crossnection sintetizzi un risultato e non faccia fan-out cerimoniale.

**Acceptance:** MC non orchestra Bot-to-Bot; Crossnection/Hermes lo fa nel proprio spazio di lavoro.

---

## Phase 5 — Release gate

### Automated verification

1. Unit test routing/mention/envelope/dedupe.
2. Gateway integration test con profilo fixture:
   - canonical existing/create/race;
   - profile scoping;
   - target reply attribution;
   - failed target + explicit retry.
3. Vite build e TypeScript check senza nuovi errori.

### Manual verification matrix

| Scenario | Expected |
|---|---|
| Desktop fresh session | `@crossnection` autocomplete, status, reply attributed |
| iPhone/Tailscale | stesso handoff e ordering UI, niente session ID divergenti |
| Refresh durante running | stato ripreso, nessun secondo prompt submit |
| Browser reconnect | handoff non duplicato, reply finale visibile |
| Unknown mention | prompt originario invariato |
| Profile removed | handle non più risolvibile |
| Context none | solo request esplicita target |
| Context selected | solo messaggi confermati target |

### Go / no-go

**GO** solo quando tutte le acceptance P0/P1 passano e una demo reale `chat origine → Crossnection → specialist → reply attribuita` funziona su desktop e mobile.

**NO-GO** se dobbiamo modificare Hermes core, persistere canonical session ID come identità, o inoltrare contesto automaticamente.

---

## Deferred deliberately

- multi-gateway connection registry;
- remote Bot / relay routing;
- group chat multi-agent;
- autonomous periodic Bot routines;
- routing per model/name senza marker di roster;
- full transcript forwarding default.
