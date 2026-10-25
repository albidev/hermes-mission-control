import assert from 'node:assert/strict';
import test from 'node:test';
import { createRoomPairingApi, RoomPairingApiError } from '../src/lib/room-pairing-api.ts';
import { createRoomJoiningApi, RoomJoiningApiError } from '../src/lib/room-joining-api.ts';
import { createRoomGuestApi, RoomGuestApiError } from '../src/lib/room-guest-api.ts';

function recorder(body: unknown = {}) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fetcher: typeof fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return Response.json(body);
  };
  return { calls, fetcher };
}
const base = '/api/local/room-invites';

function expectCall(call: { url: string; init: RequestInit }, path: string, method: string, body?: unknown) {
  assert.equal(call.url, base + path);
  assert.equal(call.init.method, method);
  assert.equal(call.init.body, body === undefined ? undefined : JSON.stringify(body));
  assert.equal(call.init.cache, 'no-store');
  assert.equal(call.init.credentials, 'same-origin');
  assert.equal((call.init.headers as Record<string, string>).Authorization, 'Bearer owner');
}

test('pairing client uses all nine exact routes, response envelopes, bodies and signals', async () => {
  const offer = { id: 'id', code: 'offer', expires_at: 1792869000 };
  const accept = { ...offer, code: 'accept', identity: { peer_id: 'p', installation_id: 'i', profile: 'b' } };
  const reveal = { ...offer, code: 'reveal', sas: '123456' };
  const pairing = { id: 'id', role: 'host', state: 'confirmed', expires_at: 1792869000 };
  const { calls, fetcher } = recorder({ offer, accept, reveal, pairing, pairings: [pairing], sas: '123456' });
  const api = createRoomPairingApi(' owner ', fetcher);
  const signal = new AbortController().signal;
  assert.deepEqual(await api.createOffer(signal), offer);
  assert.deepEqual(await api.acceptOffer('a/b', 'accept', signal), reveal);
  assert.deepEqual(await api.confirmOffer('a/b', '123456', signal), pairing);
  await api.cancelOffer('a/b', signal);
  assert.deepEqual(await api.importOffer('offer', signal), accept);
  assert.equal((await api.revealImport('a/b', 'reveal', signal)).sas, '123456');
  assert.deepEqual(await api.confirmImport('a/b', '123456', signal), pairing);
  assert.deepEqual(await api.listPairings(signal), [pairing]);
  await api.removePeer('p/q', signal);
  const expected: Array<[string, string, unknown?]> = [
    ['/pairing/offers', 'POST', {}],
    ['/pairing/offers/a%2Fb/accept', 'POST', { code: 'accept' }],
    ['/pairing/offers/a%2Fb/confirm', 'POST', { sas: '123456' }],
    ['/pairing/offers/a%2Fb/cancel', 'POST', {}],
    ['/pairing/imports', 'POST', { code: 'offer' }],
    ['/pairing/imports/a%2Fb/reveal', 'POST', { code: 'reveal' }],
    ['/pairing/imports/a%2Fb/confirm', 'POST', { sas: '123456' }],
    ['/pairing', 'GET'], ['/peers/p%2Fq', 'DELETE'],
  ];
  assert.equal(calls.length, expected.length);
  expected.forEach(([path, method, body], index) => {
    expectCall(calls[index], path, method, body);
    assert.equal(calls[index].init.signal, signal);
  });
});

