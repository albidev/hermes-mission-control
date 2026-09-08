# Mission Control Chat Cross-Device Handoff Implementation Plan

> **For Hermes:** Use the Kanban workflow for execution; keep the live detached worktree untouched.

**Goal:** Make Mission Control chat session resume and desktop↔iPhone handoff lossless for idle and live sessions.

**Architecture:** The telemetry sidecar becomes the authoritative coordinator for the *active-chat pointer* while the Hermes gateway remains authoritative for durable transcript data. A client must complete an explicit bootstrap/read phase before it opens the WebSocket or publishes a pointer. Resume snapshots must merge into, never replace, already-visible relay/optimistic state.

**Tech Stack:** React/TypeScript + Vite, Python stdlib telemetry sidecar, Hermes dashboard JSON-RPC WebSocket, authenticated SSE relay, Node behavior tests, pytest relay tests.

**Branch and deployment constraints:**
- Do **not** modify `/Users/albi/Projects/hermes-mission-control/.worktrees/idle-trigger-integrated`; it is the live detached worktree at `56ea689`.
- Main is local `95b968d` and is two commits ahead of `origin/main`; inspect/reconcile that state before any push.
- Build the clean hotfix from local `main` on `fix/chat-cross-device-handoff`.
- For an urgent deployment, merge the accepted hotfix into a separately named integration branch based on `56ea689`; do not reset, checkout, or rebase the live worktree.
- No Hermes-core changes.

---

## Acceptance criteria

1. A cold device/browser with stale or empty `localStorage` cannot overwrite `/api/local/chat/last` before it has read and adopted the server pointer.
2. Opening iPhone after desktop selects the same stored session deterministically.
3. `session.resume` plus any concurrent sidecar relay events preserves the union of durable, optimistic, streaming, tool, and mirrored user messages; no state update may shrink the visible transcript.
4. The last-chat pointer only advances after a meaningful ownership event: successful `session.create`, successful `session.resume`, explicit session selection/resume, or user submit. Passive React mounts/renders do not claim it.
5. A stale/out-of-order pointer write is rejected or returns the authoritative pointer; server ordering is revision-based, not client `Date.now()`-based.
6. Existing single-device reconnect/outbox behavior remains intact.
7. No service restart, Git push, merge, or live checkout change occurs without explicit approval.

---

### Task 1: Establish a clean, isolated hotfix workspace

**Objective:** Keep production and unrelated Curate/idle work isolated from the chat fix.

**Files:** None.

**Step 1: Inspect branch ancestry and remote divergence**

Run:
```bash
git -C /Users/albi/Projects/hermes-mission-control status --short --branch
git -C /Users/albi/Projects/hermes-mission-control log --oneline origin/main..main
git -C /Users/albi/Projects/hermes-mission-control/.worktrees/idle-trigger-integrated diff --stat main..HEAD
```

**Expected:** Main's two unpushed commits and live-only work are identified before any integration/push decision.

**Step 2: Create the clean worktree from local main**

```bash
git -C /Users/albi/Projects/hermes-mission-control worktree add \
  -b fix/chat-cross-device-handoff \
  /Users/albi/Projects/hermes-mission-control/.worktrees/chat-cross-device-handoff \
  main
```

**Step 3: Verify isolation**

```bash
git -C /Users/albi/Projects/hermes-mission-control/.worktrees/chat-cross-device-handoff status --short --branch
```

Expected: clean `fix/chat-cross-device-handoff`; the live worktree is unchanged.

---

### Task 2: Add server-owned pointer revision behavior tests

**Objective:** Specify how the sidecar rejects stale pointer claims and preserves a server-authoritative active chat.

**Files:**
- Modify: `server/last_chat_store.py`
- Create or modify: `server/tests/test_last_chat_store.py`
- Modify: `server/local_telemetry_server.py`

**Step 1: Write failing backend behavior tests**

Cover:
- first claim creates `{sessionId, sessionKey, revision}`;
- a write with matching `expectedRevision` advances the pointer and revision;
- a stale `expectedRevision` cannot replace a newer server pointer;
- server assigns ordering metadata; client-supplied wall-clock time cannot determine conflict resolution;
- an invalid/missing session id remains a 400 at the HTTP boundary.

**Step 2: Run targeted test red**

```bash
/Users/albi/.hermes/hermes-agent/venv/bin/python -m pytest server/tests/test_last_chat_store.py -q
```

Expected: failure because revision/CAS behavior does not exist.

**Step 3: Implement minimal sidecar contract**

- Extend persisted `last_chat.json` with `revision` and server-generated `updatedAt`.
- Update `set_last_chat()` to accept an expected revision and return either an accepted pointer or a conflict/current pointer.
- Make `POST /api/local/chat/last` expose an explicit conflict status/payload without accepting stale writes.
- Preserve the authenticated `/api/local/chat/last` GET contract.

