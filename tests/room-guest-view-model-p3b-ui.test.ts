import assert from 'node:assert/strict';
import test from 'node:test';
import { mergeGuestEvents } from '../src/lib/room-guest-api.ts';
const event = (event_id: string, seq: number) => ({ event_id, seq, kind: 'message', actor: 'bot', authority_epoch: 1, payload: event_id, created_at: '' });
test('guest transcript deduplicates event_id and keeps original seq order', () => { assert.deepEqual(mergeGuestEvents([event('b', 2), event('a', 1)], [event('b', 2), event('c', 3)]).map((item) => item.event_id), ['a', 'b', 'c']); });
test('guest transcript does not rewrite payloads', () => { const item = event('x', 9); assert.deepEqual(mergeGuestEvents([], [item])[0], item); });
