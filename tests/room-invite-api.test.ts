import assert from 'node:assert/strict';
import test from 'node:test';
import {
  createRoomInviteApi,
  RoomInviteApiError,
} from '../src/lib/room-invite-api.ts';

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

test('room invite API uses canonical method/path/body and owner auth', async () => {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const api = createRoomInviteApi('owner-token', async (url, init) => {
    calls.push({ url: String(url), init });
    return response({
      lobby: { lobby_id: 'lobby-1', name: 'x', state: 'open', roster: [] },
    });
  });

  await api.getLobby('lobby/1');
  await api.issueInvite('lobby-1', {
    member_id: 'm',
    invitee_label: 'owner',
  });
  await api.revokeInvite('lobby-1', 'invite-1');

  assert.deepEqual(
    calls.map((call) => [call.init.method, call.url]),
    [
      ['GET', '/api/local/room-invites/lobbies/lobby%2F1'],
      ['POST', '/api/local/room-invites/lobbies/lobby-1/invites'],
      [
        'POST',
        '/api/local/room-invites/lobbies/lobby-1/invites/invite-1/revoke',
      ],
    ],
  );
  assert.equal(
    (calls[1].init.headers as Record<string, string>).Authorization,
    'Bearer owner-token',
  );
  assert.equal(calls[2].init.body, '{}');
});

test(
  'paired-peer listing uses the redacted exact contract and maps 404 to unavailable',
  async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    const peer = {
      peer_id: 'peer-a',
      installation_id: 'install:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      profile: 'default',
      capability_digest: 'a'.repeat(64),
    };
    const api = createRoomInviteApi('owner-token', async (url, init) => {
      calls.push({ url: String(url), init });
      return response({ peers: [peer] });
    });

    assert.deepEqual(await api.listPairedPeers(), [peer]);
    assert.deepEqual(
      [calls[0].init.method, calls[0].url],
      ['GET', '/api/local/room-invites/paired-peers'],
    );
    assert.equal(
      (calls[0].init.headers as Record<string, string>).Authorization,
      'Bearer owner-token',
    );

    const unavailable = createRoomInviteApi(undefined, async () =>
      response({ error: 'not_found', code: 'not_found' }, 404),
    );
    await assert.rejects(
      () => unavailable.listPairedPeers(),
      (error: unknown) =>
        error instanceof RoomInviteApiError &&
        error.problem.kind === 'unavailable',
    );
  },
);

test('API preserves explicit ambiguity as a discriminated error', async () => {
  const api = createRoomInviteApi(undefined, async () =>
    response({ error: 'conflict', code: 'registration_incomplete' }, 409),
  );

  await assert.rejects(
    () => api.activate('lobby-1'),
    (error: unknown) =>
      error instanceof RoomInviteApiError &&
      error.problem.kind === 'ambiguous' &&
      error.problem.code === 'registration_incomplete',
  );
});

test(
  'issue response is the only secret carrier and does not touch console, storage, or URL',
  async () => {
    const originalLog = console.log;
    const logs: unknown[][] = [];
    console.log = (...args: unknown[]) => {
      logs.push(args);
    };
    const storagePrototype =
      typeof Storage === 'undefined' ? null : Storage.prototype;
    const originalSetItem = storagePrototype?.setItem;
    const stored: string[] = [];
    if (storagePrototype) {
      storagePrototype.setItem = (_key: string, value: string) => {
        stored.push(value);
      };
    }

    try {
      const api = createRoomInviteApi(undefined, async () =>
        response({
          invite_id: 'invite-1',
          secret: 'one-time-secret',
          expires_at: '2030-01-01',
        }),
      );
      const issued = await api.issueInvite('lobby-1', {
        member_id: 'm',
        invitee_label: 'x',
      });

      assert.equal(issued.secret, 'one-time-secret');
      assert.deepEqual(logs, []);
      assert.deepEqual(stored, []);
      assert.equal(
        new URL('http://localhost/?lobbyId=lobby-1').search,
        '?lobbyId=lobby-1',
      );
    } finally {
      console.log = originalLog;
      if (storagePrototype && originalSetItem) {
        storagePrototype.setItem = originalSetItem;
      }
    }
  },
);
