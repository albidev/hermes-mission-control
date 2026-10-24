import assert from 'node:assert/strict';
import test from 'node:test';
import { canAdvanceJoining, canGiveConsent } from '../src/lib/room-joining-state.ts';
test('joining flow is ordered and consent is blocked before scope', () => { assert.equal(canAdvanceJoining('paste', 'host'), true); assert.equal(canAdvanceJoining('paste', 'consent'), false); assert.equal(canGiveConsent('scope', true), false); assert.equal(canGiveConsent('scope_confirmed', true), true); });
test('joining flow supports cancellation and expiry from every active step', () => { for (const step of ['paste','host','metadata','redeem','approval','scope','scope_confirmed','consent','activation'] as const) { assert.equal(canAdvanceJoining(step, 'cancelled'), true); assert.equal(canAdvanceJoining(step, 'expired'), true); } });
