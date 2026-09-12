import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import { createBotChatResolver } from '../src/lib/bot-chat-routing.ts';

class SeqMock {
  calls = [];
  sequence = [];
  idx = 0;

  addList(result) { this.sequence.push({ method: 'list', result }); }
  addCreate(result) { this.sequence.push({ method: 'create', result }); }
  addTitle(result) { this.sequence.push({ method: 'title', result }); }
  getCalls() { return this.calls; }

  async list(params) {
    this.calls.push({ method: 'list', params });
    const step = this.sequence[this.idx++];
    if (!step || step.method !== 'list') throw new Error('unexpected list');
    if (step.result instanceof Error) throw step.result;
    return step.result;
  }
  async create(params) {
    this.calls.push({ method: 'create', params });
    const step = this.sequence[this.idx++];
    if (!step || step.method !== 'create') throw new Error('unexpected create');
    if (step.result instanceof Error) throw step.result;
    return step.result;
  }
  async title(params) {
    this.calls.push({ method: 'title', params });
    const step = this.sequence[this.idx++];
    if (!step || step.method !== 'title') throw new Error('unexpected title');
    if (step.result instanceof Error) throw step.result;
    return step.result;
  }
}

function paramsCheck(calls, expect, method) {
  const m = calls.filter(c => c.method === method);
  assert.strictEqual(m.length, expect.length, method + ' count');
  for (let i = 0; i < expect.length; i++) {
    for (const [k, v] of Object.entries(expect[i])) assert.strictEqual(m[i].params[k], v, method + '.' + k);
  }
}

