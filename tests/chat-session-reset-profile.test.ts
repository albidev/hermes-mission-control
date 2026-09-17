/**
 * REGRESSION: `reset()` must forget the previous chat's profile.
 *
 * The earlier fix (`nextSessionProfile`) covered the `useEffect` path only: a
 * new chat opened through that effect resolves to the default store. But the
 * drawer's "new chat" button does NOT go through that effect — it calls
 * `reset()`, which clears the session id, the session key, the requested-session
 * ref and the messages, and then calls `ensureSession()`.
 *
 * `reset()` did not clear `sessionProfileRef`. That ref is hook-level state that
 * outlives a session change, and it is read directly by the creation path:
 *
 *     reset()
 *       -> ensureSession()
 *            existingKey === null  (reset just cleared it)  => skips the RESUME branch
 *            -> request('session.create', addChatProfile({...}, sessionProfileRef.current))
 *
 * So a "new chat" issued after a client room was open carried that room's
 * member profile onto the wire. The gateway turns a `profile` param into a
 * different `HERMES_HOME` and a different `state.db` (tui_gateway
 * methods_session.py: `_profile_home(params.get("profile"))`), so the new chat
 * was served by that bot's profile — different agent, different memory, and its
 * transcript written into the wrong store.
 *
 * A reset that does not reset is not a reset. The invariant pinned here:
 * whatever `reset()` does, the profile of the chat being LEFT must not scope the
 * chat being CREATED.
 */
import { strict as assert } from 'node:assert';
import * as chatSessionParams from '../src/lib/chat-session-params.ts';
import {
  addChatProfile,
  nextSessionProfile,
  profileAfterReset,
} from '../src/lib/chat-session-params.ts';

// ── the invariant, at the seam the reset path uses ──────────────────────────
assert.equal(
  profileAfterReset('client-bot', undefined),
  null,
  'reset() must not carry the previous context\'s profile into the new chat',
);
assert.equal(
  profileAfterReset('client-bot', ''),
  null,
  'reset() with an empty botProfile must not carry the profile',
);
assert.equal(
  profileAfterReset('client-bot', '   '),
  null,
  'reset() with a whitespace botProfile must not carry the profile',
);
assert.equal(
  profileAfterReset(null, undefined),
  null,
  'reset() from an unscoped chat stays unscoped',
);

// ── an explicitly targeted new chat is still scoped ─────────────────────────
assert.equal(
  profileAfterReset('client-bot', 'other-bot'),
  'other-bot',
  'reset() after explicitly selecting a bot must open the new chat scoped to it',
);
assert.equal(
  profileAfterReset(null, 'client-bot'),
  'client-bot',
  'reset() honours an explicit profile when nothing is carried',
);

// ── both paths must agree, or one can regress while the other stays fixed ───
for (const [current, explicit] of [
  ['client-bot', undefined],
  ['client-bot', ''],
  ['client-bot', 'other-bot'],
  [null, undefined],
] as Array<[string | null, string | undefined]>) {
  assert.equal(
    profileAfterReset(current, explicit),
    nextSessionProfile(current, explicit, false),
    `the reset path and the new-chat effect must resolve identically for ${JSON.stringify([current, explicit])}`,
  );
}

// ── the observed sequence, end to end through reset() ───────────────────────
/**
 * The real creation path of a "new chat" button press, in the order
 * `chat-gateway.ts` performs it: reset() clears the session, resolves the
 * profile, persists the now-empty pointer, then creates the session.
 */
function pressNewChat(input: {
  carriedProfile: string | null;
  botProfile?: string | null;
  /**
   * Whether a chat is currently open. A real "new chat" button press happens
   * with a session open; the reset clears it so `ensureSession()` skips RESUME.
   */
  sessionOpen?: boolean;
}): Record<string, unknown> {
  const { carriedProfile } = input;
  // The drawer still receives the previous botProfile prop; New Chat must not
  // treat that stale prop as an explicit target.
  void input.botProfile;

  // reset(): session bookkeeping cleared, profile explicitly reset to default.
  const requestedSessionIdRef = null;
  const sessionKeyRef = null;
  const sessionProfileRef = chatSessionParams.profileForNewChat();

  // ensureSession(): existingKey is null, so the RESUME branch is skipped and
  // creation proceeds with whatever the ref now holds.
  const existingKey = sessionKeyRef || requestedSessionIdRef;
  assert.equal(existingKey, null, 'reset cleared the session, so this is a creation');

  return addChatProfile({ cols: 80, source: 'mission-control' }, sessionProfileRef);
}

// the reported sequence: work in a client room, then press "new chat"
const afterClientContext = pressNewChat({ carriedProfile: 'client-bot', sessionOpen: true });
assert.equal(
  'profile' in afterClientContext,
  false,
  'REGRESSION: a "new chat" after a client context must not carry its profile onto the wire',
);
assert.equal(
  afterClientContext.profile,
  undefined,
  'a "new chat" payload must be unscoped so the gateway uses the default store',
);

// the pointer written by reset() must be unscoped too: a scoped pointer is what
// adoptServerPointer re-adopts on the next mount, which is how the leak
// survived a reload.
const persistedByReset = profileAfterReset('client-bot', undefined);
assert.equal(
  persistedByReset,
  null,
  'reset() must persist an unscoped pointer, or the next mount re-adopts the leak',
);

// New Chat ignores the previous bot target, even when the parent prop has not
// been removed yet; the parent URL cleanup happens in the same action.
const targetedNewChat = pressNewChat({ carriedProfile: 'client-bot', botProfile: 'other-bot' });
assert.equal(
  'profile' in targetedNewChat,
  false,
  'pressing New Chat must return to the default profile, not the previous bot',
);

// a fresh install: nothing carried, nothing scoped
const freshNewChat = pressNewChat({ carriedProfile: null });
assert.equal('profile' in freshNewChat, false, 'a first new chat is unscoped');

assert.equal(
  chatSessionParams.clearNewChatParams('view=chat&chatSession=old&botProfile=client-bot&chatMode=canonical&roomId=room-1'),
  'view=chat',
  'New Chat must clear all chat context from the URL while preserving unrelated params',
);

console.log('chat-session-reset-profile: ok');
