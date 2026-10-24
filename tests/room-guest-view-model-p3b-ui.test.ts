import assert from 'node:assert/strict';
import test from 'node:test';
import { mergeGuestEvents, type GuestLogEvent } from '../src/lib/room-guest-api.ts';
import { defaultGuestTab, guestEventToChatMessage, guestOfflineTime } from '../src/lib/room-guest-view-model.ts';
const event = (event_id: string, seq: number): GuestLogEvent => ({
  event_id, seq, kind: 'message', actor: 'Basa', authority_epoch: 2,
  payload: { text: `message-${seq}` }, created_at: '2026-10-24T12:00:00Z',
});

test('guest transcript sorts by original seq, dedups overlapping pages and keeps immutable event objects', () => {
  const first = event('a', 1);
  const second = event('b', 2);
  const third = event('c', 4);
  const input = [second, first];
  const result = mergeGuestEvents(input, [event('b', 99), third]);
  assert.deepEqual(result.map((item) => item.seq), [1, 2, 4]);
  assert.deepEqual(result.map((item) => item.event_id), ['a', 'b', 'c']);
  assert.equal(result[0], first);
  assert.equal(result[1], second);
  assert.equal(result[2], third);
  assert.deepEqual(input, [second, first]);
  assert.equal(result[1].authority_epoch, 2);
  assert.deepEqual(mergeGuestEvents(result, []), result);
});

test('shared presentation adapter retains wire identity and payload without JSON text wrapping', () => {
  const original = event('stable-id', 42);
  const message = guestEventToChatMessage(original);
  assert.equal(message.id, 'stable-id');
  assert.equal(message.text, 'message-42');
  assert.equal(message.role, 'assistant');
  assert.equal(message.attribution?.displayName, 'Basa');
  assert.equal(message.createdAt, Date.parse(original.created_at));
  assert.deepEqual(original.payload, { text: 'message-42' });
  assert.equal(guestEventToChatMessage({ ...original, payload: 'verbatim' }).text, 'verbatim');
  assert.equal(guestEventToChatMessage({ ...original, payload: { action: 'joined' } }).text, '{"action":"joined"}');
});

test('My bot is default; offline time is explicit and malformed timestamps are never printed', () => {
  assert.equal(defaultGuestTab, 'bot');
  assert.equal(guestOfflineTime('14:02'), '14:02');
  assert.equal(guestOfflineTime(undefined), '—');
  assert.equal(guestOfflineTime('not-a-date'), '—');
});
