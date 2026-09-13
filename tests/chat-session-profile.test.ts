/**
 * A session id belongs to exactly ONE profile's state.db.
 *
 * Regression: opening a session whose owner is not the default profile showed
 * the conversation and then blanked it. `useGatewayChat` resolved the profile
 * correctly while the drawer sat idle, but the resume effect reset it to
 * `null` whenever the deep link carried no `?botProfile=` — so the resume and
 * every later transcript lookup ran against the default store, missed, and the
 * drawer fell back to an empty preview.
 *
 * The invariant pinned here is the one that broke: a known profile survives a
 * profile-less resume, and only an explicitly supplied profile may replace it.
 * These assertions exercise the pure resolution rules, not the React effect.
 */
import { strict as assert } from 'node:assert';
import { resolveSessionOwner, nextSessionProfile } from '../src/lib/chat-session-params.ts';

// --- a known owner is never discarded by a profile-less resume -------------
assert.equal(
  nextSessionProfile('example-bot', undefined, true),
  'example-bot',
  'a profile-less resume must keep the known owner',
);
assert.equal(
  nextSessionProfile('example-bot', '', true),
  'example-bot',
  'an empty botProfile must not clear the known owner',
);
assert.equal(
  nextSessionProfile('example-bot', '   ', true),
  'example-bot',
  'a whitespace botProfile must not clear the known owner',
);

// --- an explicit profile wins --------------------------------------------
assert.equal(
  nextSessionProfile('example-bot', 'other-profile', true),
  'other-profile',
  'an explicit profile replaces the known owner',
);
assert.equal(
  nextSessionProfile(null, 'example-bot', true),
  'example-bot',
  'an explicit profile is adopted when nothing is known yet',
);

// --- no resume in flight: nothing changes --------------------------------
assert.equal(
  nextSessionProfile('example-bot', undefined, false),
  'example-bot',
  'without a requested session the profile is untouched',
);
assert.equal(nextSessionProfile(null, undefined, false), null);

// --- owner resolution from the sessions index ----------------------------
assert.equal(resolveSessionOwner({ profile: 'example-bot' }), 'example-bot');
assert.equal(resolveSessionOwner({ profile: '  example-bot  ' }), 'example-bot');
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
