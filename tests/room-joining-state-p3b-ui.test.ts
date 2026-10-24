import assert from 'node:assert/strict';
import test from 'node:test';
import { canAdvanceJoining, canGiveConsent, startJoiningPoll, type JoiningStep } from '../src/lib/room-joining-state.ts';

const ordered: JoiningStep[] = [
  'paste', 'host', 'metadata', 'redeem', 'approval', 'scope', 'scope_confirmed', 'consent', 'activation', 'open',
];
test('joining requires each owner decision in order and supports only the unpaired-host skip', () => {
  for (let index = 0; index < ordered.length - 1; index++) {
    assert.equal(canAdvanceJoining(ordered[index], ordered[index + 1]), true);
    assert.equal(canAdvanceJoining(ordered[index + 1], ordered[index]), false);
  }
  assert.equal(canAdvanceJoining('paste', 'metadata'), true);
  assert.equal(canAdvanceJoining('paste', 'consent'), false);
  assert.equal(canAdvanceJoining('scope', 'activation'), false);
  for (const step of ordered) assert.equal(canGiveConsent(step, false), false);
  for (const step of ordered.filter((item) => item !== 'scope_confirmed')) {
    assert.equal(canGiveConsent(step, true), false);
  }
  assert.equal(canGiveConsent('scope_confirmed', true), true);
});

test('every active joining step supports cancel/expiry; terminal steps cannot restart themselves', () => {
  for (const step of ordered.slice(0, -1)) {
    assert.equal(canAdvanceJoining(step, 'cancelled'), true);
    assert.equal(canAdvanceJoining(step, 'expired'), true);
  }
  for (const step of ['open', 'expired', 'cancelled'] as const) {
    for (const target of ordered) assert.equal(canAdvanceJoining(step, target), false);
  }
});

const settle = () => new Promise<void>((resolve) => setImmediate(resolve));
test('polling backs off sequentially to 15s, and cleanup cancels timer and aborts signal', async () => {
  let callback: (() => void) | undefined;
  const delays: number[] = [];
  const cancelled: unknown[] = [];
  const signals: AbortSignal[] = [];
  let received = 0;
  const stop = startJoiningPoll({
    read: async (signal) => { signals.push(signal); return 'awaiting'; },
    onValue: () => { received++; return false; },
    onError: () => assert.fail('unexpected error'),
    schedule: (fn, delay) => { callback = fn; delays.push(delay); return delays.length as unknown as ReturnType<typeof setTimeout>; },
    cancel: (id) => cancelled.push(id),
  });
  await settle();
  for (let index = 0; index < 6; index++) {
    callback!();
    await settle();
  }
  assert.deepEqual(delays, [1000, 2000, 4000, 8000, 15000, 15000, 15000]);
  assert.equal(received, 7);
  assert.equal(signals.length, 7);
  stop();
  assert.deepEqual(cancelled, [7]);
  assert.equal(signals[0].aborted, true);
});

test('unmount during an in-flight poll suppresses late values/errors and never reschedules', async () => {
  let resolve!: (value: string) => void;
  let received = 0;
  let schedules = 0;
  const stop = startJoiningPoll({
    read: () => new Promise<string>((done) => { resolve = done; }),
    onValue: () => { received++; return false; },
    onError: () => assert.fail('late errors must be suppressed'),
    schedule: () => { schedules++; return 0 as unknown as ReturnType<typeof setTimeout>; },
  });
  stop();
  resolve('scope');
  await settle();
  assert.equal(received, 0);
  assert.equal(schedules, 0);
});

test('poll stops on active/scope result and surfaces errors without a retry storm', async () => {
  let schedules = 0;
  let seen: unknown;
  startJoiningPoll({
    read: async () => 'scope', onValue: (value) => { seen = value; return true; },
    onError: () => assert.fail(),
    schedule: () => { schedules++; return 0 as unknown as ReturnType<typeof setTimeout>; },
  });
  await settle();
  assert.equal(seen, 'scope');
  assert.equal(schedules, 0);
  const error = new Error('http_403');
  startJoiningPoll({ read: async () => { throw error; }, onValue: () => false, onError: (value) => { seen = value; } });
  await settle();
  assert.equal(seen, error);
});
