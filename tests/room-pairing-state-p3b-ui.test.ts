import assert from 'node:assert/strict';
import test from 'node:test';
import { canConfirmSas, initialPairingWizardState, pairingWizardReducer } from '../src/lib/room-joining-state.ts';
test('pairing supports cancellation and expiry', () => { const started = pairingWizardReducer(initialPairingWizardState, { type: 'start_offer', id: 'x', expiresAt: 10 }); assert.equal(pairingWizardReducer(started, { type: 'cancel' }).step, 'cancelled'); assert.equal(pairingWizardReducer(started, { type: 'expire' }).step, 'expired'); });
test('SAS rejects whitespace and non-six-digit values', () => { assert.equal(canConfirmSas('123456'), true); assert.equal(canConfirmSas(' 123456'), false); assert.equal(canConfirmSas('12345a'), false); });
