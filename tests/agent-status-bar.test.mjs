import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';

const dom = new JSDOM('<!doctype html><div id="root"></div>', {
  url: 'http://localhost/?botProfile=builder', pretendToBeVisual: true,
});
for (const key of ['window', 'document', 'HTMLElement', 'Event']) globalThis[key] = dom.window[key];
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const originalFetch = globalThis.fetch;
const originalWebSocket = globalThis.WebSocket;
const calls = [];
const sockets = [];
let response = { profiles: [{ name: 'builder', display_name: 'Builder' }, { name: 'default', is_default: true, display_name: 'Primary Agent' }] };
let deferResponses = false;
let store = { authRequired: false, storedToken: 'fixture-token', snapshot: {
  gatewayStatus: 'online', activeAgents: 1, activeModel: 'fixture-model', fallbackModel: 'fixture-fallback',
} };
globalThis.__statusBarStore = () => store;
globalThis.fetch = async (input) => {
  assert.equal(String(input), '/api/gateway-root', 'Only the existing credential bootstrap is permitted');
  return new Response('<script>window.__HERMES_SESSION_TOKEN__ = "fixture-only";</script>');
};
class FixtureSocket {
  constructor() { sockets.push(this); queueMicrotask(() => this.onopen?.()); }
  send(payload) {
    this.request = JSON.parse(payload);
    calls.push(this.request);
    if (!deferResponses) queueMicrotask(() => this.reply(response));
  }
  reply(result) { this.onmessage?.({ data: JSON.stringify({ jsonrpc: '2.0', id: this.request.id, result }) }); }
  fail() { this.onerror?.(); }
  close() { this.closed = true; }
}
globalThis.WebSocket = FixtureSocket;
const cacheDir = mkdtempSync(path.join(tmpdir(), 'mc-status-bar-'));
const server = await createServer({
  root: process.cwd(), configFile: false, cacheDir, appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true, hmr: false },
  plugins: [react(), {
    name: 'status-bar-store-fixture', enforce: 'pre',
    resolveId(id) { if (/\/mission-control-store(?:\.tsx?)?$/.test(id)) return '\0status-bar-store'; },
    load(id) { if (id === '\0status-bar-store') return 'export const useMissionControl=()=>globalThis.__statusBarStore();'; },
  }],
});
const { createRoot } = await import('react-dom/client');
let root;
try {
  const { AgentStatusBar } = await server.ssrLoadModule('/src/components/overview/AgentStatusBar.tsx');
  const { I18nProvider } = await server.ssrLoadModule('/src/lib/i18n.tsx');
  async function flush(action = () => {}) {
    await act(async () => { action(); for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); });
  }
  const render = () => root.render(React.createElement(I18nProvider, null, React.createElement(AgentStatusBar)));
  const identity = () => document.querySelector('.text-sm.font-semibold')?.textContent;
  root = createRoot(document.getElementById('root'));

  await test('Overview shows the primary display name, not the chat bot or static Hermes label', async () => {
    await flush(render);
    assert.equal(identity(), 'Primary Agent');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, 'profiles.list');
    assert.deepEqual(calls[0].params, { include_sessions: false });
    assert.equal(document.body.textContent.includes('fixture-model'), true);
    assert.equal(document.body.textContent.includes('fixture-fallback'), true);
    assert.equal(sockets[0].closed, true);
  });

  await test('Returning to Overview refreshes the primary name without following the selected bot', async () => {
    response = { profiles: [{ name: 'builder', display_name: 'Different Bot' }, { name: 'default', display_name: '  Renamed Primary  ' }] };
    await flush(() => window.dispatchEvent(new Event('focus')));
    assert.equal(identity(), 'Renamed Primary');
    assert.equal(calls.length, 2);
  });

  await test('An older focus response cannot overwrite a newer primary name', async () => {
    deferResponses = true;
    await flush(() => window.dispatchEvent(new Event('focus')));
    const older = sockets.at(-1);
    await flush(() => window.dispatchEvent(new Event('focus')));
    const newer = sockets.at(-1);
    await flush(() => newer.reply({ profiles: [{ name: 'default', display_name: 'Newest Primary' }] }));
    await flush(() => older.reply({ profiles: [{ name: 'default', display_name: 'Obsolete Primary' }] }));
    assert.equal(identity(), 'Newest Primary');
    deferResponses = false;
  });

  await test('Transient failures retain the last valid name', async () => {
    deferResponses = true;
    await flush(() => window.dispatchEvent(new Event('focus')));
    await flush(() => sockets.at(-1).fail());
    assert.equal(identity(), 'Newest Primary');
    deferResponses = false;
  });

  await test('Missing, blank, malformed and legacy metadata use Hermes, never a bot name', async () => {
    for (const payload of [
      { profiles: [{ name: 'builder', is_default: true, display_name: 'Do not use a bot' }] },
      { profiles: [{ name: 'default' }] },
      { profiles: [{ name: 'default', display_name: ' \n ' }] },
      { profiles: [{ name: 'default', display_name: 42 }] },
      { profiles: [null, 'invalid', { name: 'default', display_name: '' }] },
      {}, null,
    ]) {
      response = payload;
      await flush(() => window.dispatchEvent(new Event('focus')));
      assert.equal(identity(), 'Hermes');
    }
  });

  await test('The full long name is available in the title', async () => {
    const name = 'Primary '.repeat(8).trim();
    response = { profiles: [{ name: 'default', display_name: name }] };
    await flush(() => window.dispatchEvent(new Event('focus')));
    assert.equal(identity(), name);
    assert.equal(document.querySelector('.text-sm.font-semibold').title, name);
  });

  await test('Token changes scrub the old identity and reject late responses', async () => {
    deferResponses = true;
    await flush(() => window.dispatchEvent(new Event('focus')));
    const oldTokenSocket = sockets.at(-1);
    store = { ...store, storedToken: 'fixture-other-token' };
    await flush(render);
    const newTokenSocket = sockets.at(-1);
    assert.equal(identity(), 'Hermes', 'No previous identity during authentication changes');
    await flush(() => newTokenSocket.reply({ profiles: [{ name: 'default', display_name: 'Other Primary' }] }));
    await flush(() => oldTokenSocket.reply({ profiles: [{ name: 'default', display_name: 'Old Credential Primary' }] }));
    assert.equal(identity(), 'Other Primary');
    deferResponses = false;
  });

  await test('Lock removes identity, makes no requests and fences pending responses', async () => {
    deferResponses = true;
    await flush(() => window.dispatchEvent(new Event('focus')));
    const pending = sockets.at(-1);
    store = { ...store, authRequired: true };
    const count = calls.length;
    await flush(render);
    assert.equal(identity(), 'Hermes');
    await flush(() => pending.reply({ profiles: [{ name: 'default', display_name: 'Late Secret Label' }] }));
    await flush(() => window.dispatchEvent(new Event('focus')));
    assert.equal(calls.length, count);
    assert.equal(identity(), 'Hermes');
    deferResponses = false;
  });

  await test('No token means no identity request; an initial failure does not break the bar', async () => {
    store = { ...store, authRequired: false, storedToken: '' };
    const count = calls.length;
    await flush(render);
    await flush(() => window.dispatchEvent(new Event('focus')));
    assert.equal(calls.length, count);
    assert.equal(identity(), 'Hermes');
    deferResponses = true;
    store = { ...store, storedToken: 'fixture-retry-token' };
    await flush(render);
    await flush(() => sockets.at(-1).fail());
    assert.equal(identity(), 'Hermes');
    deferResponses = false;
  });

  await test('Unmount removes the focus listener and late responses cannot update the page', async () => {
    deferResponses = true;
    await flush(() => window.dispatchEvent(new Event('focus')));
    const pending = sockets.at(-1);
    await flush(() => root.unmount());
    root = null;
    const count = calls.length;
    await flush(() => window.dispatchEvent(new Event('focus')));
    await flush(() => pending.reply({ profiles: [{ name: 'default', display_name: 'Unmounted' }] }));
    assert.equal(calls.length, count);
    assert.equal(document.getElementById('root').textContent, '');
    assert.ok(sockets.every(socket => socket.closed), 'Every settled RPC socket is closed');
  });
} finally {
  if (root) await act(async () => root.unmount());
  await server.close();
  dom.window.close();
  rmSync(cacheDir, { recursive: true, force: true });
  globalThis.fetch = originalFetch;
  globalThis.WebSocket = originalWebSocket;
  delete globalThis.__statusBarStore;
}
