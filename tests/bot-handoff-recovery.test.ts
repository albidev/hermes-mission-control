import { strict as assert } from 'node:assert';
import { findHandoffCompletion } from '../src/lib/bot-handoff-recovery.ts';

const handoffId = 'mc-handoff-123';

assert.equal(findHandoffCompletion([
  { type: 'message.started', seq: 1, payload: { text: `[MISSION CONTROL HANDOFF] handoff_id: ${handoffId}` } },
  { type: 'tool.completed', seq: 2, payload: { text: 'tool output' } },
  { type: 'message.complete', seq: 3, payload: { content: 'Bot completed while the chat was closed.' } },
], handoffId), 'Bot completed while the chat was closed.');

assert.equal(findHandoffCompletion([
  { type: 'message.started', seq: 1, payload: { text: 'another handoff' } },
  { type: 'message.complete', seq: 2, payload: { content: 'Unrelated answer.' } },
], handoffId), null);

assert.equal(findHandoffCompletion([
  { type: 'message.started', seq: 1, payload: { text: `[MISSION CONTROL HANDOFF] handoff_id: ${handoffId}` } },
  { type: 'tool.completed', seq: 2, payload: { text: 'still working' } },
], handoffId), null);

console.log('bot handoff recovery tests passed');