test('joining API exercises every route and never implicitly consents during redeem/scope', async () => {
  const record = { id: 'import', status: 'preview' };
  const { calls, fetcher } = recorder({ import: record });
  const api = createRoomJoiningApi('owner', fetcher);
  assert.deepEqual(await api.preview({ code: 'secret', host_id: 'paired-host' }), record);
  await api.metadata('a/b');
  await api.redeem('a/b');
  await api.getImport('a/b');
  await api.confirmScope('a/b', { room_id: 'room', member_id: 'member' });
  await api.consent('a/b', false);
  await api.resolve('a/b');
  const expected: Array<[string, string, unknown?]> = [
    ['/imports/preview', 'POST', { code: 'secret', host_id: 'paired-host' }],
    ['/imports/a%2Fb/metadata', 'POST', {}], ['/imports/a%2Fb/redeem', 'POST', {}],
    ['/imports/a%2Fb', 'GET'],
    ['/imports/a%2Fb/scope', 'POST', { room_id: 'room', member_id: 'member' }],
    ['/imports/a%2Fb/consent', 'POST', { approve: false }], ['/imports/a%2Fb/resolve', 'POST', {}],
  ];
  assert.equal(calls.length, 7);
  expected.forEach(([path, method, body], index) => expectCall(calls[index], path, method, body));
});

test('guest API preserves since_seq=0, pagination, approval choices and escaped identifiers', async () => {
  const { calls, fetcher } = recorder({ rooms: [] });
  const api = createRoomGuestApi('owner', fetcher);
  assert.deepEqual(await api.listRooms(), []);
  await api.getLog('r/s', { sinceSeq: 0, limit: 20 });
  await api.getLog('r/s', { sinceSeq: 42, limit: 100 });
  await api.getMyBot('r/s');
  await api.decideApproval('r/s', 'q/r', 'reject');
  await api.leaveRoom('r/s');
  const expected: Array<[string, string, unknown?]> = [
    ['/guest-rooms', 'GET'], ['/guest-rooms/r%2Fs/log?since_seq=0&limit=20', 'GET'],
    ['/guest-rooms/r%2Fs/log?since_seq=42&limit=100', 'GET'], ['/guest-rooms/r%2Fs/my-bot', 'GET'],
    ['/guest-rooms/r%2Fs/approvals/q%2Fr', 'POST', { choice: 'reject' }], ['/guest-rooms/r%2Fs/leave', 'POST', {}],
  ];
  expected.forEach(([path, method, body], index) => expectCall(calls[index], path, method, body));
});

for (const [status, kind] of [[401, 'unauthorized'], [403, 'forbidden'], [404, 'unavailable'], [409, 'conflict'], [503, 'error']] as const) {
  test(`all API clients map HTTP ${status} to ${kind} without exposing response details`, async () => {
    const fetcher: typeof fetch = async () => Response.json({ code: 'generic_error', secret: 'never-display' }, { status });
    const requests = [
      [() => createRoomPairingApi(undefined, fetcher).createOffer(), RoomPairingApiError],
      [() => createRoomJoiningApi(undefined, fetcher).preview({ code: 'c' }), RoomJoiningApiError],
      [() => createRoomGuestApi(undefined, fetcher).listRooms(), RoomGuestApiError],
    ] as const;
    for (const [request, ErrorType] of requests) {
      await assert.rejects(request, (error: unknown) => {
        assert.ok(error instanceof ErrorType);
        assert.deepEqual(error.problem, { kind, code: 'generic_error' });
        assert.doesNotMatch(error.message, /never-display/);
        return true;
      });
    }
  });
}

test('empty HTTP errors and malformed success fail safely', async () => {
  const bad: typeof fetch = async () => new Response('not JSON', { status: 503 });
  await assert.rejects(() => createRoomJoiningApi(undefined, bad).getImport('x'), { message: 'http_503' });
  const malformed = recorder({});
  await assert.rejects(() => createRoomPairingApi(undefined, malformed.fetcher).listPairings(), { message: 'invalid_response' });
});

test('clients do not access localStorage even while carrying one-time codes', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  let accesses = 0;
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true, get() { accesses++; throw new Error('storage forbidden'); },
  });
  try {
    const { fetcher } = recorder({ offer: {}, import: {}, rooms: [] });
    await createRoomPairingApi(undefined, fetcher).createOffer();
    await createRoomJoiningApi(undefined, fetcher).preview({ code: 'secret' });
    await createRoomGuestApi(undefined, fetcher).listRooms();
    assert.equal(accesses, 0);
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'localStorage', descriptor);
    else delete (globalThis as { localStorage?: unknown }).localStorage;
  }
});
