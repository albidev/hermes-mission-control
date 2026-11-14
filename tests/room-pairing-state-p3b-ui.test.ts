import assert from 'node:assert/strict';
import test from 'node:test';
import {
  canConfirmSas, initialPairingWizardState, pairingWizardReducer, secondsRemaining,
} from '../src/lib/room-joining-state.ts';

test('host follows offer → accept/reveal → SAS → local confirmation, clearing SAS', () => {
  const offer = pairingWizardReducer(initialPairingWizardState, { type: 'start_offer', id: 'a', expiresAt: 15000 });
  assert.equal(offer.step, 'offer');
  assert.equal(offer.id, 'a');
  assert.equal(offer.expiresAt, 15000);
  const reveal = pairingWizardReducer(offer, { type: 'accept', sas: '000123' });
  assert.equal(reveal.step, 'reveal');
  assert.equal(reveal.sas, '000123');
  const sas = pairingWizardReducer(reveal, { type: 'reveal', sas: '000123' });
  assert.equal(sas.step, 'sas');
  const complete = pairingWizardReducer(sas, { type: 'confirm' });
  assert.equal(complete.step, 'complete');
  assert.equal(complete.sas, null);
  assert.equal(pairingWizardReducer(complete, { type: 'reveal', sas: '123456' }), complete);
});

test('joining role imports offer then reveal; cannot skip SAS', () => {
  const accepted = pairingWizardReducer(initialPairingWizardState, { type: 'import_offer', id: 'b', expiresAt: 9000 });
  assert.equal(accepted.step, 'accept');
  assert.equal(pairingWizardReducer(accepted, { type: 'confirm' }), accepted);
  const sas = pairingWizardReducer(accepted, { type: 'reveal', sas: '123456' });
  assert.equal(sas.step, 'sas');
  assert.equal(pairingWizardReducer(sas, { type: 'confirm' }).step, 'complete');
});

test('invalid transitions cannot inject/replace SAS or overwrite an active offer', () => {
  assert.equal(pairingWizardReducer(initialPairingWizardState, { type: 'confirm' }), initialPairingWizardState);
  assert.equal(pairingWizardReducer(initialPairingWizardState, { type: 'reveal', sas: '123456' }), initialPairingWizardState);
  const offer = pairingWizardReducer(initialPairingWizardState, { type: 'start_offer', id: 'a', expiresAt: 1000 });
  assert.equal(pairingWizardReducer(offer, { type: 'accept', sas: 'bad' }), offer);
  assert.equal(pairingWizardReducer(offer, { type: 'start_offer', id: 'b', expiresAt: 1000 }), offer);
});

test('cancel and expiry are terminal, clear SAS and require deliberate reset', () => {
  const state = { ...initialPairingWizardState, step: 'sas' as const, sas: '123456', id: 'x' };
  for (const type of ['cancel', 'expire'] as const) {
    const terminal = pairingWizardReducer(state, { type });
    assert.equal(terminal.step, type === 'cancel' ? 'cancelled' : 'expired');
    assert.equal(terminal.sas, null);
    assert.equal(pairingWizardReducer(terminal, { type: 'confirm' }), terminal);
    assert.deepEqual(pairingWizardReducer(terminal, { type: 'reset' }), initialPairingWizardState);
  }
  assert.equal(pairingWizardReducer(state, { type: 'error', code: 'pairing_unavailable' }).error, 'pairing_unavailable');
});

test('SAS is exactly six ASCII digits; countdown has a hard zero boundary', () => {
  for (const value of ['12345', '1234567', ' 123456', '123456\n', '12345a', '１２３４５６', '']) {
    assert.equal(canConfirmSas(value), false, value);
  }
  assert.equal(canConfirmSas('000000'), true);
  assert.equal(canConfirmSas('123456'), true);
  assert.equal(secondsRemaining(1000, 0), 1);
  assert.equal(secondsRemaining(1000, 999), 1);
  assert.equal(secondsRemaining(1000, 1000), 0);
  assert.equal(secondsRemaining(1000, 1001), 0);
  assert.equal(secondsRemaining(null, 0), 0);
});
