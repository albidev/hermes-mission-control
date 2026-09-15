/**
 * The profile ref must never leak from one chat context into a NEW chat.
 *
 * Regression (observed live): a client room was open in the drawer, then a
 * normal chat was opened. The chat was created with the ROOM's member profile
 * instead of the default one, so it was served by that bot's profile — a
 * different HERMES_HOME, a different state.db, a different agent — and its
 * memory landed in the wrong store.
 *
 * `sessionProfileRef` is hook-level state that outlives a session change.
 * `nextSessionProfile` was written for the RESUME case ("a profile-less resume
 * must keep the known owner"), and its `!hasRequestedSession` branch is a
 * deliberate no-op — correct while the ref still describes the same session,
 * wrong the moment the context changes. A fresh chat (`hasRequestedSession`
 * false, no explicit `?botProfile=`) is exactly that moment: the ref describes
 * the PREVIOUS context and must reset to the default instead of being carried.
 *
 * The invariant pinned here: a NEW chat with no explicit profile resolves to
 * the default (null = unscoped/default store), never to a carried-over owner.
 */
import { strict as assert } from 'node:assert';
import { resolveSessionOwner, nextSessionProfile } from '../src/lib/chat-session-params.ts';

// --- the regression: a carried profile must not reach a new chat -----------
assert.equal(
  nextSessionProfile('client-bot', undefined, false),
  null,
  'a new chat must not adopt the profile of the previously open context',
);
assert.equal(
  nextSessionProfile('client-bot', '', false),
  null,
  'a new chat with an empty botProfile must not adopt the carried profile',
);
assert.equal(
  nextSessionProfile('client-bot', '   ', false),
  null,
  'a new chat with a whitespace botProfile must not adopt the carried profile',
);

// --- a new chat that DOES name a bot keeps it -----------------------------
assert.equal(
  nextSessionProfile('other-bot', 'client-bot', false),
  'client-bot',
  'an explicit profile is still honoured when opening a new chat',
);
assert.equal(
  nextSessionProfile(null, 'client-bot', false),
  'client-bot',
  'an explicit profile opens a new chat scoped to it',
);

// --- resume behaviour is unchanged (the invariant that must survive) ------
assert.equal(
  nextSessionProfile('client-bot', undefined, true),
  'client-bot',
  'a profile-less resume must keep the known owner',
);
assert.equal(
  nextSessionProfile('client-bot', '', true),
  'client-bot',
  'an empty botProfile must not clear the known owner on resume',
);
assert.equal(
  nextSessionProfile('client-bot', '   ', true),
  'client-bot',
  'a whitespace botProfile must not clear the known owner on resume',
);
assert.equal(
  nextSessionProfile('client-bot', 'other-profile', true),
  'other-profile',
  'an explicit profile replaces the known owner on resume',
);
assert.equal(
  nextSessionProfile(null, 'client-bot', true),
  'client-bot',
  'an explicit profile is adopted when nothing is known yet',
);

// --- the observed sequence, end to end through the ref lifecycle ----------
// Simulates what the effect does across contexts on ONE mounted hook: the ref
// is carried between calls, exactly as `sessionProfileRef.current` is.
function refLifecycle(steps: Array<{ explicit?: string; requested: boolean }>): Array<string | null> {
  let current: string | null = null;
  return steps.map((step) => {
    current = nextSessionProfile(current, step.explicit, step.requested);
    return current;
  });
}

assert.deepEqual(
  refLifecycle([
    // 1. a client room is open: its member profile scopes the context
    { explicit: 'client-bot', requested: false },
    // 2. a NEW chat is opened with no bot selected (the regression)
    { requested: false },
    // 3. the new chat is created — it must go to the default store
    { requested: false },
  ]),
  ['client-bot', null, null],
  'opening a new chat after a room must not inherit the room member profile',
);

assert.deepEqual(
  refLifecycle([
    { explicit: 'client-bot', requested: false },
    // a resume of a client-bot session must keep its owner
    { requested: true },
  ]),
  ['client-bot', 'client-bot'],
  'a profile-less resume must still keep the owning profile',
);

assert.deepEqual(
  refLifecycle([
    { explicit: 'client-bot', requested: false },
    // a new chat that explicitly targets another bot is scoped to it
    { explicit: 'other-bot', requested: false },
  ]),
  ['client-bot', 'other-bot'],
  'an explicitly targeted new chat adopts that profile',
);

// --- owner resolution from the sessions index ----------------------------
assert.equal(resolveSessionOwner({ profile: 'client-bot' }), 'client-bot');
assert.equal(resolveSessionOwner({ profile: '  client-bot  ' }), 'client-bot');
assert.equal(
  resolveSessionOwner({ profile: 'default' }),
  null,
  'the default profile is not a scoped owner: it must not be forwarded as one',
);
assert.equal(resolveSessionOwner({ profile: null }), null);
assert.equal(resolveSessionOwner({}), null);
assert.equal(resolveSessionOwner(null), null);
assert.equal(resolveSessionOwner(undefined), null);

console.log('chat-session-profile: ok');
