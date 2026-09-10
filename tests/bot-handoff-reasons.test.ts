import assert from 'node:assert/strict';
import test from 'node:test';
import {
  classifyHandoffFailure,
  isRetryableHandoffReason,
} from '../src/lib/bot-handoff-reasons.ts';

test('classifies structured gateway reason and retryability', () => {
  assert.deepEqual(classifyHandoffFailure({ reason: 'provider_rate_limit', message: 'slow down' }), {
    reason: 'provider_rate_limit',
    retryable: true,
  });
  assert.equal(isRetryableHandoffReason('provider_quota_limit'), false);
});

test('extracts reason marker from gateway error text', () => {
  assert.deepEqual(classifyHandoffFailure(new Error('[reason: target_busy] another turn is running')), {
    reason: 'target_busy',
    retryable: true,
  });
});

test('unknown failures fail closed and are not retryable', () => {
  assert.deepEqual(classifyHandoffFailure('something unexpected'), {
    reason: 'unknown',
    retryable: false,
  });
});
