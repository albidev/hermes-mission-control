import assert from 'node:assert/strict';
import test from 'node:test';
import { cronScheduleInput } from '../src/lib/cron-form.ts';

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
