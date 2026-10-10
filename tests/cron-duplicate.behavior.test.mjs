import assert from 'node:assert/strict';
import { routeHarness } from './helpers/route-dom-harness.mjs';

const source = {
  id: 'original',
  name: 'Source job',
  profile: 'missing-profile',
  enabled: true,
  state: 'scheduled',
  schedule: { kind: 'interval', expr: 'every 1h', display: 'Every hour' },
  prompt: 'Fixture prompt',
  model: 'fixture-model',
  provider: 'fixture-provider',
  deliver: 'discord:fixture',
  repeat: { times: 4, completed: 3 },
  skills: ['one', 'two'],
  enabled_toolsets: ['web', 'file'],
  workdir: '/fixture/project',
  script: 'fixture.sh',
  no_agent: true,
  monitor_script: 'monitor.sh',
  monitor_url: 'https://fixture.invalid',
  reasoning_effort: 'high',
  last_run_at: '2026-01-01T00:00:00Z',
  next_run_at: '2026-10-10T15:00:00Z',
  last_output: 'Old output',
  context_from: ['hidden-session'],
  attach_to_session: true,
  latest_execution: { status: 'completed' },
  history: ['old'],
};
const original = structuredClone(source);

let jobs = [source];
let deferCreate = false;
let finishCreate;
let edits = 0;

const h = await routeHarness('CronRoute', { storedToken: 'synthetic-token' }, async (call) => {
  if (call.path === '/api/local/cron/jobs' && call.method === 'GET') return Response.json(jobs);
  if (call.path === '/api/local/cron/jobs/original' && call.method === 'GET') return Response.json(source);
  if (call.path === '/api/local/cron/jobs' && call.method === 'POST') {
    if (call.body.schedule === 'bad schedule') return Response.json({ detail: 'Invalid fixture schedule' }, { status: 400 });
    const persist = () => {
      const saved = {
        ...call.body,
        id: 'created',
        enabled: true,
        state: 'scheduled',
        schedule: { kind: 'interval', expr: call.body.schedule, display: call.body.schedule },
        repeat: { times: call.body.repeat, completed: 0 },
      };
      jobs.push(saved);
      return Response.json({ job: saved });
    };
    if (deferCreate) return await new Promise((resolve) => { finishCreate = () => resolve(persist()); });
    return persist();
  }
  if (call.path === '/api/local/cron/jobs/original' && call.method === 'PATCH') {
    edits++;
    return Response.json({ job: { ...source, ...call.body, schedule: source.schedule } });
  }
  throw Error(`Unexpected ${call.method} ${call.path}`);
});