**Step 4: Run targeted test green**

```bash
/Users/albi/.hermes/hermes-agent/venv/bin/python -m pytest server/tests/test_last_chat_store.py -q
```

**Step 5: Commit**

```bash
git add server/last_chat_store.py server/local_telemetry_server.py server/tests/test_last_chat_store.py
git commit -m "fix(chat): make last chat pointer revisioned"
```

---

### Task 3: Extract client bootstrap and pointer-claim policy into a testable helper

**Objective:** Prevent stale local state from writing over the server during mount.

**Files:**
- Create: `src/lib/chat-bootstrap.ts`
- Modify: `src/lib/chat-persistence.ts`
- Modify: `src/lib/chat-gateway.ts`
- Create or modify: `tests/chat-bootstrap.test.ts`

**Step 1: Write failing behavior tests**

Test pure state transitions:
- empty local cache + server pointer → adopt server pointer;
- stale/different local pointer + server pointer → adopt server pointer, emit no claim;
- no server pointer + valid local pointer → adopt local pointer and issue one initial claim;
- explicit local user action after bootstrap → issue a compare-and-swap pointer claim;
- rejected stale claim → retain/adopt the returned authoritative pointer.

**Step 2: Run test red**

```bash
node --experimental-strip-types tests/chat-bootstrap.test.ts
```

**Step 3: Implement bootstrap policy**

- Add a bootstrap result/state helper with no DOM/network side effects.
- Fetch the server pointer first whenever the generic drawer opens.
- Do not invoke `persistChat()`/`syncLastChatToServer()` until bootstrap resolves.
- Keep transcript persistence local, but split it from pointer ownership writes.
- Include the known server revision in pointer claims.
- Treat `New`, an explicit session resume, and a successful prompt/session creation as intentional claims; passive state effects are never claims.

**Step 4: Run behavior test green**

```bash
node --experimental-strip-types tests/chat-bootstrap.test.ts
```

**Step 5: Commit**

```bash
git add src/lib/chat-bootstrap.ts src/lib/chat-persistence.ts src/lib/chat-gateway.ts tests/chat-bootstrap.test.ts
git commit -m "fix(chat): bootstrap shared session before local persistence"
```

---

### Task 4: Make initial resume hydration monotonic

**Objective:** A late/partial `session.resume` response must not erase events received from another device.

**Files:**
- Modify: `src/lib/chat-gateway.ts`
- Modify: `src/lib/chat-sync.ts`
- Modify: `tests/chat-sync.test.ts`
- Create or modify: `tests/chat-resume-handoff.test.ts`

**Step 1: Write failing regression tests**

Cover the following chronological scenario:
1. device A has a durable transcript and receives a live mirrored user/assistant event;
2. device B adopts the shared pointer and starts `session.resume`;
3. device B receives a relay event while the resume request is in flight;
4. resume returns a transcript that does not yet include that event;
5. visible state still contains all durable messages plus the relay event, in order, with no duplicated optimistic user row.

Also test a partial/empty snapshot: it must never clear existing visible messages.

**Step 2: Run tests red**

```bash
node --experimental-strip-types tests/chat-sync.test.ts
node --experimental-strip-types tests/chat-resume-handoff.test.ts
```

**Step 3: Implement a single monotonic hydration path**

- Route both initial `ensureSession()` resume and periodic `reconcileSessionSnapshot()` through the same snapshot-merging helper.
- Replace initial `setMessages(transcript)` with functional `mergeDurableChatMessages(current, snapshot)`.
- Preserve current streaming assistant/tool detail and optimistic user messages until the durable snapshot contains them.
- Do not clear messages merely because the gateway returns an empty/partial transcript.
- Keep gateway event watermarks and relay sequence state scoped by stored session key so a device handoff cannot mix old and new sessions.

**Step 4: Run tests green**

```bash
node --experimental-strip-types tests/chat-sync.test.ts
node --experimental-strip-types tests/chat-resume-handoff.test.ts
node --experimental-strip-types tests/chat-protocol.test.ts
```

**Step 5: Commit**

```bash
git add src/lib/chat-gateway.ts src/lib/chat-sync.ts tests/chat-sync.test.ts tests/chat-resume-handoff.test.ts
git commit -m "fix(chat): preserve relay events across session resume"
```

---

### Task 5: Validate the actual two-client handoff contract

**Objective:** Verify the composed sidecar + client behavior instead of trusting isolated helpers.

**Files:**
- Modify or create: `server/tests/test_chat_handoff_contract.py`
- Modify: `tests/chat-ui-contract.test.ts` only if a UI-visible contract needs coverage
- Update: `docs/chat.md`

**Step 1: Add integration-style contract coverage**

