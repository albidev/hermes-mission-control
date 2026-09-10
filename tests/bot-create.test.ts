import assert from 'node:assert/strict';
import test from 'node:test';
import { buildBotCreateInput } from '../src/lib/bot-create.ts';

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

console.log('bot create tests passed');
