import {
  GroupGatewayClient,
  GroupServiceUnavailableError,
  createGroupGatewayClient,
  normalizeGroupEvent,
  normalizeGroupLog,
  normalizeGroupRoom,
} from '../src/lib/group-gateway.ts';

function assertEqual<T>(actual: T, expected: T) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

const room = normalizeGroupRoom({
  room_id: 'room-1',
  name: 'Ops',
  authority_gateway_id: 'gateway-a',
  authority_epoch: 3,
  latest_seq: 4,
  members: [{ member_id: 'member-a', profile: 'default', handle: 'albi', target_profile: 'default' }],
});
assertEqual(room, {
  id: 'room-1',
  name: 'Ops',
  authorityGatewayId: 'gateway-a',
  authorityEpoch: 3,
  latestSeq: 4,
  disbandedAt: null,
  members: [{ id: 'member-a', profile: 'default', handle: 'albi', targetProfile: 'default' }],
});

assertEqual(normalizeGroupEvent({
  event_id: 'evt-1', seq: 1, kind: 'message.member',
  actor: { kind: 'member', id: 'member-a' },
  payload: { text: 'hello', thread_id: 'thread-1', member: { member_id: 'member-a', handle: 'albi' } },
  created_at: '2026-09-10T20:00:00Z',
}), {
  id: 'evt-1', seq: 1, kind: 'message.member', actor: { kind: 'member', id: 'member-a' },
  message: { text: 'hello', threadId: 'thread-1', member: { id: 'member-a', handle: 'albi' } },
  createdAt: Date.parse('2026-09-10T20:00:00Z'),
});

assertEqual(normalizeGroupLog({
  events: [{ event_id: 'evt-1', seq: 1, kind: 'message.user', actor: { kind: 'user', id: 'desktop' }, payload: { text: 'x' }, created_at: 123 }],
  cursor: 1, latest_seq: 2, has_more: true,
  authority: { gateway_id: 'gateway-a', epoch: 3 },
}), {
  events: [{ id: 'evt-1', seq: 1, kind: 'message.user', actor: { kind: 'user', id: 'desktop' }, message: { text: 'x', threadId: null, member: null }, createdAt: 123000 }],
  cursor: 1, latestSeq: 2, hasMore: true, authority: { gatewayId: 'gateway-a', epoch: 3 },
});

const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
const client = new GroupGatewayClient(async (method, params) => {
  calls.push({ method, params });
  if (method === 'groups.list') return { rooms: [], next_offset: null };
  if (method === 'groups.capabilities') return { protocol_version: 2, methods: ['groups.list'] };
  return { room: { room_id: 'room-1' } };
});
await client.capabilities();
await client.list({ limit: 2, offset: 4, cursor: 'cursor-1' });
await client.log('room-1', { cursor: 100 });
await client.create({ roomId: 'room-1', name: 'Ops', roster: [
  { memberId: 'member-a', profile: 'default', handle: 'albi', targetProfile: 'default' },
  { memberId: 'member-b', profile: 'reviewer', handle: 'reviewer', targetProfile: 'reviewer', targetInstallId: 'install-b' },
] });
await client.send('room-1', 'event-1', { text: 'hello', threadId: 'thread-1' });
await client.rename('room-1', 'rename-1', 'Renamed');
await client.disband('room-1');
assertEqual(calls.map((call) => call.method), [
  'groups.capabilities', 'groups.list', 'groups.log', 'groups.create', 'groups.send', 'groups.rename', 'groups.disband',
]);
assertEqual(calls[2].params, { room_id: 'room-1', since_seq: 100 });
assertEqual(calls[4].params, { room_id: 'room-1', event_id: 'event-1', payload: { text: 'hello', thread_id: 'thread-1' } });
assertEqual(Object.prototype.hasOwnProperty.call(calls[3].params, 'actor'), false);

const unavailable = new GroupGatewayClient(async () => { throw new GroupServiceUnavailableError(); });
try {
  await unavailable.list();
  throw new Error('expected service unavailable');
} catch (error) {
  assertEqual(error instanceof GroupServiceUnavailableError, true);
}

const scopedTokens: Array<string | undefined> = [];
const authenticated = createGroupGatewayClient('room-token', async <T = unknown>(method: string, _params: Record<string, unknown>, accessToken?: string) => {
  scopedTokens.push(accessToken);
  if (method === 'groups.capabilities') return { protocol_version: 2, driver: true, methods: ['groups.send'] } as T;
  return { event: { event_id: 'event-2', seq: 2, kind: 'message.user', actor: { kind: 'user', id: 'desktop' }, payload: { text: 'hello' } } } as T;
});
await authenticated.capabilities();
await authenticated.send('room-1', 'event-2', { text: 'hello' });
assertEqual(scopedTokens, ['room-token', 'room-token']);
