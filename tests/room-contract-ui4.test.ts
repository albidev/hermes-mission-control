import assert from 'node:assert/strict';
import test from 'node:test';
import { createRoomPairingApi, RoomPairingApiError } from '../src/lib/room-pairing-api.ts';
import { createRoomGuestApi, RoomGuestApiError } from '../src/lib/room-guest-api.ts';

const base = '/api/local/room-invites';
function fixture(value: unknown, status = 200) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fetcher: typeof fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return Response.json(value, { status });
  };
  return { calls, fetcher };
}

test('unpair preserves the server notice and uses the non-pairing DELETE route', async () => {
  const result = { unpaired: true, peer_id: 'p', generation: 2, notice: 'Existing native grants are not revoked.' };
  const { fetcher, calls } = fixture(result);
  const signal = new AbortController().signal;
  assert.deepEqual(await createRoomPairingApi('owner', fetcher).removePeer('p/q', signal), result);
  assert.equal(calls[0].url, `${base}/peers/p%2Fq`);
  assert.equal(calls[0].init.method, 'DELETE');
  assert.equal(calls[0].init.body, undefined);
  assert.equal(calls[0].init.signal, signal);
  assert.equal(calls[0].init.cache, 'no-store');
});

test('active peers come from GET /peers, not the bounded pairing journal', async () => {
  const peers = [{ peer_id: 'p', installation_id: 'i', profile: 'b', state: 'acknowledged', role: 'host', generation: 1 }];
  const { fetcher, calls } = fixture({ peers });
  const api = createRoomPairingApi('owner', fetcher);
  assert.equal(typeof api.listPeers, 'function');
  assert.deepEqual(await api.listPeers(), peers);
  assert.equal(calls[0].url, `${base}/peers`);
  assert.equal(calls[0].init.method, 'GET');
  assert.equal(calls[0].init.body, undefined);
  assert.equal(calls[0].init.credentials, 'same-origin');
});

test('pairing wire expiries stay integer epoch seconds for offer, accept, reveal and journal', async () => {
  const expires_at = 1792869000;
  const row = { id: 'p', role: 'host', state: 'confirmed', expires_at };
  const offer = { id: 'p', code: 'public-code', expires_at };
  const { fetcher } = fixture({ offer, accept: { ...offer, identity: {} }, reveal: { ...offer, sas: '001234' },
    ...offer, sas: '001234', pairing: row, pairings: [row], available: true });
  const api = createRoomPairingApi(undefined, fetcher);
  for (const value of [await api.createOffer(), await api.importOffer('code'), await api.acceptOffer('p', 'code'),
    await api.revealImport('p', 'code'), await api.confirmOffer('p', '001234'),
    await api.confirmImport('p', '001234'), ...(await api.listPairings())]) {
    assert.equal(value.expires_at, expires_at);
    assert.equal(Number.isInteger(value.expires_at), true);
  }
});

for (const [status, code, kind] of [
  [409, 'pairing_unavailable', 'conflict'], [409, 'pairing_sas_mismatch', 'conflict'],
  [429, 'pairing_cooldown', 'error'], [503, 'pairing_busy', 'error'],
  [503, 'pairing_native_unavailable', 'error'], [503, 'pairing_identity_unavailable', 'error'],
  [404, 'http_404', 'unavailable'],
] as const) {
  test(`pairing ${status}/${code} preserves only the generic problem`, async () => {
    const { fetcher, calls } = fixture({ ...(status === 404 ? {} : { code }), path: '/private', sas: '123456' }, status);
    await assert.rejects(() => createRoomPairingApi(undefined, fetcher).createOffer(), (error: unknown) => {
      assert.ok(error instanceof RoomPairingApiError);
      assert.deepEqual(error.problem, { kind, code });
      assert.equal(error.message, code);
      assert.doesNotMatch(JSON.stringify(error), /private|123456/);
      return true;
    });
    assert.equal(calls.length, 1);
  });
}

test('guest responses preserve rooms, verbatim event fields and the exact bot envelope', async () => {
  const rooms = [{ room_id: 'r', name: 'Room', owner: 'Host', owner_online: true, members: [], permissions: [] }];
  const page = { events: [{ seq: 42, event_id: 'immutable-id', kind: 'native-event', payload: { extra: 'untouched' } }],
    cursor: 'opaque-cursor', latest_seq: 99, has_more: true };
  const bot = { status: 'pending', approvals: [{ request_id: 'q', title: 'Build' }], permissions: ['build'] };
  const signal = new AbortController().signal;
  for (const [value, invoke, suffix] of [
    [{ rooms }, (api: ReturnType<typeof createRoomGuestApi>) => api.listRooms(signal), '/guest-rooms'],
    [page, (api: ReturnType<typeof createRoomGuestApi>) => api.getLog('r', { sinceSeq: 0, limit: 200 }, signal),
      '/guest-rooms/r/log?since_seq=0&limit=200'],
    [bot, (api: ReturnType<typeof createRoomGuestApi>) => api.getMyBot('r', signal), '/guest-rooms/r/my-bot'],
  ] as const) {
    const { fetcher, calls } = fixture(value);
    assert.deepEqual(await invoke(createRoomGuestApi('owner', fetcher)), 'rooms' in value ? rooms : value);
    assert.equal(calls[0].url, base + suffix);
    assert.equal(calls[0].init.method, 'GET');
    assert.equal(calls[0].init.signal, signal);
    assert.equal(calls[0].init.body, undefined);
  }
});

for (const [status, code, kind] of [[503, 'host_offline', 'error'], [403, 'forbidden', 'forbidden'],
  [501, 'not_supported', 'error'], [404, 'http_404', 'unavailable']] as const) {
  test(`guest ${status}/${code}: reads and mutations fail without retry or native scope`, async () => {
    const { fetcher, calls } = fixture(status === 404 ? {} : { code, private_detail: 'secret' }, status);
    const api = createRoomGuestApi('owner', fetcher);
    for (const invoke of [() => api.getMyBot('r'), () => api.getLog('r'),
      () => api.decideApproval('r', 'q', 'approve'), () => api.leaveRoom('r')]) {
      await assert.rejects(invoke, (error: unknown) => {
        assert.ok(error instanceof RoomGuestApiError);
        assert.deepEqual(error.problem, { kind, code });
        assert.equal(error.code, code);
        assert.doesNotMatch(JSON.stringify(error), /secret|private_detail/);
        return true;
      });
    }
    assert.equal(calls.length, 4);
    assert.equal(calls[2].init.body, JSON.stringify({ choice: 'approve' }));
    assert.equal(calls[3].init.body, '{}');
  });
}
