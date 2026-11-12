import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';

const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/', pretendToBeVisual: true });
for (const key of ['window','document','HTMLElement','HTMLInputElement','HTMLSelectElement','HTMLFormElement','Event','KeyboardEvent','MouseEvent','Node']) globalThis[key] = dom.window[key];
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
window.HTMLElement.prototype.scrollIntoView = () => {};
const settle = () => new Promise((resolve) => setImmediate(resolve));
async function flush(fn = () => {}) { await act(async () => { await fn(); for (let i = 0; i < 5; i++) await settle(); }); }
const peer = { peer_id: 'peer-b', installation_id: 'install-b', profile: 'default', capability_digest: 'c'.repeat(64) };
const lobby = { lobby_id: 'lobby-1', name: 'x', state: 'open', roster: [] };
function fakeApi(overrides = {}) { return { getPairing: async () => ({ peers: [peer], owner_profile: 'default' }), createLobby: async () => lobby, ...overrides }; }
function alertText() { return document.querySelector('[role="alert"]')?.textContent ?? ''; }
let currentRoot;
async function mount(Component, I18nProvider, api) {
  if (currentRoot) await flush(() => currentRoot.unmount());
  const root = document.getElementById('root'); root.replaceChildren();
  const reactRoot = (await import('react-dom/client')).createRoot(root); currentRoot = reactRoot;
  await flush(() => reactRoot.render(React.createElement(I18nProvider, null, React.createElement(Component, { api, onCreated: () => {} }))));
  const input = document.querySelector('input[aria-label="Lobby name"]'); Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, 'Room'); input.dispatchEvent(new Event('input', { bubbles: true }));
  const select = document.querySelector('select[aria-label="Paired peer"]'); Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set.call(select, 'peer-b'); select.dispatchEvent(new Event('change', { bubbles: true }));
  await flush();
  return reactRoot;
}
async function suite(flag) {
  const cacheDir = mkdtempSync(path.join(tmpdir(), `ttl-${flag}-`));
  const vite = await createServer({ root: process.cwd(), configFile: false, cacheDir, logLevel: 'silent', appType: 'custom', server: { middlewareMode: true, hmr: false }, plugins: [react()], define: { 'import.meta.env.VITE_ROOMS_EXTENDED_TTL': JSON.stringify(flag ? '1' : '') } });
  const { InviteLobbyCreateForm } = await vite.ssrLoadModule('/src/components/chat/InviteLobbyCreateForm.tsx');
  const { I18nProvider } = await vite.ssrLoadModule('/src/lib/i18n.tsx');
  const { RoomInviteApiError, createRoomInviteApi } = await vite.ssrLoadModule('/src/lib/room-invite-api.ts');
  try { return { Component: InviteLobbyCreateForm, I18nProvider, RoomInviteApiError, createRoomInviteApi, close: async () => { await vite.close(); rmSync(cacheDir, { recursive: true, force: true }); } }; } catch (e) { await vite.close(); throw e; }
}

