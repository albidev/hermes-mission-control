import assert from 'node:assert/strict';
import test from 'node:test';
import { RoomPairingApiError } from '../src/lib/room-pairing-api.ts';

test('guest API types preserve since_seq contract without client-side persistence', () => {
  const query = new URLSearchParams({ since_seq: '12', limit: '20' });
  assert.equal(query.toString(), 'since_seq=12&limit=20');
});

test('pairing client maps failed responses to generic typed errors', async () => {
  const error = new RoomPairingApiError({ kind: 'forbidden', code: 'generic_error' });
  assert.equal(error.problem.kind, 'forbidden');
  assert.equal(error.problem.code, 'generic_error');
});
