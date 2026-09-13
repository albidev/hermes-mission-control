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
      event({ id: 'r', type: 'assistant_response', detail: '\n\n  Inspector is healthy.  \nMore detail.', timestamp: 2 }),
    ]);
    assert.equal(summary.headline, 'Inspector is healthy.');
  });

  it('ignores a tool call with no resolvable name', () => {
    const summary = summarizeHandoffTrace([
      event({ id: 'a', type: 'tool_call_started', timestamp: 1 }),
    ]);
    assert.equal(summary.toolCount, 0);
    assert.equal(summary.rows.length, 0);
  });
});
