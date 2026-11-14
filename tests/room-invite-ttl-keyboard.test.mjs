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
 test('review: accessible name and every keyboard direction update focus, checked state and tabstop', async () => {
  const root = await mount(on.Component,on.I18nProvider,fakeApi());
  const group=document.querySelector('[role="radiogroup"]');
  assert.equal(group.getAttribute('aria-label'),'Access duration','TTL group accessible name');
  const radios=[...group.querySelectorAll('[role="radio"]')];
  const steps=[['End',3],['Home',0],['ArrowLeft',3],['ArrowRight',0],['ArrowUp',3],['ArrowDown',0],['ArrowDown',1],['ArrowUp',0]];
  radios[0].focus();
  for(const [key,index] of steps){document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key,bubbles:true}));await flush();assert.ok(document.activeElement===radios[index],`${key} must focus option ${index}`);assert.deepEqual(radios.map(r=>r.getAttribute('aria-checked')),radios.map((r,i)=>String(i===index)),`${key} checked state`);assert.deepEqual(radios.map(r=>r.tabIndex),radios.map((r,i)=>i===index?0:-1),`${key} roving tabindex`);}
  await flush(()=>root.unmount());currentRoot=undefined;
 });

}finally{await on.close();}
