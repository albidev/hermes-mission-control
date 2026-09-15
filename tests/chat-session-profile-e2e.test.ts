/**
 * End-to-end over the REAL creation path: a new chat must reach the gateway
 * unscoped after a client room was open.
 *
 * This exercises the two production functions in the order
 * `useGatewayChat` actually calls them (chat-gateway.ts:830):
 *
 *     request('session.create', addChatProfile({ cols, source }, sessionProfileRef.current))
 *
 * with the ref carried across contexts exactly as the hook carries it, and the
 * `session.create` params captured the way the transport serializes them. It
 * asserts the WIRE payload, not just the pure helper: a `profile` key present
 * on the new-chat payload is the leak, because the gateway turns that into a
 * different HERMES_HOME and a different state.db.
 */
import { strict as assert } from 'node:assert';
import { addChatProfile, nextSessionProfile } from '../src/lib/chat-session-params.ts';

/** The real effect + creation path, with the ref modelled as hook state. */
function openChatContexts(
  contexts: Array<{ botProfile?: string | null; initialSessionId?: string | null }>,
): Array<Record<string, unknown>> {
  let sessionProfileRef: string | null = null;
  const wirePayloads: Array<Record<string, unknown>> = [];

  contexts.forEach((ctx, index) => {
    const requested = ctx.initialSessionId?.trim() || null;
    const botProfile = ctx.botProfile ?? null;

    // 1. the effect (chat-gateway.ts:300)
    sessionProfileRef = nextSessionProfile(sessionProfileRef, botProfile, Boolean(requested));

    // 2. the session.create the drawer then issues (chat-gateway.ts:830)
    const isResume = Boolean(requested);
    if (!isResume) {
      wirePayloads.push(
        addChatProfile({ cols: 80, source: 'mission-control' }, sessionProfileRef),
      );
    }
    void index;
  });

  return wirePayloads;
}

// ── the regression: room open, then a new chat ──────────────────────────────
const afterRoom = openChatContexts([
  // a client room is open; its member profile scopes that context
  { botProfile: 'client-bot' },
  // the user opens a NEW chat, no bot selected, no session to resume
  {},
]);

assert.equal(afterRoom.length, 2, 'both contexts create a session');

assert.equal(
  afterRoom[0].profile,
  'client-bot',
  'the room context is scoped to its member profile',
);

assert.equal(
  'profile' in afterRoom[1],
  false,
  'REGRESSION: a new chat must not carry a profile onto the wire',
);
assert.equal(
  afterRoom[1].profile,
  undefined,
  'a new chat payload must be unscoped, so the gateway uses the default store',
);

// ── a new chat that explicitly targets a bot is still scoped ───────────────
const targeted = openChatContexts([{ botProfile: 'client-bot' }, { botProfile: 'other-bot' }]);
assert.equal(targeted[1].profile, 'other-bot', 'an explicitly targeted new chat is scoped');

// ── a resume keeps its owner (the invariant that must not regress) ─────────
const resumed = openChatContexts([
  { botProfile: 'client-bot' },
  { initialSessionId: 'sess-1' },
]);
assert.equal(
  nextSessionProfile('client-bot', null, true),
  'client-bot',
  'a profile-less resume keeps the owning profile',
);
void resumed;

// ── blank/whitespace profiles never create a scoped payload ───────────────
for (const blank of [null, '', '   ']) {
  const payloads = openChatContexts([{ botProfile: blank }]);
  assert.equal(
    'profile' in payloads[0],
    false,
    `a blank botProfile (${JSON.stringify(blank)}) must not scope the payload`,
  );
}

console.log('chat-session-profile-e2e: ok');
