import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import { summarizeHandoffTrace } from '../src/lib/handoff-trace.ts';

const event = (over: Record<string, unknown>) => ({
  id: '', type: '', label: '', detail: '', tone: 'good' as const,
  timestamp: 0, sessionId: 'sid', turnId: 1, ...over,
});

describe('summarizeHandoffTrace', () => {
  it('returns an empty summary for no events', () => {
    assert.deepEqual(summarizeHandoffTrace(null), { rows: [], toolCount: 0, reasoningCount: 0, headline: '' });
    assert.deepEqual(summarizeHandoffTrace([]), { rows: [], toolCount: 0, reasoningCount: 0, headline: '' });
  });

  it('pairs a completed call with its started call and computes the duration', () => {
    const summary = summarizeHandoffTrace([
      event({ id: 'a', type: 'tool_call_started', toolName: 'terminal', request: '{"command":"ls"}', status: 'running', timestamp: 100 }),
      event({ id: 'b', type: 'tool_call_completed', toolName: 'terminal', response: '{"out":"ok"}', status: 'complete', timestamp: 102.5 }),
    ]);
    assert.equal(summary.toolCount, 1);
    assert.equal(summary.rows.length, 1);
    const row = summary.rows[0];
    assert.equal(row.kind, 'tool');
    if (row.kind !== 'tool') return;
    assert.equal(row.toolName, 'terminal');
    assert.equal(row.input, '{"command":"ls"}');
    assert.equal(row.output, '{"out":"ok"}');
    assert.equal(row.status, 'complete');
    assert.equal(row.durationSeconds, 2.5);
  });

  it('keeps an unpaired completed call rather than dropping it', () => {
    const summary = summarizeHandoffTrace([
      event({ id: 'b', type: 'tool_call_completed', toolName: 'bdh_query', response: '{"n":3}', timestamp: 5 }),
    ]);
    assert.equal(summary.toolCount, 1);
    const row = summary.rows[0];
    if (row.kind !== 'tool') throw new Error('expected tool row');
    assert.equal(row.output, '{"n":3}');
    assert.equal(row.durationSeconds, null);
  });

  it('does not let one tool name close another tool call', () => {
    const summary = summarizeHandoffTrace([
      event({ id: 'a', type: 'tool_call_started', toolName: 'skill_view', timestamp: 1 }),
      event({ id: 'b', type: 'tool_call_completed', toolName: 'terminal', response: 'wrong', timestamp: 2 }),
      event({ id: 'c', type: 'tool_call_completed', toolName: 'skill_view', response: 'right', timestamp: 3 }),
    ]);
    const tools = summary.rows.filter((r) => r.kind === 'tool');
    assert.equal(tools.length, 2);
    const skill = tools.find((r) => r.kind === 'tool' && r.toolName === 'skill_view');
    if (skill?.kind !== 'tool') throw new Error('expected skill_view row');
    assert.equal(skill.output, 'right', 'the mismatched name must not steal the pairing');
  });

  it('counts reasoning rows and keeps them in order with the tools', () => {
    const summary = summarizeHandoffTrace([
      event({ id: 't1', type: 'thought', detail: 'first thought', timestamp: 1 }),
      event({ id: 'a', type: 'tool_call_started', toolName: 'terminal', timestamp: 2 }),
      event({ id: 't2', type: 'thought', detail: 'second thought', timestamp: 3 }),
    ]);
    assert.equal(summary.reasoningCount, 2);
    assert.equal(summary.toolCount, 1);
    assert.deepEqual(summary.rows.map((r) => r.kind), ['reasoning', 'tool', 'reasoning']);
  });

  it('derives a single-line headline from the assistant response', () => {
    const summary = summarizeHandoffTrace([
      event({ id: 'u', type: 'user_message', detail: 'the request', timestamp: 1 }),
      event({ id: 'r', type: 'assistant_response', detail: '\n\n  Service is healthy.  \nMore detail.', timestamp: 2 }),
    ]);
    assert.equal(summary.headline, 'Service is healthy.');
  });

  it('ignores a tool call with no resolvable name', () => {
    const summary = summarizeHandoffTrace([
      event({ id: 'a', type: 'tool_call_started', timestamp: 1 }),
    ]);
    assert.equal(summary.toolCount, 0);
    assert.equal(summary.rows.length, 0);
  });

  it('pairs a completion whose started event carries the generic name "tool_call"', () => {
    // Some gateway events use a generic started name (`tool_call_started` says only
    // "tool_call") while completion names the actual tool. Pairing by name left every
    // one of these as two half-rows (input with no output, and an orphaned output).
    const summary = summarizeHandoffTrace([
      event({ id: 'evt_4', type: 'tool_call_started', toolName: 'tool_call', request: '{"query":"sentry"}', timestamp: 10 }),
      event({ id: 'evt_5', type: 'tool_call_completed', toolName: 'mcp__sentry__search', response: '{"hits":3}', parentEventId: 'evt_4', timestamp: 12 }),
    ]);

    assert.equal(summary.rows.length, 1, 'one call must produce one row, not two');
    const row = summary.rows[0] ;
    assert.equal(row.kind, 'tool');
    assert.equal(row.input, '{"query":"sentry"}');
    assert.equal(row.output, '{"hits":3}', 'the output must land on the row that holds the input');
    assert.equal(row.toolName, 'mcp__sentry__search', 'the specific name from the completion wins');
    assert.equal(row.durationSeconds, 2);
  });

  it('keeps parallel calls of the same name apart by their parent link', () => {
    const summary = summarizeHandoffTrace([
      event({ id: 'a', type: 'tool_call_started', toolName: 'tool_call', request: 'first', timestamp: 1 }),
      event({ id: 'b', type: 'tool_call_started', toolName: 'tool_call', request: 'second', timestamp: 1 }),
      event({ id: 'c', type: 'tool_call_completed', toolName: 'mcp__x', response: 'R-second', parentEventId: 'b', timestamp: 2 }),
      event({ id: 'd', type: 'tool_call_completed', toolName: 'mcp__x', response: 'R-first', parentEventId: 'a', timestamp: 3 }),
    ]);

    const rows = summary.rows as Array<{ input: string; output: string }>;
    assert.equal(rows.length, 2, 'two calls, two rows');
    assert.equal(rows[0].input, 'first');
    assert.equal(rows[0].output, 'R-first', 'the parent link must beat arrival order');
    assert.equal(rows[1].input, 'second');
    assert.equal(rows[1].output, 'R-second');
  });

  it('falls back to name matching when a completion has no parent link', () => {
    const summary = summarizeHandoffTrace([
      event({ id: 'x', type: 'tool_call_started', toolName: 'terminal', request: 'ls', timestamp: 1 }),
      event({ id: 'y', type: 'tool_call_completed', toolName: 'terminal', response: 'ok', timestamp: 2 }),
    ]);

    const rows = summary.rows as Array<{ input: string; output: string }>;
    assert.equal(rows.length, 1);
    assert.equal(rows[0].output, 'ok', 'a missing parent link must still pair by name');
  });
});
