import { strict as assert } from 'node:assert';
import {
  buildLastChatClaimPayload,
  canClaimLastChatPointer,
  createChatBootstrapGuard,
  serverPointerMatchesRequestedSession,
  shouldAdoptServerPointer,
  type ServerLastChat,
} from '../src/lib/chat-bootstrap.ts';

const local = {
  sessionId: 'stale-local',
  sessionKey: 'stale-key',
  modelIdentity: null,
  messages: [],
  updatedAt: 9999999999999,
  revision: 3,
};
const server: ServerLastChat = {
  sessionId: 'canonical-server',
  sessionKey: 'canonical-key',
  modelIdentity: null,
  revision: 8,
  updatedAt: 1,
};

assert.equal(canClaimLastChatPointer('passive'), false);
assert.equal(canClaimLastChatPointer('bootstrap'), false);
assert.equal(canClaimLastChatPointer('create'), true);
assert.equal(canClaimLastChatPointer('resume'), true);
assert.equal(canClaimLastChatPointer('submit'), true);
assert.deepEqual(buildLastChatClaimPayload('sid', 'key', null, null), {
  sessionId: 'sid',
  sessionKey: 'key',
  modelIdentity: null,
});
assert.equal(buildLastChatClaimPayload('sid', 'key', null, 8).expectedRevision, 8);
assert.equal(shouldAdoptServerPointer(local, server), true);
assert.equal(shouldAdoptServerPointer({ ...local, sessionId: server.sessionId, revision: 8 }, server), false);
assert.equal(serverPointerMatchesRequestedSession('canonical-server', server), true);
assert.equal(serverPointerMatchesRequestedSession('canonical-key', server), true);
assert.equal(serverPointerMatchesRequestedSession('other-session', server), false);
assert.equal(serverPointerMatchesRequestedSession('canonical-server', { ...server, sessionKey: null }), true);

// A GET from the first open may resolve after close + reopen. Its aborted signal
// and obsolete generation must prevent it from replacing the new lifecycle.
const bootstrapGuard = createChatBootstrapGuard();
const firstAttempt = bootstrapGuard.begin();
let resolveFirstResponse: (pointer: ServerLastChat) => void = () => {};
const firstResponse = new Promise<ServerLastChat>((resolve) => {
  resolveFirstResponse = resolve;
});
const adopted: string[] = [];
const applyFirstResponse = firstResponse.then((pointer) => {
  if (bootstrapGuard.isCurrent(firstAttempt)) adopted.push(pointer.sessionId);
});
bootstrapGuard.invalidate();
const reopenedAttempt = bootstrapGuard.begin();
resolveFirstResponse({ ...server, sessionId: 'obsolete-first-open' });
await applyFirstResponse;
assert.equal(firstAttempt.signal.aborted, true);
assert.deepEqual(adopted, []);
assert.equal(bootstrapGuard.isCurrent(reopenedAttempt), true);

assert.equal(canClaimLastChatPointer('submit'), true);

console.log('chat bootstrap tests passed');
