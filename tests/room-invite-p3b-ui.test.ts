import assert from 'node:assert/strict';
import test from 'node:test';
import { canConfirmSas, canGiveConsent, canAdvanceJoining, initialPairingWizardState, pairingWizardReducer } from '../src/lib/room-joining-state.ts';
import { mergeGuestEvents } from '../src/lib/room-guest-api.ts';

test('pairing SAS is exactly six digits and confirms only from sas step', () => {
  assert.equal(canConfirmSas('123456'), true);
  assert.equal(canConfirmSas('12345'), false);
  assert.equal(pairingWizardReducer(initialPairingWizardState, { type: 'confirm' }).step, 'start');
  const sas = pairingWizardReducer({ ...initialPairingWizardState, step: 'sas', sas: '123456' }, { type: 'confirm' });
  assert.equal(sas.step, 'complete');
});
test('joining consent requires explicit scope confirmation', () => {
  assert.equal(canGiveConsent('scope', false), false);
  assert.equal(canGiveConsent('scope_confirmed', false), false);
  assert.equal(canGiveConsent('scope_confirmed', true), true);
  assert.equal(canAdvanceJoining('scope', 'scope_confirmed'), true);
});
test('guest events deduplicate by event id and preserve sequence order', () => {
  const event = (event_id: string, seq: number) => ({ event_id, seq, kind: 'message', actor: 'a', authority_epoch: 1, payload: event_id, created_at: '' });
  assert.deepEqual(mergeGuestEvents([event('b', 2), event('a', 1)], [event('b', 2), event('c', 3)]).map((item) => item.event_id), ['a', 'b', 'c']);
});
