import assert from 'node:assert/strict';
import test from 'node:test';
import {
  canonicalChatCommand,
  canonicalChatIdentity,
  shouldPreserveCanonicalSession,
} from '../src/lib/bot-chat-policy.ts';

test('canonical identity is profile plus Bot Chat title', () => {
  assert.deepEqual(canonicalChatIdentity(' CrossConnection '), {
    profile: 'CrossConnection',
    title: 'Bot Chat',
  });
});

test('canonical context turns new/reset into compact', () => {
  assert.equal(canonicalChatCommand('canonical', 'new'), '/compact');
  assert.equal(canonicalChatCommand('canonical', 'reset'), '/compact');
  assert.equal(canonicalChatCommand('task', 'new'), '/new');
  assert.equal(canonicalChatCommand('general', 'reset'), '/reset');
});

test('canonical context preserves the durable session', () => {
  assert.equal(shouldPreserveCanonicalSession('canonical'), true);
  assert.equal(shouldPreserveCanonicalSession('task'), false);
  assert.equal(shouldPreserveCanonicalSession('general'), false);
});
