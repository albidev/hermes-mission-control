import { strict as assert } from 'node:assert';
import {
  buildLastChatClaimPayload,
  canClaimLastChatPointer,
  shouldAdoptServerPointer,
  type ServerLastChat,
} from '../src/lib/chat-bootstrap.ts';
import type { ChatMessage } from '../src/lib/chat-protocol.ts';
import { applySyncedUserMessage, mergeDurableChatMessages } from '../src/lib/chat-sync.ts';

const message = (
  partial: Partial<ChatMessage> & Pick<ChatMessage, 'id' | 'role' | 'text'>,
): ChatMessage => ({
  createdAt: 1,
  status: 'complete',
  ...partial,
});

// Desktop's meaningful action establishes the canonical pointer. A cold iPhone
// with stale local state adopts it; bootstrap itself is never a claim.
const desktopPointer: ServerLastChat = {
  sessionId: 'desktop-session',
  sessionKey: 'desktop-key',
  revision: 4,
  updatedAt: 100,
};
assert.equal(
  shouldAdoptServerPointer({ sessionId: 'stale-iphone', revision: 99 }, desktopPointer),
  true,
);
assert.equal(canClaimLastChatPointer('bootstrap'), false);
assert.deepEqual(buildLastChatClaimPayload('desktop-session', 'desktop-key', null, null), {
  sessionId: 'desktop-session',
  sessionKey: 'desktop-key',
  modelIdentity: null,
});

// The mobile relay event arrives while session.resume is in flight. The
// partial snapshot omits it, but monotonic hydration keeps the union and order.
const durableUser = message({
  id: 'durable-user',
  role: 'user',
  kind: 'user',
  text: 'first question',
  createdAt: 10,
});
const relayedUser = message({
  id: 'relayed-user',
  role: 'user',
  kind: 'user',
  text: 'message sent while mobile resumed',
  createdAt: 20,
});
let visible = mergeDurableChatMessages([], [durableUser]);
visible = applySyncedUserMessage(visible, relayedUser);
visible = mergeDurableChatMessages(visible, [durableUser]);
assert.deepEqual(visible.map((entry) => entry.id), ['durable-user', 'relayed-user']);
assert.equal(
  visible.filter((entry) => entry.role === 'user' && entry.text === durableUser.text).length,
  1,
);

// An explicit claim carries the known revision. If another client won first,
// the stale candidate must adopt that canonical pointer instead of overwriting.
const newerPointer: ServerLastChat = { ...desktopPointer, sessionId: 'desktop-session-2', revision: 5 };
const staleClaim = buildLastChatClaimPayload('stale-iphone', 'stale-key', null, desktopPointer.revision);
assert.equal(staleClaim.expectedRevision, 4);
assert.equal(shouldAdoptServerPointer({ sessionId: 'stale-iphone', revision: 4 }, newerPointer), true);
assert.equal(canClaimLastChatPointer('submit'), true);

console.log('chat handoff contract tests passed');
