import assert from 'node:assert/strict';
import test from 'node:test';
import { cronScheduleExpired, cronScheduleInput } from '../src/lib/cron-form.ts';

test('one-shot cron editor uses the stored ISO run_at instead of human display text', () => {
  assert.equal(
    cronScheduleInput('once', null, '2026-09-27T10:30:00+02:00', 'once at 2026-09-27 10:30'),
    '2026-09-27T10:30:00+02:00',
  );
});

test('recurring cron editor prefers the parseable schedule expression', () => {
  assert.equal(cronScheduleInput('cron', '0 9 * * *', null, '0 9 * * *'), '0 9 * * *');
  assert.equal(cronScheduleInput('interval', 'every 5m', null, 'every 5m'), 'every 5m');
});

test('legacy schedule without expression or run_at falls back to its display', () => {
  assert.equal(cronScheduleInput('once', null, null, 'once at 2026-09-27 10:30'), 'once at 2026-09-27 10:30');
});

test('a one-shot schedule is expired once its run_at is in the past', () => {
  assert.equal(cronScheduleExpired('once', new Date(Date.now() - 60_000).toISOString()), true);
});

test('a one-shot schedule is not expired while its run_at is still ahead', () => {
  assert.equal(cronScheduleExpired('once', new Date(Date.now() + 3_600_000).toISOString()), false);
});

test('a one-shot schedule whose run_at equals now is already expired', () => {
  const frozen = 1_700_000_000_000;
  const realNow = Date.now;
  Date.now = () => frozen;
  try {
    assert.equal(cronScheduleExpired('once', new Date(frozen).toISOString()), true);
  } finally {
    Date.now = realNow;
  }
});

test('a one-shot schedule without a parseable run_at counts as expired', () => {
  assert.equal(cronScheduleExpired('once', null), true);
  assert.equal(cronScheduleExpired('once', 'not-a-date'), true);
});

test('recurring schedules are never treated as expired', () => {
  assert.equal(cronScheduleExpired('interval', 'every 1h'), false);
  assert.equal(cronScheduleExpired('cron', '0 9 * * *'), false);
  assert.equal(cronScheduleExpired(undefined, '2020-01-01T00:00:00Z'), false);
});