Use an in-process `ChatSyncRelay` and `last_chat_store` temporary path to exercise:
- desktop publishes pointer after a meaningful action;
- iPhone bootstrap reads it and does not overwrite it with a stale local candidate;
- messages published during mobile resume remain visible after snapshot merge;
- stale pointer claim receives the canonical server pointer.

**Step 2: Run focused validation**

```bash
/Users/albi/.hermes/hermes-agent/venv/bin/python -m pytest \
  server/tests/test_last_chat_store.py \
  server/tests/test_chat_sync_relay.py \
  server/tests/test_chat_handoff_contract.py -q
node --experimental-strip-types tests/chat-bootstrap.test.ts
node --experimental-strip-types tests/chat-sync.test.ts
node --experimental-strip-types tests/chat-resume-handoff.test.ts
node --experimental-strip-types tests/chat-protocol.test.ts
node --experimental-strip-types tests/chat-ui-contract.test.ts
node ./node_modules/vite/bin/vite.js build
```

**Step 3: Update documentation**

Document the new semantics in `docs/chat.md`:
- server pointer is bootstrap authority;
- localStorage is transcript cache/fallback, not a mount-time source of truth;
- pointer claims are revisioned and intentional;
- initial resume is monotonic and relay-safe.

**Step 4: Commit**

```bash
git add server/tests/test_chat_handoff_contract.py docs/chat.md tests/chat-ui-contract.test.ts
git commit -m "test(chat): cover cross-device resume handoff"
```

---

### Task 6: Independent review and deployment preparation

**Objective:** Separate implementation acceptance from release operations.

**Files:** No source changes expected.

**Step 1: Review the hotfix diff**

- Confirm only `chat-*`, `last_chat_store`, telemetry route, exact tests, and `docs/chat.md` changed.
- Confirm no Hermes-core path changed.
- Check no token, generated state, or `last_chat.json` is staged.
- Run `git diff --check`.

**Step 2: Run final verification**

Repeat the focused tests and Vite build from Task 5. Compare TypeScript diagnostics against baseline; accept no new error in changed files.

**Step 3: Create deployment integration branch only after explicit approval**

```bash
# preserve live history, then merge the reviewed hotfix into it
git -C /Users/albi/Projects/hermes-mission-control branch integration/live-chat-handoff 56ea689
git -C /Users/albi/Projects/hermes-mission-control worktree add \
  /Users/albi/Projects/hermes-mission-control/.worktrees/live-chat-handoff \
  integration/live-chat-handoff
git -C /Users/albi/Projects/hermes-mission-control/.worktrees/live-chat-handoff merge --no-ff fix/chat-cross-device-handoff
```

**Step 4: Read back and approve before operations**

Before any push or service restart, show:
- integration branch diff and ancestry;
- exact Vite/telemetry worktree switch or deployment procedure;
- current `main` versus `origin/main` divergence;
- test/build outputs.

---

## Kanban execution graph

1. **Triage/spec card** — captures the acceptance contract and branch constraints; no code.
2. **Backend pointer CAS** — depends on triage.
3. **Client bootstrap policy** — depends on backend contract.
4. **Resume monotonic merge** — can run in parallel with backend/client only after its shared contract is locked; use a separate worktree.
5. **Handoff integration/E2E contract** — depends on 2–4.
6. **Independent review** — depends on 5.
7. **Deployment preparation** — blocked behind explicit human approval and completion of review.

## Model assignment

- **Triage/specification:** `poolside/laguna-s-2.1:free` via OpenRouter — this is the currently configured `auxiliary.triage_specifier`; it is adequate for turning a bounded diagnosis into an acceptance-criteria card.
- **Implementation and test tasks:** pin `gpt-5.6-terra-900k` via `openai-codex` when the Kanban worker can authenticate to that provider. It is the active chat model and is the right choice for this stateful concurrency fix.
- **Fallback implementation model:** `openrouter/free` only for narrow mechanical test or documentation work; do not give it ownership of the bootstrap/merge architecture.
- **Independent review:** a separate `gpt-5.6-terra-900k` review worker, read-only, plus Hermes reruns the tests. Codex may be used as an isolated input lane, never as Kanban owner or final verifier.

## Risks and decisions

- A pointer is not a transcript: sidecar state must never replace gateway durable history.
- The dashboard gateway intentionally rebinds a live session transport to the latest resumed client. The sidecar relay is therefore mandatory for the non-owning client.
- Timestamp-only last-writer-wins is invalid because passive mounts manufacture new timestamps. Use server revisions/CAS.
- Do not deploy `main` over the live detached worktree; it would drop the live-only sidecar/Curate changes.
- Treat sidecar restart as a separate approved operation because it clears the in-memory relay ring; durable `session.resume` must remain the fallback.
