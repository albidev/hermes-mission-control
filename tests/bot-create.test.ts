import assert from 'node:assert/strict';
import test from 'node:test';
import { buildBotCreateInput } from '../src/lib/bot-create.ts';
import { deleteBotProfile } from '../src/lib/bot-delete.ts';

test('selected starting profile is preserved in the create request input', () => {
  const input = buildBotCreateInput({
    name: 'researcher',
    description: 'Finds things',
    soul: 'You research.',
    model: '',
    provider: '',
    noSkills: false,
    botRoster: true,
    cloneFrom: 'crossnection',
  });

  assert.equal(input.cloneFrom, 'crossnection');
  assert.equal(input.name, 'researcher');
});

test('fresh profile creation omits the starting profile', () => {
  const input = buildBotCreateInput({
    name: 'researcher',
    description: '',
    soul: 'You research.',
    model: '',
    provider: '',
    noSkills: true,
    botRoster: true,
    cloneFrom: null,
  });

  assert.equal(input.cloneFrom, undefined);
});

test('delete profile sends the authenticated dashboard REST request', async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ input: string; init?: RequestInit }> = [];
  globalThis.fetch = (async (input, init) => {
    calls.push({ input: String(input), init });
    return new Response(JSON.stringify({ ok: true, path: '/profiles/researcher' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as typeof fetch;
  try {
    await deleteBotProfile(' researcher ', ' mc-token ');
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(calls.length, 1);
  assert.equal(calls[0]?.input, '/api/profiles/researcher');
  assert.equal(calls[0]?.init?.method, 'DELETE');
  assert.equal(calls[0]?.init?.credentials, 'include');
  assert.deepEqual(calls[0]?.init?.headers, {
    Accept: 'application/json',
    Authorization: 'Bearer mc-token',
  });
});

test('default profile is rejected before issuing a delete request', async () => {
  await assert.rejects(() => deleteBotProfile('default'), /cannot be deleted/);
});

console.log('bot create tests passed');
