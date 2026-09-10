import assert from 'node:assert/strict';
import test from 'node:test';
import { addChatProfile } from '../src/lib/chat-session-params.ts';

test('adds the Bot profile to gateway session parameters', () => {
  assert.deepEqual(
    addChatProfile({ session_id: 'runtime-1', eager_build: true }, ' crossnection '),
    { session_id: 'runtime-1', eager_build: true, profile: 'crossnection' },
  );
});

test('does not add an empty profile to generic chat parameters', () => {
  const params = { cols: 80, source: 'mission-control' };
  assert.deepEqual(addChatProfile(params, '  '), params);
  assert.deepEqual(addChatProfile(params, null), params);
});

console.log('chat session profile tests passed');
