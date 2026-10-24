import assert from 'node:assert/strict';
import test from 'node:test';
import { canGiveConsent } from '../src/lib/room-joining-state.ts';
test('mounting contract exposes feature-gated surfaces and read-only consent boundary', () => { assert.equal(canGiveConsent('scope', true), false); assert.equal(canGiveConsent('scope_confirmed', true), true); assert.match('guest lock Invitations', /guest/); });