describe('bot-chat-routing resolver', () => {
  it('existing exact row', async () => {
    const rpc = new SeqMock();
    rpc.addList({ sessions: [{ id: 'sess-123', resolved_id: 'sess-456', title: 'Bot Chat' }] });
    const r = await createBotChatResolver(rpc).resolve('p');
    assert.deepStrictEqual(r, { profile: 'p', registryId: 'sess-123', openedId: 'sess-456', created: false });
    paramsCheck(rpc.getCalls(), [{ profile: 'p', title: 'Bot Chat', include_hidden: true, limit: 200 }], 'list');
  });

  it('list failure throws', async () => {
    const rpc = new SeqMock();
    rpc.addList(new Error('net'));
    await assert.rejects(async () => createBotChatResolver(rpc).resolve('p'), /session\.list failed/);
  });

  it('empty + knownCanonicalId throws fail-closed', async () => {
    const rpc = new SeqMock();
    rpc.addList({ sessions: [] });
    await assert.rejects(async () => createBotChatResolver(rpc).resolve('p', 'k'), /fail-closed/);
  });

  it('confirmed absence creates/adopts', async () => {
    const rpc = new SeqMock();
    rpc.addList({ sessions: [] });
    rpc.addCreate({ session_id: 'r1', stored_session_id: 's1' });
    rpc.addTitle({ title: 'Bot Chat' });
    rpc.addList({ sessions: [{ id: 's1', title: 'Bot Chat' }] });
    const r = await createBotChatResolver(rpc).resolve('p');
    assert.deepStrictEqual(r, { profile: 'p', registryId: 's1', openedId: 's1', created: true });
    paramsCheck(rpc.getCalls(), [{ profile: 'p', title: 'Bot Chat', hidden: true, follow_profile_config: true }], 'create');
    paramsCheck(rpc.getCalls(), [{ session_id: 'r1', title: 'Bot Chat', profile: 'p' }], 'title');
  });

  it('missing registry after title throws', async () => {
    const rpc = new SeqMock();
    rpc.addList({ sessions: [] });
    rpc.addCreate({ session_id: 'r2', stored_session_id: 's2' });
    rpc.addTitle({ title: 'Bot Chat' });
    rpc.addList({ sessions: [] });
    await assert.rejects(async () => createBotChatResolver(rpc).resolve('p'), /missing post-title registry row/);
  });

  it('already in use adopts', async () => {
    const rpc = new SeqMock();
    rpc.addList({ sessions: [] });
    rpc.addCreate({ session_id: 'r3', stored_session_id: 's3' });
    rpc.addTitle(new Error('already in use'));
    rpc.addList({ sessions: [{ id: 'w1', title: 'Bot Chat' }] });
    const r = await createBotChatResolver(rpc).resolve('p');
    assert.deepStrictEqual(r, { profile: 'p', registryId: 'w1', openedId: 'w1', created: false });
  });

  it('concurrent dedup clears flight', async () => {
    const rpc = new SeqMock();
    rpc.addList({ sessions: [{ id: 'd1', title: 'Bot Chat' }] });
    const res = createBotChatResolver(rpc);
    const [r1, r2] = await Promise.all([res.resolve('same'), res.resolve('same')]);
    assert.strictEqual(r1, r2);
    assert.strictEqual(rpc.getCalls().filter(c => c.method === 'list').length, 1);
  });

  it('root_title canonical with different tip title selects resolved_id', async () => {
    const rpc = new SeqMock();
    rpc.addList({ sessions: [{ id: 'sess-c', root_title: 'Bot Chat', title: 'Something Else', resolved_id: 'res-c' }] });
    const r = await createBotChatResolver(rpc).resolve('p');
    assert.deepStrictEqual(r, { profile: 'p', registryId: 'sess-c', openedId: 'res-c', created: false });
  });

  it('malformed list response / no sessions confirmed absence', async () => {
    const rpc = new SeqMock();
    rpc.addList({});
    rpc.addCreate({ session_id: 'cr', stored_session_id: 'st' });
    rpc.addTitle({ title: 'Bot Chat' });
    rpc.addList({ sessions: [{ id: 'st', title: 'Bot Chat' }] });
    const r = await createBotChatResolver(rpc).resolve('p');
    assert.strictEqual(r.created, true);
  });

  it('non-already-in-use title error throws', async () => {
    const rpc = new SeqMock();
    rpc.addList({ sessions: [] });
    rpc.addCreate({ session_id: 'r', stored_session_id: 's' });
    rpc.addTitle(new Error('other error'));
    await assert.rejects(async () => createBotChatResolver(rpc).resolve('p'), /session\.title failed: other error/);
  });

  it('create payload verifies follow_profile_config true', async () => {
    const rpc = new SeqMock();
    rpc.addList({ sessions: [] });
    rpc.addCreate({ session_id: 'r1', stored_session_id: 's1' });
    rpc.addTitle({ title: 'Bot Chat' });
    rpc.addList({ sessions: [{ id: 's1', title: 'Bot Chat' }] });
    await createBotChatResolver(rpc).resolve('p');
    paramsCheck(rpc.getCalls(), [{ profile: 'p', title: 'Bot Chat', hidden: true, follow_profile_config: true }], 'create');
  });

  it('concurrent flight clears after settle - second resolve performs fresh list', async () => {
    const rpc = new SeqMock();
    rpc.addList({ sessions: [] });
    rpc.addCreate({ session_id: 'r-new', stored_session_id: 'new-id' });
    rpc.addTitle({ title: 'Bot Chat' });
    rpc.addList({ sessions: [{ id: 'new-id', title: 'Bot Chat' }] });
    const r1 = await createBotChatResolver(rpc).resolve('p');
    assert.strictEqual(r1.created, true);
    rpc.addList({ sessions: [{ id: 'new-id', title: 'Bot Chat' }] });
    const r2 = await createBotChatResolver(rpc).resolve('p');
    assert.strictEqual(r2.created, false);
    assert.strictEqual(r2.registryId, 'new-id');
    assert.strictEqual((rpc.getCalls() as any[]).filter((c: any) => c.method === 'list').length, 3);
  });

  it('exact list params', async () => {
    const rpc = new SeqMock();
    rpc.addList({ sessions: [{ id: 'x', title: 'Bot Chat' }] });
    await createBotChatResolver(rpc).resolve('prof');
    const lc = rpc.getCalls().find(c => c.method === 'list');
    assert.strictEqual(lc.params.profile, 'prof');
    assert.strictEqual(lc.params.title, 'Bot Chat');
    assert.strictEqual(lc.params.include_hidden, true);
    assert.strictEqual(lc.params.limit, 200);
  });
});
