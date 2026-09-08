import { strict as assert } from 'node:assert';
import type { ChatMessage, GatewayEvent } from '../src/lib/chat-protocol.ts';
import { applySyncedChatMessage, applySyncedUserMessage, mergeDurableChatMessages, shouldApplySequencedEvent } from '../src/lib/chat-sync.ts';

const message = (partial: Partial<ChatMessage> & Pick<ChatMessage, 'id' | 'role' | 'text'>): ChatMessage => ({
  createdAt: 1,
  status: 'complete',
  ...partial,
});

const localStream = message({ id: 'assistant-live', role: 'assistant', kind: 'assistant', text: 'Sto elaborando', status: 'streaming' });
const remoteUser = message({ id: 'remote-user', role: 'user', kind: 'user', text: 'Messaggio inviato da iPhone' });
const remoteTool = message({ id: 'remote-tool', role: 'tool', kind: 'tool', toolName: 'terminal', toolId: 'tool-1', text: '', toolInput: 'pwd', output: 'done' });

const merged = mergeDurableChatMessages([localStream], [remoteUser, remoteTool]);
assert.equal(merged.some((entry) => entry.text === remoteUser.text), true);
assert.equal(merged.some((entry) => entry.toolId === remoteTool.toolId), true);
assert.equal(merged.at(-1)?.status, 'streaming');
assert.equal(merged.at(-1)?.text, localStream.text);

const duplicate = mergeDurableChatMessages([remoteUser], [remoteUser]);
assert.equal(duplicate.length, 1);

const durableAssistant = message({
  id: 'assistant-durable',
  role: 'assistant',
  kind: 'assistant',
  text: 'Persisted answer',
  createdAt: Date.parse('2026-09-07T17:16:00.000Z'),
});
const provisionalAssistant = { ...durableAssistant, id: 'assistant-live', status: 'streaming' as const, createdAt: Date.parse('2026-09-08T10:30:00.000Z') };
const reconciled = mergeDurableChatMessages([provisionalAssistant], [durableAssistant]);
assert.equal(reconciled.length, 2);
assert.equal(reconciled.some((entry) => entry.id === durableAssistant.id), true);
assert.equal(reconciled.some((entry) => entry.id === provisionalAssistant.id), true);

// A resume snapshot can race a relay event and be partial. Hydration must be
// monotonic: every already-visible message survives, while a user row already
// present in both sources is still rendered exactly once.
const durableUser = message({ id: 'durable-user', role: 'user', kind: 'user', text: 'first question', createdAt: 10 });
const relayAssistant = message({ id: 'relay-assistant', role: 'assistant', kind: 'assistant', text: 'relay answer', createdAt: 20 });
const optimisticUser = message({ id: 'optimistic-user', role: 'user', kind: 'user', text: 'second question', createdAt: 30 });
const streamingAssistant = message({ id: 'streaming-assistant', role: 'assistant', kind: 'assistant', text: 'working', status: 'streaming', createdAt: 40 });
const visibleTool = message({ id: 'visible-tool', role: 'tool', kind: 'tool', text: '', toolId: 'visible-tool-id', toolName: 'terminal', output: 'still running', createdAt: 50 });
let racedTranscript = mergeDurableChatMessages([], [durableUser]);
racedTranscript = applySyncedChatMessage(racedTranscript, relayAssistant);
racedTranscript = applySyncedUserMessage(racedTranscript, optimisticUser);
racedTranscript = applySyncedChatMessage(racedTranscript, streamingAssistant);
racedTranscript = applySyncedChatMessage(racedTranscript, visibleTool);
// The in-flight resume now returns a partial snapshot that omits the messages
// delivered during the race. Hydration must return their chronological union.
const hydratedUnion = mergeDurableChatMessages(racedTranscript, [durableUser]);
assert.deepEqual(hydratedUnion.map((entry) => entry.id), [
  'durable-user',
  'relay-assistant',
  'optimistic-user',
  'streaming-assistant',
  'visible-tool',
]);
assert.equal(hydratedUnion.filter((entry) => entry.role === 'user' && entry.text === durableUser.text).length, 1);

// Local relay/optimistic rows can belong between two rows in a later snapshot;
// hydration must keep the visible transcript chronological, not snapshot-first.
const chronologicalUnion = mergeDurableChatMessages(
  [message({ id: 'relay-middle', role: 'assistant', kind: 'assistant', text: 'middle', createdAt: 20 })],
  [
    message({ id: 'durable-first', role: 'user', kind: 'user', text: 'first', createdAt: 10 }),
    message({ id: 'durable-last', role: 'assistant', kind: 'assistant', text: 'last', createdAt: 30 }),
  ],
);
assert.deepEqual(chronologicalUnion.map((entry) => entry.id), ['durable-first', 'relay-middle', 'durable-last']);

// Reconciliation repeats while a stream is still omitted from the snapshot;
// the same local streaming row must not be appended again on every tick.
const streamingOnly = message({ id: 'streaming-only', role: 'assistant', kind: 'assistant', text: 'still working', status: 'streaming', createdAt: 40 });
const repeatedHydration = mergeDurableChatMessages(
  mergeDurableChatMessages([streamingOnly], [durableUser]),
  [durableUser],
);
assert.equal(repeatedHydration.filter((entry) => entry.id === streamingOnly.id).length, 1);

// Identical text is not an identity key. Without a stable DB id, the
// canonical rows and local rows remain distinct until canonical replacement.
const repeatedPrompt = message({ id: 'local-repeat', role: 'user', kind: 'user', text: 'same prompt', createdAt: 60 });
const repeatedPromptAgain = message({ id: 'local-repeat-again', role: 'user', kind: 'user', text: 'same prompt', createdAt: 70 });
const repeatedPromptUnion = mergeDurableChatMessages(
  [repeatedPrompt, repeatedPromptAgain],
  [
    message({ id: 'restored-repeat', role: 'user', kind: 'user', text: 'same prompt', createdAt: 60 }),
    message({ id: 'restored-repeat-again', role: 'user', kind: 'user', text: 'same prompt', createdAt: 70 }),
  ],
);
assert.equal(repeatedPromptUnion.filter((entry) => entry.role === 'user' && entry.text === 'same prompt').length, 4);
assert.equal(new Set(repeatedPromptUnion.map((entry) => entry.id)).size, 4);

const syncedUser = message({ id: 'user-shared-1', role: 'user', kind: 'user', text: 'shared across devices' });
assert.equal(applySyncedUserMessage([], syncedUser).length, 1);
assert.equal(applySyncedUserMessage([syncedUser], syncedUser).length, 1);

const watermarks = new Map<string, number>();
const event = (seq: number): GatewayEvent => ({ type: 'message.delta', session_id: 'session-1', seq });
assert.equal(shouldApplySequencedEvent(watermarks, event(1)), true);
watermarks.set('session-1', 1);
assert.equal(shouldApplySequencedEvent(watermarks, event(1)), false);
assert.equal(shouldApplySequencedEvent(watermarks, event(2)), true);

console.log('chat sync tests passed');
