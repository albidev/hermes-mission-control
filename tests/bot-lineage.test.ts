import assert from 'node:assert/strict';
import test from 'node:test';
import { buildBotLineageRows } from '../src/lib/bot-lineage.ts';

test('collapses origin and target persistence rows into one lineage record', () => {
  const base = { id: 'h1', handle: 'researcher', request: 'trace', status: 'completed' as const, updatedAt: 20 };
  const rows = buildBotLineageRows([
    { sessionId: 'target-session', handoff: { ...base, targetSessionId: 'target-session' } },
    { sessionId: 'origin-session', handoff: { ...base, targetSessionId: 'target-session', updatedAt: 25, reply: 'done' } },
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].originSessionId, 'origin-session');
  assert.equal(rows[0].handoff.reply, 'done');
});