const on = await suite(true);
try {
  test('flag ON renders and navigates four TTL radios, serializes every preset, and preserves sent-hint error', async () => {
    let reject = true; const bodies = [];
    const api = fakeApi({ createLobby: async (body) => { bodies.push(body); if (reject) throw new on.RoomInviteApiError({ kind: 'error', code: 'invalid_lobby', status: 400 }); return lobby; } });
    const root = await mount(on.Component, on.I18nProvider, api);
    const group = document.querySelector('[role="radiogroup"]'); assert.ok(group, 'radiogroup labelled');
    const radios = [...document.querySelectorAll('[role="radio"]')]; assert.equal(radios.length, 4); assert.equal(radios[0].getAttribute('aria-checked'), 'true'); assert.equal(radios[0].tabIndex, 0); assert.deepEqual(radios.slice(1).map((n) => n.tabIndex), [-1, -1, -1]);
    radios[0].focus(); radios[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true })); await flush(); assert.equal(document.activeElement, radios[3]);
    radios[3].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })); await flush(); assert.equal(document.activeElement, radios[0]);
    radios[0].click(); radios[1].click(); await flush();
    document.querySelector('button').textContent; [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Create lobby')).click(); await flush(); assert.equal(bodies[0].grant_ttl_hint_seconds, 3600); assert.match(alertText(), /does not allow longer/);
    radios[0].click(); await flush(); assert.match(alertText(), /does not allow longer/, 'specific alert remains keyed to sent hint');
    reject = false; [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Create lobby')).click(); await flush(); assert.equal('grant_ttl_hint_seconds' in bodies.at(-1), false);
    for (const [index, seconds] of [[2, 28800], [3, 86400]]) { radios[index].click(); await flush(); [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Create lobby')).click(); await flush(); assert.equal(bodies.at(-1).grant_ttl_hint_seconds, seconds); }
    await root.unmount();
  });
  test('flag ON keeps generic errors for an unhinted invalid lobby and other hinted failures', async () => {
    let code = 'invalid_lobby';
    const api = fakeApi({ createLobby: async () => { throw new on.RoomInviteApiError({ kind: 'error', code }); } });
    const root = await mount(on.Component, on.I18nProvider, api);
    const submit = [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Create lobby'));
    submit.click(); await flush();
    assert.match(alertText(), /invalid_lobby/, 'unhinted invalid_lobby retains the generic error code');
    assert.doesNotMatch(alertText(), /does not allow longer/, 'unhinted invalid_lobby is not attributed to an extended TTL');
    const radios = [...document.querySelectorAll('[role="radio"]')];
    radios[1].click(); await flush(); code = 'other_failure';
    submit.click(); await flush();
    assert.match(alertText(), /other_failure/, 'other errors retain their generic error code even with a hint');
    assert.doesNotMatch(alertText(), /does not allow longer/);
    await root.unmount(); currentRoot = undefined;
  });
  test('flag ON maps only hinted HTTP 400 invalid_lobby to the TTL-specific message', async () => {
    let status = 400;
    const realApi = on.createRoomInviteApi(undefined, async () => new Response(JSON.stringify({ code: 'invalid_lobby' }), { status, headers: { 'content-type': 'application/json' } }));
    const root = await mount(on.Component, on.I18nProvider, fakeApi({ createLobby: realApi.createLobby }));
    const radios = [...document.querySelectorAll('[role="radio"]')]; radios[1].click(); await flush();
    for (status of [400, 401, 403, 409, 500, 503]) {
      [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Create lobby')).click(); await flush();
      if (status === 400) assert.match(alertText(), /does not allow longer/);
      else { assert.match(alertText(), /invalid_lobby/); assert.doesNotMatch(alertText(), /does not allow longer/); }
    }
    await root.unmount(); currentRoot = undefined;
  });
  test('flag ON disables radios while pending and dedupes submit', async () => {
    let finish; let calls = 0; const api = fakeApi({ createLobby: () => { calls++; return new Promise((resolve) => { finish = resolve; }); } });
    const root = await mount(on.Component, on.I18nProvider, api); const radios = [...document.querySelectorAll('[role="radio"]')]; radios[1].click(); await flush();
    const submit = [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Create lobby')); submit.click(); submit.click(); await flush(); assert.equal(calls, 1); assert.ok(radios.every((r) => r.disabled)); finish(lobby); await flush(); await root.unmount(); currentRoot = undefined;
  });
} finally { await on.close(); }

const off = await suite(false);
try {
  test('flag OFF renders static duration and sends today body without a hint', async () => {
    const bodies = []; const api = fakeApi({ createLobby: async (body) => { bodies.push(body); return lobby; } });
    const root = await mount(off.Component, off.I18nProvider, api); assert.equal(document.querySelectorAll('[role="radio"]').length, 0); assert.equal(document.querySelector('[role="radiogroup"]'), null); [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Create lobby')).click(); await flush(); assert.deepEqual(bodies[0], { name: 'Room', roster: [{ member_id: 'local-owner', handle: 'default' }, { member_id: 'peer-peer-b', handle: 'peer-b', target: { peer_id: 'peer-b' } }], history_policy: 'postjoin' }); await root.unmount(); currentRoot = undefined;
  });
} finally { await off.close(); }