const field = (key) => {
  const label = [...document.querySelectorAll('[role="dialog"] label')].find((e) => e.querySelector('span')?.textContent === `cron.form.${key}`);
  assert.ok(label, `Missing field ${key}`);
  return label.querySelector('input,select,textarea');
};
const mutationCount = () => h.calls.filter((c) => c.method !== 'GET').length;
const duplicate = () => h.flush(() => {
  const row = [...document.querySelectorAll('.cron-job-row')].find((e) => e.textContent.includes('original'));
  assert.ok(row);
  const button = row.querySelector('[aria-label="cron.actions.duplicate"]');
  assert.ok(button, 'Missing duplicate action on the source row');
  button.click();
});
const escape = () => h.flush(() => window.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
const countCreates = () => h.calls.filter((c) => c.method === 'POST' && c.path === '/api/local/cron/jobs').length;
const realNow = Date.now;

try {
  await duplicate();
  assert.equal(h.find('#modal-title').textContent, 'cron.form.createTitle');
  assert.equal(field('name').value, '');
  assert.equal(field('schedule').value, 'every 1h');
  for (const [key, value] of Object.entries({ profile: 'missing-profile', prompt: 'Fixture prompt', delivery: 'discord:fixture', repeat: '4', reasoning: 'high', script: 'fixture.sh', workdir: '/fixture/project', skills: 'one, two', toolsets: 'web, file', monitorScript: 'monitor.sh', monitorUrl: 'https://fixture.invalid' })) {
    assert.equal(field(key).value, value, key);
  }
  assert.equal(field('profile').disabled, false);
  assert.ok([...field('profile').options].some((e) => e.value === 'missing-profile'));
  assert.equal(h.find('#cron-no-agent').checked, true);
  assert.ok(h.find('[aria-label="cron.form.provider"]').textContent.includes('Fixture'));
  assert.ok(h.find('[aria-label="cron.form.model"]').textContent.includes('fixture-model'));
  assert.ok(document.body.textContent.includes('cron.form.duplicateReview'));
  assert.equal(mutationCount(), 0);
  await h.change(field('name'), 'Cancelled');
  await h.click('common.cancel');
  assert.equal(document.querySelector('[role="dialog"]'), null);
  assert.equal(mutationCount(), 0);
  await duplicate();
  assert.equal(field('name').value, '');
  await escape();
  assert.equal(document.querySelector('[role="dialog"]'), null);
  await duplicate();
  await h.flush(() => h.find('[role="presentation"]').dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true })));
  assert.equal(document.querySelector('[role="dialog"]'), null);
  assert.equal(mutationCount(), 0);
  await duplicate();
  await h.click('cron.actions.create');
  assert.ok(document.body.textContent.includes('cron.form.nameRequired'));
  assert.equal(countCreates(), 0);
  await h.change(field('name'), 'Reviewed copy');
  await h.change(field('schedule'), '');
  await h.click('cron.actions.create');
  assert.ok(document.body.textContent.includes('cron.form.scheduleRequired'));
  assert.equal(countCreates(), 0);
  await h.change(field('schedule'), 'bad schedule');
  await h.click('cron.actions.create');
  assert.ok(document.body.textContent.includes('Invalid fixture schedule'));
  assert.ok(document.querySelector('[role="dialog"]'));
  assert.equal(jobs.length, 1);
  await h.change(field('schedule'), 'every 2h');
  deferCreate = true;
  await h.flush(() => {
    const form = h.find('[role="dialog"] form');
    form.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
    form.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  });
  assert.equal(countCreates(), 2, 'One rejected attempt and one in-flight create, not two concurrent creates');
  assert.equal(field('name').matches(':disabled'), true);
  assert.equal(h.button('common.cancel').disabled, true);
  await escape();
  assert.ok(document.querySelector('[role="dialog"]'), 'Escape must not abandon in-flight create');
  await h.flush(() => finishCreate());
  assert.equal(document.querySelector('[role="dialog"]'), null);
  const body = h.calls.filter((c) => c.method === 'POST').at(-1).body;
  assert.deepEqual(body, { name: 'Reviewed copy', profile: 'missing-profile', prompt: 'Fixture prompt', schedule: 'every 2h', deliver: 'discord:fixture', repeat: 4, script: 'fixture.sh', no_agent: true, skills: ['one', 'two'], enabled_toolsets: ['web', 'file'], workdir: '/fixture/project', monitor_script: 'monitor.sh', monitor_url: 'https://fixture.invalid', reasoning_effort: 'high', model: 'fixture-model', provider: 'fixture-provider' });
  assert.deepEqual(source, original);
  const persisted = await fetch('/api/local/cron/jobs').then((r) => r.json());
  assert.equal(persisted.length, 2);
  assert.equal(persisted[1].repeat.completed, 0);
  assert.deepEqual(persisted[0], original);
  await h.click('cron.actions.new');
  assert.equal(field('name').value, '');
  assert.equal(field('profile').value, 'default');
  assert.equal(field('prompt').value, '');
  assert.ok(!document.body.textContent.includes('cron.form.duplicateReview'));
  await escape();
  Date.now = () => Date.parse('2026-10-10T12:00:00Z');
  for (const runAt of ['2020-01-01T00:00:00Z', 'invalid', null]) {
    source.schedule = { kind: 'once', run_at: runAt, display: 'Old one-shot' };
    await h.click('common.refresh');
    await duplicate();
    assert.equal(field('schedule').value, '');
    assert.ok(document.body.textContent.includes('cron.form.duplicateExpired'));
    await escape();
  }
  source.schedule = { kind: 'once', run_at: '2030-01-01T00:00:00Z', display: 'Future one-shot' };
  await h.click('common.refresh');
  await duplicate();
  assert.equal(field('schedule').value, '2030-01-01T00:00:00Z');
  assert.ok(!document.body.textContent.includes('cron.form.duplicateExpired'));
  await h.change(field('name'), 'Future copy');
  await h.change(field('schedule'), '2020-01-01T00:00:00Z');
  const before = countCreates();
  await h.click('cron.actions.create');
  assert.equal(countCreates(), before);
  assert.ok(document.body.textContent.includes('cron.form.futureScheduleRequired'));
  await escape();
  source.schedule = structuredClone(original.schedule);
  await h.click('common.refresh');
  await h.flush(() => [...document.querySelectorAll('.cron-job-row')].find((e) => e.textContent.includes('original')).querySelector('[aria-label="cron.actions.edit"]').click());
  assert.equal(h.find('#modal-title').textContent, 'cron.form.editTitle');
  assert.equal(field('name').value, 'Source job');
  assert.equal(field('profile').disabled, true);
  await h.change(field('name'), 'Edited source');
  await h.click('cron.actions.save');
  assert.equal(edits, 1);
  assert.equal(countCreates(), before);
  assert.deepEqual(source, original);
  console.log('Cron duplicate behavior passed: reviewed create seed, cancellation, validation, one POST, read-back, one-shot and existing create/edit.');
} finally {
  Date.now = realNow;
  await h.close();
}
