import {
  deriveRoundCoordinates,
  mapActorMember,
  mergeGroupEvents,
  toGroupRoomError,
  useGroupRoom,
} from '../src/lib/use-group-room.ts';
import { normalizeGroupEvent, type GroupEvent, type GroupMember } from '../src/lib/group-gateway.ts';

function assertEqual<T>(actual: T, expected: T) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

const event = (id: string, seq: number, text: string, actorId = 'member-a'): GroupEvent => normalizeGroupEvent({
  event_id: id,
  seq,
  kind: 'message.member',
  actor: { kind: 'member', id: actorId },
  payload: { text },
});

assertEqual(mergeGroupEvents([event('a', 1, 'old'), event('b', 2, 'two')], [event('a', 1, 'new'), event('c', 3, 'three')]), [
  event('a', 1, 'new'), event('b', 2, 'two'), event('c', 3, 'three'),
]);
assertEqual(mergeGroupEvents([event('a', 1, 'one')], [event('duplicate', 1, 'replacement')]), [event('duplicate', 1, 'replacement')]);

const members: GroupMember[] = [{ id: 'member-a', profile: 'default', handle: 'albi' }];
assertEqual(mapActorMember({ kind: 'member', id: 'member-a' }, members), members[0]);
assertEqual(mapActorMember({ kind: 'user', id: 'desktop' }, members), null);

assertEqual(deriveRoundCoordinates([
  { ...event('a', 1, 'one'), round: 2, coordinates: { x: 10, y: 20 } } as GroupEvent & { round: number; coordinates: { x: number; y: number } },
  { ...event('b', 2, 'two'), round: 2, coordinates: { x: 30, y: 40 } } as GroupEvent & { round: number; coordinates: { x: number; y: number } },
]), { round: 2, coordinates: [{ x: 10, y: 20 }, { x: 30, y: 40 }] });

if (typeof useGroupRoom !== 'function') throw new Error('useGroupRoom must be exported');
assertEqual(toGroupRoomError(new Error('Gateway connection closed during groups.log.')), {
  message: 'Gateway connection closed during groups.log.', retryable: true,
});
