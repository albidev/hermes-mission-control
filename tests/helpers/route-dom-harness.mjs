// Production route + React DOM and API loaders; only state, network and unrelated hooks are fixtures.
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
export async function routeHarness(route, store, fetcher) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/', pretendToBeVisual: true });
  for (const key of ['window','document','HTMLElement','HTMLInputElement','HTMLSelectElement','HTMLTextAreaElement','Event','MouseEvent','KeyboardEvent']) globalThis[key] = dom.window[key];
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  window.HTMLElement.prototype.scrollIntoView = () => {};
  const timers = new Map(); let timerId = 0;
  window.setInterval = (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId; };
  window.clearInterval = id => timers.delete(id);
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    const parsed = new URL(String(url), 'http://localhost');
    assert.equal(parsed.origin, 'http://localhost', 'Fixture forbids external requests');
    const call = { path: parsed.pathname, query: parsed.search, method: options.method ?? 'GET', body: options.body ? JSON.parse(options.body) : null };
    calls.push(call); return fetcher(call);
  };
  const subscribers = new Set();
  const fixture = { store, version: 0, subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); } };
  globalThis.__routeDomFixture = fixture;
  const cacheDir = mkdtempSync(path.join(tmpdir(), 'mc-route-dom-'));
  const server = await createServer({ root: process.cwd(), configFile: false, cacheDir, appType: 'custom', logLevel: 'silent', server: { middlewareMode: true, hmr: false }, plugins: [react(), {
    name: 'route-dom-fixture', enforce: 'pre',
    resolveId(id) {
      if (/\/i18n(?:\.tsx)?$/.test(id)) return '\0route-i18n';
      if (/\/mission-control-store(?:\.tsx?)?$/.test(id)) return '\0route-store';
      if (/\/usePullToReload(?:\.tsx?)?$/.test(id)) return '\0route-pull';
      if (/\/PullToReloadIndicator(?:\.tsx?)?$/.test(id)) return '\0route-indicator';
      if (/\/bot-gateway(?:\.ts)?$/.test(id)) return '\0route-bots';
      if (/\/reload-diagnostics(?:\.ts)?$/.test(id)) return '\0route-diagnostics';
    },
    load(id) {
      if (id === '\0route-i18n') return `const t=(key,values={})=>Object.keys(values).length?key+' '+Object.values(values).join('/'):key;export const useI18n=()=>({t,locale:'en'});`;
      if (id === '\0route-store') return `import{useSyncExternalStore}from'react';export function useMissionControl(){const f=globalThis.__routeDomFixture;useSyncExternalStore(f.subscribe,()=>f.version);return f.store;}`;
      if (id === '\0route-pull') return 'export const usePullToReload=()=>({state:null});';
      if (id === '\0route-indicator') return 'export const PullToReloadIndicator=()=>null;';
      if (id === '\0route-diagnostics') return 'export const recordReloadDiagnostic=()=>{};';
      if (id === '\0route-bots') return `export async function loadBotProfiles(){return {profiles:[{name:'fixture-bot',is_bot:true}]}}export async function loadBotModelOptions(){return [{slug:'fixture-provider',name:'Fixture',models:['fixture-model']}]} `;
    },
  }] });
  const { createRoot } = await import('react-dom/client');
  const module = await server.ssrLoadModule(`/src/routes/${route}.tsx`);
  const root = createRoot(document.getElementById('root'));
  const settle = () => new Promise(resolve => setImmediate(resolve));
  async function flush(action = () => {}) { await act(async () => { await action(); for (let i=0;i<6;i++) await settle(); }); }
  await flush(() => root.render(React.createElement(module[route])));
  const find = selector => { const el=document.querySelector(selector); assert.ok(el,`Missing ${selector}`);return el; };
  const button = key => { const el=[...document.querySelectorAll('button')].find(el => el.textContent.trim()===key || el.getAttribute('aria-label')===key);assert.ok(el,`Missing button ${key}`);return el; };
  async function change(el,value) {
    const proto=el instanceof window.HTMLTextAreaElement?window.HTMLTextAreaElement.prototype:el instanceof window.HTMLSelectElement?window.HTMLSelectElement.prototype:window.HTMLInputElement.prototype;
    await flush(()=>{Object.getOwnPropertyDescriptor(proto,'value').set.call(el,value);el.dispatchEvent(new window.Event('input',{bubbles:true}));el.dispatchEvent(new window.Event('change',{bubbles:true}));});
  }
  async function emit() { await flush(()=>{fixture.version++;subscribers.forEach(fn=>fn());}); }
  return { calls, flush, find, button, change, emit, click: key=>flush(()=>button(key).click()), async close(){await flush(()=>root.unmount());await server.close();dom.window.close();rmSync(cacheDir,{recursive:true,force:true});delete globalThis.__routeDomFixture;} };
}
