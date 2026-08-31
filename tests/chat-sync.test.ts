import { strict as assert } from 'node:assert';
import type { ChatMessage, GatewayEvent } from '../src/lib/chat-protocol.ts';
import { mergeDurableChatMessages, shouldApplySequencedEvent } from '../src/lib/chat-sync.ts';

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

const watermarks = new Map<string, number>();
const event = (seq: number): GatewayEvent => ({ type: 'message.delta', session_id: 'session-1', seq });
assert.equal(shouldApplySequencedEvent(watermarks, event(1)), true);
watermarks.set('session-1', 1);
assert.equal(shouldApplySequencedEvent(watermarks, event(1)), false);
assert.equal(shouldApplySequencedEvent(watermarks, event(2)), true);

console.log('chat sync tests passed');
