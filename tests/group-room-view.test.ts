import {
  deriveGroupMemberStatus,
  groupEventLabel,
  visibleGroupEvents,
  type GroupMemberStatus,
} from '../src/components/chat/group-room-view-model.ts';
import type { GroupEvent, GroupMember } from '../src/lib/group-gateway.ts';

function assertEqual<T>(actual: T, expected: T) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

const event = (kind: string, seq: number, actorKind: 'user' | 'member' | 'system', text: string, member?: string): GroupEvent => ({
  id: `${kind}-${seq}`,
  seq,
  kind,
  actor: { kind: actorKind, id: member ?? actorKind },
  message: { text, threadId: null, member: member ? { id: member } : null },
  createdAt: 1_700_000_000_000 + seq * 1000,
});

const members: GroupMember[] = [
  { id: 'a', profile: 'default', handle: 'alpha', displayName: 'Alpha' },
  { id: 'b', profile: 'default', handle: 'beta' },
];

assertEqual(visibleGroupEvents([
  event('message.user', 1, 'user', 'Question'),
  event('turn.started', 2, 'system', 'ignored'),
  event('message.member', 3, 'member', 'Answer', 'a'),
  event('driver_status', 4, 'system', 'ignored'),
], members), [
  { event: event('message.user', 1, 'user', 'Question'), member: null },
  { event: event('message.member', 3, 'member', 'Answer', 'a'), member: members[0] },
]);

assertEqual(groupEventLabel(event('turn.started', 2, 'system', '')), 'Turn started');
assertEqual(groupEventLabel(event('message.member', 3, 'member', '')), 'Final answer');

const statuses: GroupMemberStatus[] = ['idle', 'working', 'settled', 'unavailable'];
for (const status of statuses) {
  const driverStatus = status === 'unavailable' ? { unavailable: ['a'] } : status === 'working' ? { status: 'working', member_id: 'a' } : status === 'settled' ? { status: 'settled', member_id: 'a' } : {};
  assertEqual(deriveGroupMemberStatus(members[0], driverStatus, status === 'idle' ? null : event('message.member', 3, 'member', 'done', 'a')), status);
}
