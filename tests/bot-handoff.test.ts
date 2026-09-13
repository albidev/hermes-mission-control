import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  createHandoffEnvelope,
  createHandoffDedupe,
  submitHandoff,
  type HandoffRpc,
  type HandoffOrigin,
} from '../src/lib/bot-handoff.ts';

const ORIGIN: HandoffOrigin = { connectionId: 'local', profile: 'default', sessionId: 'origin-1' };

function makeRpc(record: { submits: Array<{ session_id: string; text: string }> }): HandoffRpc {
  return {
    resolveCanonical: async (profile) => ({ profile, registryId: `reg-${profile}`, openedId: `run-${profile}`, created: false }),
    submit: async (params) => { record.submits.push(params); return { ok: true }; },
  };
}

describe('createHandoffEnvelope', () => {
  it('builds a scoped envelope with unique handoffId', () => {
    const a = createHandoffEnvelope(ORIGIN, { profile: 'example-bot' }, 'analizza questo trace');
    const b = createHandoffEnvelope(ORIGIN, { profile: 'example-bot' }, 'analizza questo trace');
    assert.match(a.handoffId, /^mc-handoff-/);
    assert.notStrictEqual(a.handoffId, b.handoffId);
    assert.deepStrictEqual(a.origin, ORIGIN);
    assert.deepStrictEqual(a.target, { profile: 'example-bot', canonicalTitle: 'Bot Chat' });
    assert.strictEqual(a.request, 'analizza questo trace');
    assert.deepStrictEqual(a.context, { mode: 'none', messages: [] });
  });
});

describe('createHandoffDedupe', () => {
  it('allows a fresh handoffId once', () => {
    const dedupe = createHandoffDedupe();
    const id = 'mc-handoff-abc';
    assert.strictEqual(dedupe.tryClaim(id), true);
    assert.strictEqual(dedupe.tryClaim(id), false);
  });

  it('releases the claim after settle', () => {
    const dedupe = createHandoffDedupe();
    const id = 'mc-handoff-abc';
    assert.strictEqual(dedupe.tryClaim(id), true);
    dedupe.release(id);
    assert.strictEqual(dedupe.tryClaim(id), true);
  });
});

describe('submitHandoff', () => {
  it('resolves canonical target before submit and sends request text', async () => {
    const record = { submits: [] as Array<{ session_id: string; text: string }> };
    const rpc = makeRpc(record);
    const envelope = createHandoffEnvelope(ORIGIN, { profile: 'example-bot' }, 'analizza questo trace');
    const result = await submitHandoff(rpc, envelope);
    assert.strictEqual(result.openedId, 'run-example-bot');
    assert.strictEqual(record.submits.length, 1);
    assert.strictEqual(record.submits[0].session_id, 'run-example-bot');
    assert.match(record.submits[0].text, /\[MISSION CONTROL HANDOFF — NEW REQUEST\]/);
    assert.match(record.submits[0].text, /Answer the CURRENT REQUEST directly/);
    assert.match(record.submits[0].text, /CURRENT REQUEST:\nanalizza questo trace/);
  });

  it('delivers the transcript context in the submitted prompt', async () => {
    const record = { submits: [] as Array<{ session_id: string; text: string }> };
    const rpc = makeRpc(record);
    const envelope = createHandoffEnvelope(
      ORIGIN,
      { profile: 'example-bot', canonicalTitle: 'Bot Chat' },
      'follow up',
      { mode: 'transcript', messages: [{ role: 'user', text: 'earlier message' }] },
    );
    await submitHandoff(rpc, envelope);
    assert.match(record.submits[0].text, /CONVERSATION CONTEXT:\nuser: earlier message/);
    assert.match(record.submits[0].text, /CURRENT REQUEST:\nfollow up/);
  });

  it('does not submit twice for the same handoffId', async () => {
    const record = { submits: [] as Array<{ session_id: string; text: string }> };
    const rpc = makeRpc(record);
    const dedupe = createHandoffDedupe();
    const envelope = createHandoffEnvelope(ORIGIN, { profile: 'example-bot' }, 'fai x');
    const first = await submitHandoff(rpc, envelope, dedupe);
    assert.strictEqual(first.submitted, true);
    const second = await submitHandoff(rpc, envelope, dedupe);
    assert.strictEqual(second.submitted, false);
    assert.strictEqual(record.submits.length, 1);
  });

  it('releases the dedupe claim after a failed submit so retry is possible', async () => {
    const record = { submits: [] as Array<{ session_id: string; text: string }> };
    const rpc: HandoffRpc = {
      resolveCanonical: async (profile) => ({ profile, registryId: `reg-${profile}`, openedId: `run-${profile}`, created: false }),
      submit: async () => { throw new Error('gateway down'); },
    };
    const dedupe = createHandoffDedupe();
    const envelope = createHandoffEnvelope(ORIGIN, { profile: 'example-bot' }, 'fai x');
    await assert.rejects(() => submitHandoff(rpc, envelope, dedupe), /gateway down/);
    assert.strictEqual(dedupe.tryClaim(envelope.handoffId), true);
  });
});
