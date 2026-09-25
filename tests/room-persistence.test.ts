/**
 * Regression contract for the cross-device last-room claim.
 *
 * The telemetry store rejects a claim whose `expectedRevision` does not match
 * the canonical revision. The UI sends NO revision whenever it switches to a
 * room other than the one it last saw, so a single attempt always conflicts and
 * the shared pointer freezes at the first room ever selected — every device then
 * keeps adopting that stale room. `claimLastRoomPointer` must retry with the
 * canonical revision returned by the 409.
 */
import { claimLastRoomPointer, syncLastRoomToServer } from '../src/lib/room-persistence.ts';

function assertEqual(actual: unknown, expected: unknown, label: string) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

type Call = { body: Record<string, unknown>; authorization: string | null };

/** Minimal stand-in for the telemetry store: revisioned CAS, 409 on mismatch. */
function installFakeStore() {
  const calls: Call[] = [];
  let pointer: { roomId: string; revision: number } | null = null;

  globalThis.fetch = (async (_url: string, init: RequestInit = {}) => {
    const body = init.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
    const headers = (init.headers ?? {}) as Record<string, string>;
    calls.push({ body, authorization: headers.Authorization ?? null });

    const expected = body.expectedRevision;
    if (pointer && expected !== pointer.revision) {
      return {
        ok: false,
        status: 409,
        json: async () => ({ error: 'revision_conflict', lastRoom: pointer }),
      } as unknown as Response;
    }
    pointer = { roomId: String(body.roomId), revision: (pointer?.revision ?? 0) + 1 };
    return {
      ok: true,
      status: 200,
      json: async () => ({ success: true, lastRoom: pointer }),
    } as unknown as Response;
  }) as typeof fetch;

  return { calls, current: () => pointer };
}

// 1. A blind claim (no revision) against an existing pointer must conflict, so
//    the single-attempt primitive is not sufficient on its own.
{
  const store = installFakeStore();
  store.current();
  await claimLastRoomPointer('mc-A', 'A', 'token', null);
  const single = await syncLastRoomToServer('mc-B', 'B', 'token', null);
  assertEqual(single.accepted, false, 'blind single attempt is rejected');
  assertEqual(single.conflict, true, 'blind single attempt reports conflict');
  assertEqual(store.current()?.roomId, 'mc-A', 'pointer unchanged without retry');
}

// 2. The retry is what moves the pointer — across repeated switches, the
//    canonical revision adopted from the 409 always wins.
{
  const store = installFakeStore();
  let ref: { roomId: string; revision: number } | null = null;
  for (const target of ['mc-A', 'mc-B', 'mc-C', 'mc-D']) {
    const expected = ref && ref.roomId === target ? ref.revision : null;
    const result = await claimLastRoomPointer(target, target, 'token', expected);
    assertEqual(result.accepted, true, `switch to ${target} is accepted`);
    assertEqual(store.current()?.roomId, target, `pointer moved to ${target}`);
    if (result.lastRoom) ref = { roomId: result.lastRoom.roomId, revision: result.lastRoom.revision };
  }
  assertEqual(store.current()?.revision, 4, 'one revision per accepted claim');
  assertEqual(store.current()?.roomId, 'mc-D', 'last claim wins');
}

// 3. A matching revision takes the fast path: exactly one request, no retry.
{
  const store = installFakeStore();
  const first = await claimLastRoomPointer('mc-A', 'A', 'token', null);
  store.calls.length = 0;
  const again = await claimLastRoomPointer('mc-A', 'A', 'token', first.lastRoom?.revision ?? null);
  assertEqual(store.calls.length, 1, 'matching revision does not retry');
  assertEqual(again.accepted, true, 'matching revision is accepted');
}

// 4. The claim carries the bearer token and the room name, and it terminates
//    instead of retrying forever when the store keeps conflicting.
{
  const store = installFakeStore();
  await claimLastRoomPointer('mc-A', 'Example room', 'secret-token', null);
  assertEqual(store.calls[0].authorization, 'Bearer secret-token', 'token is forwarded');
  assertEqual(store.calls[0].body.roomName, 'Example room', 'room name is forwarded');

  let alwaysConflict = 0;
  globalThis.fetch = (async () => {
    alwaysConflict += 1;
    return {
      ok: false,
      status: 409,
      json: async () => ({ error: 'revision_conflict', lastRoom: { roomId: 'mc-A', revision: 1 } }),
    } as unknown as Response;
  }) as typeof fetch;
  const bounded = await claimLastRoomPointer('mc-B', null, 'token', null);
  assertEqual(bounded.accepted, false, 'persistent conflict is not accepted');
  assertEqual(alwaysConflict <= 3, true, `retries are bounded (made ${alwaysConflict} calls)`);
  assertEqual(alwaysConflict, 3, 'retries use exactly MAX_CLAIM_ATTEMPTS');
}

// 5. An empty room id never reaches the network.
{
  const store = installFakeStore();
  const result = await claimLastRoomPointer('   ', null, 'token', null);
  assertEqual(result.accepted, false, 'blank room id is rejected');
  assertEqual(store.calls.length, 0, 'blank room id performs no request');
}

console.log('room persistence claim tests passed');
