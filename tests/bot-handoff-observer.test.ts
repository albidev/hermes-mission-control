import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import { createHandoffObserver, type HandoffEventRpc } from '../src/lib/bot-handoff-observer.ts';

function makeRpc(sequence: Array<{ events?: Array<{ type: string; payload?: Record<string, unknown> }>; truncated?: boolean }>): HandoffEventRpc {
  let index = 0;
  return {
    eventsSince: async (params) => {
      const item = sequence[Math.min(index, sequence.length - 1)];
      index += 1;
      return { events: item.events ?? [], truncated: item.truncated ?? false, epoch: 'e1' };
    },
  };
}

describe('createHandoffObserver', () => {
  it('polls events.since with increasing last_seen and reports completion', async () => {
    const rpc = makeRpc([
      { events: [{ type: 'run.started', payload: {} }] },
      { events: [{ type: 'message.complete', payload: { text: 'risposta finale' } }] },
      { events: [] },
    ]);
    const seen: string[] = [];
    const observer = createHandoffObserver(rpc, {
      sessionId: 'run-crossnection',
      intervalMs: 1,
      onEvent: (event) => { seen.push(event.type); },
      onComplete: (text) => { seen.push(`complete:${text}`); },
      onError: (message) => { seen.push(`error:${message}`); },
    });
    await observer.start();
    await new Promise((resolve) => setTimeout(resolve, 20));
    await observer.stop();
    assert.ok(seen.includes('run.started'));
    assert.ok(seen.includes('message.complete'));
    assert.ok(seen.some((entry) => entry.startsWith('complete:')));
  });

  it('reports error events and stops', async () => {
    const rpc = makeRpc([
      { events: [{ type: 'error', payload: { message: 'gateway down' } }] },
    ]);
    const seen: string[] = [];
    const observer = createHandoffObserver(rpc, {
      sessionId: 'run-crossnection',
      intervalMs: 1,
      onEvent: () => {},
      onComplete: () => { seen.push('complete'); },
      onError: (message) => { seen.push(`error:${message}`); },
    });
    await observer.start();
    await new Promise((resolve) => setTimeout(resolve, 20));
    await observer.stop();
    assert.deepStrictEqual(seen, ['error:gateway down']);
  });

  it('does not double-report after stop', async () => {
    let calls = 0;
    const rpc: HandoffEventRpc = {
      eventsSince: async () => { calls += 1; return { events: [], truncated: false, epoch: 'e1' }; },
    };
    const observer = createHandoffObserver(rpc, {
      sessionId: 'run-crossnection',
      intervalMs: 1,
      onEvent: () => {},
      onComplete: () => {},
      onError: () => {},
    });
    await observer.start();
    await new Promise((resolve) => setTimeout(resolve, 10));
    await observer.stop();
    const callsAfterStop = calls;
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.strictEqual(calls, callsAfterStop);
  });
});
