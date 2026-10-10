import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';

// MC-FIX-8: real ToolsRoute, real MissionControlProvider, real pull-to-reload
// hook driven by touch events. Fixtures: fetch, translations, diagnostics.
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/tools', pretendToBeVisual: true });
for (const key of ['window', 'document', 'HTMLElement', 'Event', 'MouseEvent', 'CustomEvent', 'Node', 'Element']) {
  globalThis[key] = dom.window[key];
}
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
window.setInterval = () => 0; // store polling never fires: only the gesture may fetch
window.clearInterval = () => {};
window.localStorage.setItem('mission-control-token', 'fixture-token');

// Core dashboard rows (GET /api/tools/toolsets), not the retired sidecar snapshot.
const toolset = (name) => ({ name, label: `${name} toolset`, description: `${name}_tool`, enabled: true, available: true, configured: true, tools: [`${name}_tool`] });
const TOOLS_PATH = '/api/tools/toolsets';
let toolsResponse;
let toolsGate = null;
const calls = [];
const toolsAuth = [];
globalThis.fetch = async (url, init = {}) => {
  const pathname = new URL(String(url), 'http://localhost').pathname;
  calls.push(pathname);
  if (pathname.endsWith('/tools')) throw new Error(`retired sidecar endpoint requested: ${pathname}`);
  if (pathname === TOOLS_PATH) {
    toolsAuth.push(init.headers?.Authorization ?? null);
    if (toolsGate) await toolsGate.promise;
    if (toolsResponse === 'auth') return new Response('{}', { status: 401 });
    if (toolsResponse === 'http') return new Response('{}', { status: 503 });
    return Response.json(toolsResponse);
  }
  if (pathname.endsWith('/skills')) return Response.json({ available: true, items: [] });
  if (pathname.endsWith('/mission-control/sessions')) return Response.json({ items: [], stats: { totalSessions: 0, activeAgents: 0 } });
  if (pathname.endsWith('/cron/jobs')) return Response.json([]);
  return Response.json({});
};

const cacheDir = mkdtempSync(path.join(tmpdir(), 'mc-tools-pull-'));
const server = await createServer({
  root: process.cwd(), configFile: false, cacheDir, appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true, hmr: false },
  plugins: [react(), {
    name: 'tools-pull-fixtures', enforce: 'pre',
    resolveId(id) {
      if (/\/i18n(?:\.tsx)?$/.test(id)) return '\0fixture-i18n';
      if (/\/reload-diagnostics(?:\.ts)?$/.test(id)) return '\0fixture-diagnostics';
    },
    load(id) {
      if (id === '\0fixture-i18n') return "export const useI18n = () => ({ locale: 'en', t: (key, vars) => vars?.detail ? `${key}: ${vars.detail}` : key });";
      if (id === '\0fixture-diagnostics') return 'export const recordReloadDiagnostic = () => {};';
    },
  }],
});
const { createRoot } = await import('react-dom/client');
const settle = () => new Promise((resolve) => setImmediate(resolve));
async function flush(action = () => {}) {
  await act(async () => { await action(); for (let i = 0; i < 8; i++) await settle(); });
}
const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; };

function touch(type, clientY) {
  const event = new window.Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'touches', { value: type === 'touchend' ? [] : [{ clientY }] });
  return event;
}
const scroller = () => document.querySelector('.route-page-scroll');
async function pull() {
  await flush(() => {
    scroller().dispatchEvent(touch('touchstart', 0));
    scroller().dispatchEvent(touch('touchmove', 40));
    scroller().dispatchEvent(touch('touchmove', 260));
    scroller().dispatchEvent(touch('touchend', 260));
  });
}
const toolCalls = () => calls.filter((p) => p === TOOLS_PATH).length;
const otherCalls = () => calls.filter((p) => p !== TOOLS_PATH);
const text = () => document.getElementById('root').textContent;

let root;
let Provider;
let ToolsRoute;
async function mount() {
  toolsResponse = [toolset('alpha')];
  toolsGate = null;
  toolsAuth.length = 0;
  root = createRoot(document.getElementById('root'));
  await flush(() => root.render(React.createElement(Provider, null, React.createElement(ToolsRoute))));
  await flush();
  assert.match(text(), /alpha toolset/);
  calls.length = 0;
}
async function unmount() { await flush(() => root.unmount()); }

try {
  ({ MissionControlProvider: Provider } = await server.ssrLoadModule('/src/lib/mission-control-store.tsx'));
  ({ ToolsRoute } = await server.ssrLoadModule('/src/routes/ToolsRoute.tsx'));

  await test('pull fetches Tools only and shows the new data', async () => {
    await mount();
    try {
      toolsResponse = [toolset('beta')];
      await pull();
      assert.equal(toolCalls(), 1);
      assert.deepEqual(toolsAuth.slice(-1), ['Bearer fixture-token'], 'dashboard API needs the Mission Control bearer');
      assert.deepEqual(otherCalls(), [], 'no Sessions/Config/Cron/Skills refresh from the gesture');
      assert.match(text(), /beta toolset/);
      assert.doesNotMatch(text(), /alpha toolset/);
    } finally { await unmount(); }
  });

  await test('spinner stays until the request settles and a second pull is ignored', async () => {
    await mount();
    try {
      toolsGate = deferred();
      await pull();
      assert.equal(document.querySelector('.ptr-surface')?.getAttribute('aria-busy'), 'true', 'spinner while pending');
      await pull();
      assert.equal(toolCalls(), 1, 'concurrent gesture does not start a second request');
      toolsGate.resolve();
      toolsGate = null;
      await flush();
      assert.equal(document.querySelector('.ptr-surface'), null, 'spinner gone after settle');
      await pull();
      assert.equal(toolCalls(), 2);
    } finally { await unmount(); }
  });

  await test('a failed refresh keeps the previous data and reports the error', async () => {
    await mount();
    try {
      toolsResponse = 'http';
      await pull();
      assert.equal(toolCalls(), 1);
      assert.match(text(), /alpha toolset/);
      assert.match(text(), /tools\.refreshFailed/);
      toolsResponse = [toolset('gamma')];
      await pull();
      assert.doesNotMatch(text(), /tools\.refreshFailed/);
      assert.match(text(), /gamma toolset/);
    } finally { await unmount(); }
  });

  await test('a payload that is not a toolset list is a failed refresh, not an empty inventory', async () => {
    await mount();
    try {
      toolsResponse = { toolsets: [] };
      await pull();
      assert.equal(toolCalls(), 1);
      assert.match(text(), /alpha toolset/);
      assert.match(text(), /tools\.refreshFailed/);
    } finally { await unmount(); }
  });

  await test('an auth failure is reported, not swallowed', async () => {
    await mount();
    try {
      toolsResponse = 'auth';
      await pull();
      assert.equal(toolCalls(), 1);
      assert.match(text(), /tools\.refreshFailed/);
    } finally { await unmount(); }
  });

  await test('unmounting during a pending refresh does not throw', async () => {
    await mount();
    toolsGate = deferred();
    await pull();
    await unmount();
    toolsGate.resolve();
    toolsGate = null;
    await flush();
  });
} finally {
  await server.close();
  rmSync(cacheDir, { recursive: true, force: true });
}
