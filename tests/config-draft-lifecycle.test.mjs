import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { parse as parseYaml } from 'yaml';

// Real React DOM, ConfigRoute, provider, API loader and pull-to-reload hook.
// Only network/timers, translations, diagnostics and the unrelated Honcho panel
// are fixtures. No source inspection and no real config or API access.
const dom = new JSDOM('<!doctype html><div id="root"></div>', {
  url: 'http://localhost/config', pretendToBeVisual: true,
});
for (const key of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement', 'Event', 'MouseEvent', 'CustomEvent']) {
  globalThis[key] = dom.window[key];
}
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
window.HTMLElement.prototype.scrollIntoView = () => {};
let timerId = 0;
const intervals = new Map();
window.setInterval = (fn, delay) => { const id = ++timerId; intervals.set(id, { fn, delay }); return id; };
window.clearInterval = id => intervals.delete(id);
let confirmCount = 0;
let discardConfirmed = false;
window.confirm = () => { confirmCount++; return discardConfirmed; };
const BASE_TEXT = 'fixture:\n  label: disk-value\n  items:\n    - one\n';
const hash = text => createHash('sha256').update(text).digest('hex');
let text, parsed, failure, deferredRead, deferReads, deferredWrite, deferWrites, api, allowWrites, failReadback;
const calls = [];
globalThis.fetch = async (url, options = {}) => {
  const pathname = new URL(String(url), 'http://localhost').pathname;
  const method = options.method ?? 'GET';
  assert.ok(!/^https?:/.test(String(url)) || String(url).startsWith('http://localhost/'), 'No external API access');
  const call = { pathname, method, body: options.body ? JSON.parse(options.body) : null };
  calls.push(call);
  if (pathname.endsWith('/config')) {
    if (method === 'PUT') {
      assert.equal(allowWrites, true, 'Only explicitly enabled synthetic writes');
      if (deferWrites) {
        deferWrites = false;
        await new Promise(resolve => { deferredWrite = resolve; });
      }
      if (call.body.hash !== hash(text)) return Response.json({ error: 'synthetic conflict' }, { status: 409 });
      text = call.body.content;
      parsed = parseYaml(text);
      if (failReadback) failure = 'http';
      return Response.json({ ok: true });
    }
    const readFailure = failure;
    const payload = { content: text, hash: hash(text), path: '/fixture/config.yaml', config: structuredClone(parsed) };
    if (deferReads && method === 'GET') {
      deferReads = false;
      await new Promise(resolve => { deferredRead = resolve; });
    }
    if (readFailure === 'network') throw new Error('synthetic offline');
    if (readFailure === 'auth') return new Response('{}', { status: 401 });
    if (readFailure === 'http') return new Response('{}', { status: 503 });
    assert.equal(method, 'GET', 'Unexpected config write in read-only scenario');
    return Response.json(payload);
  }
  assert.equal(method, 'GET', 'No unrelated mutations');
  if (pathname.endsWith('/tools') || pathname.endsWith('/skills')) return Response.json({ available: true, items: [] });
  if (pathname.endsWith('/system')) return Response.json({ ...api.getFallbackSnapshot().machine, source: 'local', health: 'healthy' });
  if (pathname.endsWith('/status')) return Response.json({ gateway_running: true, model: 'fixture-model' });
  if (pathname.endsWith('/model/info')) return Response.json({ model: 'fixture-model', provider: 'fixture' });
  if (pathname.endsWith('/cron/jobs')) return Response.json([]);
  if (pathname.endsWith('/mission-control/sessions')) return Response.json({ items: [], stats: { totalSessions: 0, activeAgents: 0 } });
  return Response.json({});
};
const cacheDir = mkdtempSync(path.join(tmpdir(), 'mc-config-lifecycle-'));
const server = await createServer({
  root: process.cwd(), configFile: false, cacheDir, appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true, hmr: false },
  plugins: [react(), {
    name: 'config-lifecycle-fixtures', enforce: 'pre',
    resolveId(id) {
      if (/\/i18n(?:\.tsx)?$/.test(id)) return '\0fixture-i18n';
      if (/\/HonchoSettingsPanel(?:\.tsx)?$/.test(id)) return '\0fixture-honcho';
      if (/\/reload-diagnostics(?:\.ts)?$/.test(id)) return '\0fixture-diagnostics';
    },
    load(id) {
      if (id === '\0fixture-i18n') return "export const useI18n = () => ({ locale: 'en', t: key => key });";
      if (id === '\0fixture-honcho') return 'export const HonchoSettingsPanel = () => null;';
      if (id === '\0fixture-diagnostics') return 'export const recordReloadDiagnostic = () => {};';
    },
  }],
});
const { createRoot } = await import('react-dom/client');
let root, context;
let scenarioCount = 0;
const settle = () => new Promise(resolve => setImmediate(resolve));
async function flush(action = () => {}) {
  await act(async () => { await action(); for (let i = 0; i < 6; i++) await settle(); });
}
function button(key) {
  const found = [...document.querySelectorAll('button')].find(el => el.textContent === key);
  assert.ok(found, `Missing button ${key}`);
  return found;
}
function label() { const el = document.getElementById('fixture.label'); assert.ok(el); return el; }
function raw() { const el = document.querySelector('textarea.mobile-config-editor'); assert.ok(el); return el; }
function expectUnloadGuard(expected) {
  const event = new window.Event('beforeunload', { cancelable: true });
  window.dispatchEvent(event);
  assert.equal(event.defaultPrevented, expected, 'Unload guard follows the real editor dirty state');
}
async function type(el, value) {
  assert.equal(el.matches(':disabled'), false, 'Only type into interactive editors');
  const proto = el instanceof window.HTMLTextAreaElement ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  await flush(() => {
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
    el.dispatchEvent(new window.Event('input', { bubbles: true }));
    el.dispatchEvent(new window.Event('change', { bubbles: true }));
  });
}
async function click(key) {
  assert.equal(button(key).disabled, false, `${key} must be enabled`);
  await flush(() => button(key).click());
}
async function ticks(count) {
  for (let i = 0; i < count; i++) {
    const list = [...intervals.values()].filter(timer => timer.delay === 15000);
    assert.equal(list.length, 1, 'Exactly one real provider polling interval');
    await flush(() => list[0].fn());
  }
}
try {
  api = await server.ssrLoadModule('/src/lib/hermes-api.ts');
  const { MissionControlProvider, useMissionControl } = await server.ssrLoadModule('/src/lib/mission-control-store.tsx');
  const { ConfigRoute } = await server.ssrLoadModule('/src/routes/ConfigRoute.tsx');
  function Observe() { context = useMissionControl(); return React.createElement(ConfigRoute); }
  async function mount(initialFailure = null) {
    text = BASE_TEXT; parsed = { fixture: { label: 'disk-value', items: ['one'] } };
    failure = initialFailure; deferredRead = null; deferReads = false; deferredWrite = null; deferWrites = false;
    discardConfirmed = false; allowWrites = false; failReadback = false;
    calls.length = 0; confirmCount = 0;
    root = createRoot(document.getElementById('root'));
    await flush(() => root.render(React.createElement(React.StrictMode, null,
      React.createElement(MissionControlProvider, null, React.createElement(Observe)))));
    if (!initialFailure) assert.equal(label().value, 'disk-value');
    assert.equal(context.config.available, !initialFailure);
    expectUnloadGuard(false);
  }
  async function unmount() {
    await flush(() => root.unmount()); root = null;
    assert.equal(intervals.size, 0);
    expectUnloadGuard(false);
    scenarioCount++;
  }

  await mount();
  await type(label(), 'unsaved-form');
  expectUnloadGuard(true);
  const before = context.config;
  await ticks(3);
  assert.equal(context.config, before);
  assert.equal(label().value, 'unsaved-form');
  await ticks(1);
  assert.notEqual(context.config, before);
  assert.equal(context.config.hash, before.hash);
  assert.equal(label().value, 'unsaved-form', 'Identical config poll must preserve the unsaved form draft');
  assert.equal(button('config.save').disabled, false);
  await unmount();

  await mount();
  await type(label(), 'unsaved-external');
  const baseHash = context.config.hash;
  text = BASE_TEXT.replace('disk-value', 'external-value');
  parsed.fixture.label = 'external-value';
  await ticks(4);
  assert.equal(label().value, 'unsaved-external');
  assert.notEqual(context.config.hash, baseHash);
  assert.ok(document.body.textContent.includes(baseHash.slice(0, 12)), 'Displayed lock token must describe the editor base');
  assert.ok(document.body.textContent.includes('config.changedExternally'), 'Warn about external changes without replacing the draft');
  allowWrites = true;
  await click('config.save');
  assert.equal(calls.find(call => call.method === 'PUT').body.hash, baseHash, 'Save must use the accepted editor base, not the latest poll hash');
  assert.equal(label().value, 'unsaved-external');
  assert.equal(parsed.fixture.label, 'external-value', 'Conflict must not overwrite external changes');
  assert.equal(button('config.save').disabled, false);
  assert.ok(document.body.textContent.includes('config save API returned 409'));
  expectUnloadGuard(true);
  await unmount();

  await mount();
  await type(label(), 'reload-draft');
  const readsBeforeCancel = calls.length;
  await click('config.reload');
  assert.equal(confirmCount, 1, 'Dirty Reload must ask before discarding');
  assert.equal(calls.length, readsBeforeCancel, 'Cancelled Reload must not fetch');
  assert.equal(label().value, 'reload-draft');
  expectUnloadGuard(true);
  discardConfirmed = true;
  deferReads = true;
  await click('config.reload');
  assert.ok(deferredRead);
  assert.equal(label().matches(':disabled'), true, 'Freeze editors while accepting an explicit Reload');
  await flush(() => { deferredRead(); deferredRead = null; });
  assert.equal(label().value, 'disk-value');
  assert.equal(label().matches(':disabled'), false);
  expectUnloadGuard(false);
  assert.equal(button('config.save').disabled, true);
  await type(label(), 'pull-draft');
  discardConfirmed = false;
  const readsBeforePull = calls.length;
  const container = document.querySelector('.route-page-scroll');
  for (const [event, clientY] of [['mousedown', 0], ['mousemove', 160], ['mouseup', 160]]) {
    await flush(() => container.dispatchEvent(new window.MouseEvent(event, { clientY, bubbles: true })));
  }
  assert.equal(confirmCount, 3, 'Actual pull hook must use the same discard confirmation');
  assert.equal(calls.length, readsBeforePull);
  assert.equal(label().value, 'pull-draft');
  discardConfirmed = true;
  for (const [event, clientY] of [['mousedown', 0], ['mousemove', 160], ['mouseup', 160]]) {
    await flush(() => container.dispatchEvent(new window.MouseEvent(event, { clientY, bubbles: true })));
  }
  assert.equal(confirmCount, 4);
  assert.equal(label().value, 'disk-value');
  assert.equal(button('config.save').disabled, true);
  await unmount();

  await mount();
  allowWrites = true;
  await type(label(), 'saved-value');
  deferReads = true;
  await ticks(4);
  assert.ok(deferredRead, 'An old config poll is in flight');
  await click('config.save');
  const savedHash = context.config.hash;
  assert.equal(label().value, 'saved-value');
  assert.equal(button('config.save').disabled, true);
  assert.equal(parsed.fixture.label, 'saved-value');
  expectUnloadGuard(false);
  await flush(() => { deferredRead(); deferredRead = null; });
  assert.equal(context.config.hash, savedHash, 'Pre-save polling response must not replace the post-save revision');
  assert.equal(label().value, 'saved-value');
  await type(label(), 'second-save');
  await click('config.save');
  assert.equal(calls.filter(call => call.method === 'PUT')[1].body.hash, savedHash);
  assert.equal(button('config.save').disabled, true);
  await unmount();

  await mount();
  await click('config.yamlMode');
  const uncertainDraft = BASE_TEXT.replace('disk-value', 'save-without-readback');
  await type(raw(), uncertainDraft);
  allowWrites = true;
  failReadback = true;
  await click('config.save');
  assert.equal(raw().value, uncertainDraft, 'Failed post-save readback must not replace the draft with a fallback');
  assert.ok(document.body.textContent.includes('config.saveUnverified'));
  assert.ok(!document.body.textContent.includes('config.saved'));
  assert.equal(button('config.save').disabled, false);
  assert.equal(parsed.fixture.label, 'save-without-readback', 'The write may have succeeded even though readback failed');
  expectUnloadGuard(true);
  await click('config.resetDraft');
  expectUnloadGuard(false);
  assert.equal(raw().value, BASE_TEXT, 'Reset during outage must use the last accepted live base, never demo config');
  await unmount();

  await mount('http');
  await click('config.yamlMode');
  assert.equal(raw().matches(':disabled'), true, 'Fallback config is not an editable or saveable live base');
  assert.equal(button('config.save').disabled, true);
  failure = null;
  await ticks(1);
  assert.equal(raw().value, BASE_TEXT);
  assert.equal(raw().matches(':disabled'), false);
  await unmount();

  for (const mode of ['yaml', 'invalid-yaml', 'invalid-array']) {
    await mount();
    let editor, edited;
    if (mode === 'invalid-array') {
      editor = document.querySelector('textarea');
      edited = '[broken';
    } else {
      await click('config.yamlMode');
      editor = raw();
      edited = mode === 'yaml' ? BASE_TEXT.replace('disk-value', 'yaml-draft') : '[broken';
    }
    await type(editor, edited);
    expectUnloadGuard(true);
    await ticks(4);
    assert.equal(editor.value, edited, `Polling must preserve ${mode}`);
    if (mode === 'invalid-yaml') {
      assert.equal(button('config.save').disabled, true);
      assert.ok(document.body.textContent.includes('config.yamlInvalid'));
    } else {
      assert.equal(button('config.save').disabled, false);
    }
    if (mode === 'invalid-array') {
      await click('config.save');
      assert.equal(calls.filter(call => call.method !== 'GET').length, 0);
      assert.equal(editor.value, edited);
    }
    await unmount();
  }

  for (const mode of ['http', 'network', 'auth']) {
    await mount();
    await click('config.yamlMode');
    const edited = BASE_TEXT.replace('disk-value', `draft-${mode}`);
    await type(raw(), edited);
    failure = mode;
    await ticks(4);
    assert.equal(context.config.available, false);
    assert.equal(raw().value, edited);
    assert.equal(button('config.save').disabled, false);
    discardConfirmed = true;
    await click('config.reload');
    assert.equal(raw().value, edited, 'Failed explicit Reload must retain even a confirmed draft');
    assert.ok(document.body.textContent.includes('config.failedReload'));
    expectUnloadGuard(true);
    assert.ok(!document.body.textContent.includes('config.reloaded'));
    failure = null;
    await click('config.reload');
    assert.equal(raw().value, BASE_TEXT);
    expectUnloadGuard(false);
    assert.equal(button('config.save').disabled, true);
    await unmount();
  }

  await mount();
  await click('config.yamlMode');
  const sameAsExternal = BASE_TEXT.replace('disk-value', 'same-as-external');
  await type(raw(), sameAsExternal);
  text = sameAsExternal; parsed.fixture.label = 'same-as-external';
  await ticks(4);
  assert.equal(raw().value, sameAsExternal);
  assert.equal(button('config.save').disabled, false, 'Dirty is relative to the accepted base, not to a poll matching the draft');
  assert.ok(document.body.textContent.includes('config.changedExternally'));
  await unmount();

  await mount();
  await type(label(), 'filtered-draft');
  const search = document.querySelector('input[aria-label="config.searchKeys"]');
  await type(search, 'fixture');
  await click('config.showChangedOnly');
  await ticks(4);
  assert.equal(search.value, 'fixture');
  assert.ok(button('config.showAllFields'));
  assert.equal(label().value, 'filtered-draft');
  await unmount();

  await mount();
  text = BASE_TEXT.replace('disk-value', 'clean-update'); parsed.fixture.label = 'clean-update';
  await ticks(4);
  assert.equal(label().value, 'clean-update');
  assert.equal(button('config.save').disabled, true);
  await click('config.reload');
  assert.equal(confirmCount, 0, 'Clean Reload needs no discard confirmation');
  await unmount();

  await mount();
  deferReads = true;
  let olderRead;
  await flush(() => { olderRead = context.reloadConfig(); });
  assert.ok(deferredRead);
  text = BASE_TEXT.replace('disk-value', 'newer-read'); parsed.fixture.label = 'newer-read';
  await flush(() => context.reloadConfig());
  const newestHash = context.config.hash;
  assert.equal(label().value, 'newer-read');
  await flush(async () => { deferredRead(); deferredRead = null; await olderRead; });
  assert.equal(context.config.hash, newestHash);
  assert.equal(label().value, 'newer-read');
  await unmount();

  await mount();
  await type(label(), 'save-freeze');
  allowWrites = true;
  deferReads = true;
  await click('config.save');
  assert.ok(deferredRead, 'Canonical save readback is in flight');
  assert.equal(label().matches(':disabled'), true, 'Freeze editors during Save, not only Reload');
  assert.equal(button('config.save').disabled, true);
  assert.equal(button('config.reload').disabled, true);
  const requestsDuringSave = calls.length;
  const saveContainer = document.querySelector('.route-page-scroll');
  for (const [event, clientY] of [['mousedown', 0], ['mousemove', 160], ['mouseup', 160]]) {
    await flush(() => saveContainer.dispatchEvent(new window.MouseEvent(event, { clientY, bubbles: true })));
  }
  assert.equal(calls.length, requestsDuringSave, 'Pull must not overlap an editor Save');
  assert.equal(calls.filter(call => call.method === 'PUT').length, 1);
  await flush(() => { deferredRead(); deferredRead = null; });
  assert.equal(label().matches(':disabled'), false);
  assert.equal(label().value, 'save-freeze');
  assert.equal(button('config.save').disabled, true);
  assert.equal(button('config.reload').disabled, false);
  await unmount();

  await mount();
  await type(label(), 'delayed-write');
  allowWrites = true;
  deferWrites = true;
  await click('config.save');
  assert.ok(deferredWrite, 'The PUT itself is still in flight');
  assert.equal(label().matches(':disabled'), true, 'Freeze the form throughout the write, not just its readback');
  assert.equal(button('config.resetDraft').disabled, true);
  deferReads = true;
  await ticks(4);
  assert.ok(deferredRead, 'A poll started during the write captured the old revision');
  await flush(() => { deferredWrite(); deferredWrite = null; });
  const writeHash = context.config.hash;
  assert.equal(parsed.fixture.label, 'delayed-write');
  assert.equal(label().value, 'delayed-write');
  assert.equal(button('config.save').disabled, true);
  await flush(() => { deferredRead(); deferredRead = null; });
  assert.equal(context.config.hash, writeHash, 'Polls started during the write cannot roll back canonical readback');
  assert.equal(label().value, 'delayed-write');
  await unmount();

  await mount();
  await click('config.yamlMode');
  const touchDraft = BASE_TEXT.replace('disk-value', 'touch-draft');
  await type(raw(), touchDraft);
  const touchContainer = document.querySelector('.route-page-scroll');
  async function touchPull() {
    for (const [name, clientY] of [['touchstart', 0], ['touchmove', 160], ['touchend', 160]]) {
      await flush(() => {
        const event = new window.Event(name, { bubbles: true, cancelable: true });
        Object.defineProperty(event, 'touches', { value: name === 'touchend' ? [] : [{ clientY }] });
        touchContainer.dispatchEvent(event);
      });
    }
  }
  const touchReads = calls.length;
  await touchPull();
  assert.equal(confirmCount, 1, 'Touch pull reaches the real hook and asks before discarding YAML');
  assert.equal(calls.length, touchReads);
  assert.equal(raw().value, touchDraft);
  discardConfirmed = true;
  await touchPull();
  assert.equal(confirmCount, 2);
  assert.equal(raw().value, BASE_TEXT);
  assert.equal(button('config.save').disabled, true);
  await unmount();

  console.log(`Config draft lifecycle: ${scenarioCount} scenarios passed.`);
} finally {
  if (root) await flush(() => root.unmount());
  await server.close();
  dom.window.close();
  rmSync(cacheDir, { recursive: true, force: true });
}
